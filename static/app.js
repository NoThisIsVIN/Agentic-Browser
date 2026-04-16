(function () {
  const form = document.getElementById("agent-form");
  const objectiveInput = document.getElementById("objective");
  const updates = document.getElementById("updates");
  const finalReport = document.getElementById("final-report");
  const reportPanel = document.getElementById("report-panel");
  const previewImage = document.getElementById("preview-image");
  const previewPlaceholder = document.getElementById("preview-placeholder");
  const resultsPanel = document.getElementById("results-panel");
  const runButton = document.getElementById("run-button");
  const runState = document.getElementById("run-state") || { textContent: "", classList: { toggle() {} } };
  const statusText = document.getElementById("status-text");
  const keepBrowserOpenToggle = document.getElementById("keep-browser-open");
  const suggestionPills = Array.from(document.querySelectorAll(".suggestion-pill"));
  const pageShell = document.querySelector(".page-shell");

  let running = false;

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function renderInlineRichText(value) {
    let safe = escapeHtml(value);
    safe = safe.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>'
    );
    safe = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    safe = safe.replace(/`([^`]+)`/g, "<code>$1</code>");
    return safe;
  }

  function renderRichText(value) {
    const lines = String(value || "").replace(/\r\n/g, "\n").split("\n");
    return lines
      .map((line) => {
        const trimmed = line.trim();
        if (!trimmed) {
          return '<div class="rich-gap"></div>';
        }

        const cssClass = /^(\d+\.|[-*])\s/.test(trimmed) ? "rich-line rich-line-list" : "rich-line";
        return `<div class="${cssClass}">${renderInlineRichText(trimmed)}</div>`;
      })
      .join("");
  }

  function animateScrollTo(element, offset = 0, duration = 950) {
    if (!element) {
      return;
    }

    const startY = window.scrollY;
    const targetY = Math.max(0, element.getBoundingClientRect().top + window.scrollY - offset);
    const distance = targetY - startY;
    const startTime = performance.now();

    function easeInOutCubic(t) {
      return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    }

    function step(now) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = easeInOutCubic(progress);
      window.scrollTo(0, startY + distance * eased);
      if (progress < 1) {
        requestAnimationFrame(step);
      }
    }

    requestAnimationFrame(step);
  }

  function setRunningState(isRunning) {
    running = isRunning;
    runButton.disabled = isRunning;
    objectiveInput.disabled = isRunning;
    suggestionPills.forEach((pill) => {
      pill.disabled = isRunning;
    });

    runState.textContent = isRunning ? "Running" : "Idle";
    runState.classList.toggle("idle", !isRunning);
    runState.classList.toggle("running", isRunning);
    document.body.classList.toggle("running-mode", isRunning);
    statusText.textContent = isRunning
      ? "The agent is accelerating through the task and streaming every decision."
      : "Waiting for a mission.";
  }

  function clearOutput() {
    updates.innerHTML = "";
    finalReport.innerHTML = "The final answer from the agent will appear here.";
    previewImage.classList.add("hidden");
    previewImage.removeAttribute("src");
    previewPlaceholder.classList.remove("hidden");
    document.body.classList.remove("result-mode");
  }

  function appendUpdate(message, imageBase64) {
    const item = document.createElement("article");
    item.className = "update-item";

    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    item.innerHTML = `
      <div class="update-meta">
        <span class="update-dot"></span>
        <span>${escapeHtml(time)}</span>
      </div>
      <div class="update-body">${renderRichText(message)}</div>
    `;
    updates.appendChild(item);
    updates.scrollTo({ top: updates.scrollHeight, behavior: "smooth" });

    if (imageBase64) {
      previewImage.src = `data:image/png;base64,${imageBase64}`;
      previewImage.classList.remove("hidden");
      previewPlaceholder.classList.add("hidden");
    }
  }

  async function streamRun(objective) {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        objective,
        keep_browser_open: Boolean(keepBrowserOpenToggle && keepBrowserOpenToggle.checked),
      }),
    });

    if (!response.ok || !response.body) {
      throw new Error("The agent server could not start the run.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          continue;
        }

        const event = JSON.parse(line);
        if (event.type === "update") {
          appendUpdate(event.message, event.image);
        } else if (event.type === "result") {
          finalReport.innerHTML = renderRichText(event.message);
          document.body.classList.remove("running-mode");
          document.body.classList.add("result-mode");
          setTimeout(() => {
            animateScrollTo(reportPanel, 24, 1100);
          }, 260);
        } else if (event.type === "error") {
          finalReport.innerHTML = renderRichText(`**Error:** ${event.message}`);
          document.body.classList.remove("running-mode");
          document.body.classList.add("result-mode");
          setTimeout(() => {
            animateScrollTo(reportPanel, 24, 1100);
          }, 260);
        }
      }
    }
  }

  suggestionPills.forEach((pill) => {
    pill.addEventListener("click", () => {
      objectiveInput.value = pill.textContent.trim();
      objectiveInput.focus();
    });
  });

  document.addEventListener("pointermove", (event) => {
    const x = (event.clientX / window.innerWidth) * 100;
    const y = (event.clientY / window.innerHeight) * 100;
    document.body.style.setProperty("--pointer-x", `${x}%`);
    document.body.style.setProperty("--pointer-y", `${y}%`);
  });

  window.addEventListener(
    "scroll",
    () => {
      const offset = Math.min(window.scrollY * 0.035, 18);
      if (pageShell) {
        pageShell.style.transform = `translateY(${offset}px)`;
      }
    },
    { passive: true }
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const objective = objectiveInput.value.trim();
    if (!objective || running) {
      return;
    }

    clearOutput();
    appendUpdate(`**Mission received:** ${objective}`, null);
    setRunningState(true);
    animateScrollTo(resultsPanel, 18, 1000);

    try {
      await streamRun(objective);
    } catch (error) {
      finalReport.innerHTML = renderRichText(
        `**Error:** ${error.message || "Something went wrong while running the agent."}`
      );
      document.body.classList.remove("running-mode");
      document.body.classList.add("result-mode");
    } finally {
      setRunningState(false);
    }
  });
})();

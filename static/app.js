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
  const downloadFeedButton = document.getElementById("download-feed");
  const suggestionPills = Array.from(document.querySelectorAll(".suggestion-pill"));
  const pageShell = document.querySelector(".page-shell");

  let running = false;
  let currentObjective = "";
  let feedEntries = [];
  let finalReportText = "";
  let tokenLog = [];

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
    feedEntries = [];
    finalReportText = "";
    tokenLog = [];
    if (downloadFeedButton) {
      downloadFeedButton.disabled = true;
    }
    previewImage.classList.add("hidden");
    previewImage.removeAttribute("src");
    previewPlaceholder.classList.remove("hidden");
    document.body.classList.remove("result-mode");
  }

  function appendUpdate(message, imageBase64, tokenInfo) {
    const item = document.createElement("article");
    item.className = "update-item";

    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    feedEntries.push({ time, message: String(message || ""), imageBase64: imageBase64 || "" });
    if (tokenInfo) {
      tokenLog.push(tokenInfo);
    }
    if (downloadFeedButton) {
      downloadFeedButton.disabled = false;
    }
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

  function slugify(value) {
    const slug = String(value || "agent-run")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48);
    return slug || "agent-run";
  }

  function buildFeedMarkdown() {
    const startedAt = new Date().toLocaleString();
    const totalPromptTokens = tokenLog.reduce((s, t) => s + (t.prompt_tokens || 0), 0);
    const totalResponseTokens = tokenLog.reduce((s, t) => s + (t.response_tokens || 0), 0);

    const lines = [
      "# Agentic Browser Live Feed",
      "",
      `**Task:** ${currentObjective || "Untitled run"}`,
      `**Exported:** ${startedAt}`,
      `**Total Steps with AI calls:** ${tokenLog.length}`,
      `**Total Prompt Tokens (sent):** ${totalPromptTokens.toLocaleString()}`,
      `**Total Response Tokens (received):** ${totalResponseTokens.toLocaleString()}`,
      `**Total Tokens:** ${(totalPromptTokens + totalResponseTokens).toLocaleString()}`,
      "",
      "---",
      "",
    ];

    // --- Token Usage Summary Table ---
    if (tokenLog.length) {
      lines.push("## Token Usage Summary");
      lines.push("");
      lines.push("| Step | Prompt Tokens (Sent) | Response Tokens (Received) | Total |");
      lines.push("|------|----------------------|----------------------------|-------|");
      tokenLog.forEach((t) => {
        const total = (t.prompt_tokens || 0) + (t.response_tokens || 0);
        lines.push(
          `| ${t.step} | ${(t.prompt_tokens || 0).toLocaleString()} | ${(t.response_tokens || 0).toLocaleString()} | ${total.toLocaleString()} |`
        );
      });
      lines.push(
        `| **Total** | **${totalPromptTokens.toLocaleString()}** | **${totalResponseTokens.toLocaleString()}** | **${(totalPromptTokens + totalResponseTokens).toLocaleString()}** |`
      );
      lines.push("");
      lines.push("---");
      lines.push("");
    }

    // --- Live Feed ---
    lines.push("## Live Feed");
    lines.push("");

    if (!feedEntries.length) {
      lines.push("No live feed entries were captured.");
    }

    feedEntries.forEach((entry, index) => {
      lines.push(`### ${index + 1}. ${entry.time}`);
      lines.push("");
      lines.push(entry.message);
      lines.push("");
      if (entry.imageBase64) {
        lines.push(`> 📸 *Screenshot captured at this step (not included in export to keep file readable)*`);
        lines.push("");
      }
    });

    // --- Final Report ---
    if (finalReportText) {
      lines.push("## Final Report");
      lines.push("");
      lines.push(finalReportText);
      lines.push("");
      lines.push("---");
      lines.push("");
    }

    // --- Full Prompt/Response Log ---
    if (tokenLog.length) {
      lines.push("## Full Prompt & Response Log");
      lines.push("");
      tokenLog.forEach((t) => {
        lines.push(`### Step ${t.step}`);
        lines.push("");
        lines.push(`**Prompt Tokens:** ${(t.prompt_tokens || 0).toLocaleString()} | **Response Tokens:** ${(t.response_tokens || 0).toLocaleString()}`);
        lines.push("");
        lines.push("<details>");
        lines.push(`<summary>Prompt sent to AI (Step ${t.step})</summary>`);
        lines.push("");
        lines.push("```");
        lines.push(t.prompt_text || "(no prompt captured)");
        lines.push("```");
        lines.push("");
        lines.push("</details>");
        lines.push("");
        lines.push("<details>");
        lines.push(`<summary>Response received from AI (Step ${t.step})</summary>`);
        lines.push("");
        lines.push("```json");
        lines.push(t.response_text || "(no response captured)");
        lines.push("```");
        lines.push("");
        lines.push("</details>");
        lines.push("");
        lines.push("---");
        lines.push("");
      });
    }

    return lines.join("\n");
  }

  function downloadFeed() {
    const markdown = buildFeedMarkdown();
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = url;
    link.download = `${slugify(currentObjective)}-${timestamp}.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
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
          appendUpdate(event.message, event.image, event.token_info || null);
        } else if (event.type === "result") {
          finalReportText = String(event.message || "");
          finalReport.innerHTML = renderRichText(event.message);
          document.body.classList.remove("running-mode");
          document.body.classList.add("result-mode");
          setTimeout(() => {
            animateScrollTo(reportPanel, 24, 1100);
          }, 260);
        } else if (event.type === "error") {
          finalReportText = `Error: ${event.message}`;
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

  if (downloadFeedButton) {
    downloadFeedButton.addEventListener("click", downloadFeed);
  }

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
    currentObjective = objective;
    appendUpdate(`**Mission received:** ${objective}`, null);
    setRunningState(true);
    animateScrollTo(resultsPanel, 18, 1000);

    try {
      await streamRun(objective);
    } catch (error) {
      finalReportText = `Error: ${error.message || "Something went wrong while running the agent."}`;
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

import asyncio
import base64
import json
import os
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from schema import AgentOutput

# --- Optional SDK imports ---
try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
_RETAINED_BROWSER_SESSION = None
_RATE_LIMIT_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_NO_EVALUATE_DEFAULT = object()

_MODEL_LIMITS = {
    # Anthropic
    "claude-haiku-4-5-20251001": {
        "context_window": 200_000,
        "max_output_tokens": 1024,
        "default_rpm_limit": 50,
    },
    # Google
    "gemini-2.5-flash": {
        "context_window": 1_000_000,
        "max_output_tokens": 1024,
        "default_rpm_limit": 15,
    },
    "gemini-2.5-flash-lite": {
        "context_window": 1_000_000,
        "max_output_tokens": 1024,
        "default_rpm_limit": 15,
    },
}


def _detect_provider():
    """Auto-detect AI provider from environment variables."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GOOGLE_API_KEY"):
        return "google"
    raise RuntimeError(
        "No API key found. Set ANTHROPIC_API_KEY or GOOGLE_API_KEY in your .env file."
    )


def _get_model_name():
    provider = _detect_provider()
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    return os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")


def _build_client():
    provider = _detect_provider()
    if provider == "anthropic":
        if anthropic is None:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
        return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    else:
        if genai is None:
            raise RuntimeError("google-genai package not installed. Run: pip install google-genai")
        return genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


def _is_transient_navigation_error(exc):
    error_text = str(exc).lower()
    transient_markers = [
        "execution context was destroyed",
        "most likely because of a navigation",
        "cannot find context with specified id",
        "target closed",
        "frame was detached",
    ]
    return any(marker in error_text for marker in transient_markers)


async def _evaluate_page_safely(page, script, default=_NO_EVALUATE_DEFAULT, attempts=3):
    for attempt in range(attempts):
        try:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:
                pass
            return await page.evaluate(script)
        except Exception as exc:
            if not _is_transient_navigation_error(exc) or attempt >= attempts - 1:
                if default is not _NO_EVALUATE_DEFAULT:
                    return default
                raise
            await asyncio.sleep(0.35 * (attempt + 1))

    return default


async def get_dom_snapshot(page, deep_read=False):
    """Capture a richer interactive page snapshot for the model."""
    if deep_read:
        selectors = "button, a, input, select, option, [role=\"button\"], [role=\"link\"], [role=\"option\"], [role=\"menuitem\"], [role=\"heading\"], textarea, h1, h2, h3, p, li, label, [contenteditable=\"true\"]"
        max_chars = 1000
        max_elements = 180
        include_layout = "false"
    else:
        selectors = "button, a, input, select, li, [role=\"button\"], [role=\"link\"], [role=\"option\"], [role=\"menuitem\"], [role=\"heading\"], textarea, h1, h2, h3, label, [contenteditable=\"true\"]"
        max_chars = 120
        max_elements = 120
        include_layout = "false"

    js_code = f"""
    () => {{
        let oldElements = document.querySelectorAll('[data-agent-id]');
        for (let oldEl of oldElements) {{
            oldEl.removeAttribute('data-agent-id');
        }}

        let elements = document.querySelectorAll('{selectors}');
        let simplifiedDOM = [];
        let maxElements = {max_elements};
        let count = 0;

        for (let el of elements) {{
            if (count >= maxElements) break;

            let rect = el.getBoundingClientRect();
            let style = window.getComputedStyle(el);
            
            // 1. Basic visibility & opacity checks
            if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity) <= 0.05) {{
                continue;
            }}
            
            // 2. Strict Viewport Check: Ignore elements completely off-screen
            let isOffScreen = (
                rect.bottom < 0 || 
                rect.top > window.innerHeight || 
                rect.right < 0 || 
                rect.left > window.innerWidth
            );
            
            if (isOffScreen) {{
                continue;
            }}

            let id = count + 1;
            el.setAttribute('data-agent-id', id);

            let rawText =
                el.innerText ||
                el.value ||
                el.getAttribute('aria-label') ||
                el.getAttribute('placeholder') ||
                el.getAttribute('title') ||
                'No Text';
            let cleanText = rawText.substring(0, {max_chars}).replace(/\\n/g, ' ');
            let role = el.getAttribute('role') || '';
            let ariaLabel = el.getAttribute('aria-label') || '';
            let placeholder = el.getAttribute('placeholder') || '';
            let rawHref = el.getAttribute('href') || '';
            let href = '';
            if (rawHref) {{
                try {{
                    href = new URL(rawHref, window.location.href).toString();
                }} catch (error) {{
                    href = rawHref;
                }}
            }}
            let name = el.getAttribute('name') || '';
            let title = el.getAttribute('title') || '';
            let value = (el.value || '').toString().substring(0, 120);
            let formId = '';
            let formName = '';
            if (el.form) {{
                formId = el.form.getAttribute('id') || '';
                formName = el.form.getAttribute('name') || '';
            }}
            let searchCandidate = /search|find|products|brands|youtube|amazon|flipkart|myntra|google/i.test(
                [cleanText, ariaLabel, placeholder, name, title].join(' ')
            );

            let item = {{ id: id, tag: el.tagName }};
            if (cleanText && cleanText !== 'No Text') item.text = cleanText;
            if (el.type && el.type !== 'submit' && el.type !== 'button') item.type = el.type;
            if (role) item.role = role;
            if (ariaLabel) item.aria_label = ariaLabel;
            if (placeholder) item.placeholder = placeholder;
            if (name) item.name = name;
            if (title) item.title = title;

            if (value) item.value = value;
            if (el.required) item.required = true;
            if (el.disabled) item.disabled = true;
            if (el.checked) item.checked = true;
            if (searchCandidate) item.search = true;
            if ({include_layout}) {{
                item.x = Math.round(rect.x);
                item.y = Math.round(rect.y);
                item.width = Math.round(rect.width);
                item.height = Math.round(rect.height);
            }}
            simplifiedDOM.push(item);
            count++;
        }}
        return simplifiedDOM;
    }}
    """
    return await _evaluate_page_safely(page, js_code, default=[])



async def _emit_final_screenshot(page, ui_callback):
    if not ui_callback or page.is_closed():
        return

    try:
        final_image = await page.screenshot(full_page=False)
    except Exception:
        return

    ui_callback("**Final screen captured.**", image_bytes=final_image)


async def _ensure_active_page(context, current_page):
    if current_page and not current_page.is_closed():
        return current_page

    open_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    if open_pages:
        return open_pages[-1]

    new_page = await context.new_page()
    await new_page.goto("about:blank")
    return new_page


async def _adopt_newest_page_if_needed(context, current_page, previous_page_count):
    open_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    if not open_pages:
        return current_page

    if current_page is None or current_page.is_closed() or len(open_pages) > previous_page_count:
        return open_pages[-1]

    return current_page


async def _adopt_new_tab_after_action(context, current_page, previous_page_count, wait_seconds=0.8):
    adopted_page = current_page
    deadline = asyncio.get_running_loop().time() + wait_seconds

    while asyncio.get_running_loop().time() < deadline:
        open_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
        if open_pages:
            newest_page = open_pages[-1]
            if (
                current_page is None
                or current_page.is_closed()
                or len(open_pages) > previous_page_count
                or newest_page is not current_page
            ):
                adopted_page = newest_page
                break
        await asyncio.sleep(0.05)

    adopted_page = await _adopt_newest_page_if_needed(context, adopted_page, previous_page_count)
    if adopted_page and not adopted_page.is_closed():
        try:
            await adopted_page.bring_to_front()
        except Exception:
            pass
    return adopted_page


async def _settle_page(page, delay_seconds=0.3):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=2000)
    except Exception:
        pass
    await asyncio.sleep(delay_seconds)


async def _click_locator_safely(locator):
    try:
        await locator.click(timeout=2500)
        return True
    except Exception:
        pass

    try:
        handle = await locator.element_handle(timeout=1200)
        if handle is None:
            return False
        await handle.evaluate("(node) => node.click()")
        return True
    except Exception:
        return False


async def _release_retained_browser_session():
    global _RETAINED_BROWSER_SESSION

    session = _RETAINED_BROWSER_SESSION
    if not session:
        return

    _RETAINED_BROWSER_SESSION = None
    context = session.get("context")
    playwright = session.get("playwright")

    if context is not None:
        try:
            await context.close()
        except Exception:
            pass

    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


def _get_model_limits(model_name):
    normalized_model = _normalize_text(model_name)
    return _MODEL_LIMITS.get(normalized_model, {})


def _get_request_interval_seconds(model_name=None):
    disable_local_limit = os.getenv("DISABLE_LOCAL_RATE_LIMIT", "").strip().lower()
    if disable_local_limit in {"1", "true", "yes"}:
        return 0.0

    rpm_text = os.getenv("RPM_LIMIT", "").strip()
    if rpm_text:
        try:
            rpm_value = float(rpm_text)
            if rpm_value > 0:
                return 60.0 / rpm_value
        except ValueError:
            pass

    model_rpm_limit = _get_model_limits(model_name).get("default_rpm_limit")
    if model_rpm_limit:
        return 60.0 / model_rpm_limit

    return 1.2


def _get_max_output_tokens(model_name):
    limits = _get_model_limits(model_name)
    output_limit = int(limits.get("max_output_tokens") or 8192)
    token_text = os.getenv("MAX_OUTPUT_TOKENS", "").strip()
    if not token_text:
        return output_limit

    try:
        requested_tokens = int(token_text)
    except ValueError:
        return output_limit

    return min(max(requested_tokens, 1), output_limit)


def _build_agent_tool():
    """Build the Anthropic tool definition from the AgentOutput Pydantic schema."""
    return {
        "name": "browser_action",
        "description": "Execute browser actions. You MUST call this tool.",
        "input_schema": AgentOutput.model_json_schema(),
    }


def _get_agent_max_steps():
    max_steps_text = os.getenv("AGENT_MAX_STEPS", "60").strip()
    try:
        max_steps = int(max_steps_text)
    except ValueError:
        return 60

    return min(max(max_steps, 1), 100)


async def _respect_rate_limit(model_name=None):
    global _NEXT_REQUEST_AT

    interval_seconds = _get_request_interval_seconds(model_name)
    wait_seconds = 0.0

    with _RATE_LIMIT_LOCK:
        now = time.monotonic()
        wait_seconds = max(0.0, _NEXT_REQUEST_AT - now)
        scheduled_start = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = scheduled_start + interval_seconds

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def _call_llm(client, provider, model_name, system_prompt, context_text, screenshot_bytes=None):
    """Unified LLM call. Returns (ai_decision_dict, prompt_tokens, response_tokens, raw_text)."""
    await _respect_rate_limit(model_name=model_name)
    max_tokens = _get_max_output_tokens(model_name)

    if provider == "anthropic":
        return await _call_anthropic_impl(client, model_name, system_prompt, context_text, screenshot_bytes, max_tokens)
    else:
        return await _call_google_impl(client, model_name, system_prompt, context_text, screenshot_bytes, max_tokens)


async def _call_anthropic_impl(client, model_name, system_prompt, context_text, screenshot_bytes, max_tokens):
    user_content = [{"type": "text", "text": context_text}]
    if screenshot_bytes:
        user_content.append({"type": "text", "text": "\n\nVISION: Screenshot attached. Analyze it."})
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(screenshot_bytes).decode("ascii")},
        })

    tool_def = _build_agent_tool()

    def _sync():
        return client.messages.create(
            model=model_name, max_tokens=max_tokens, temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=[tool_def],
            tool_choice={"type": "tool", "name": "browser_action"},
        )

    response = await asyncio.to_thread(_sync)

    prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
    response_tokens = getattr(response.usage, "output_tokens", 0) or 0

    tool_block = next((b for b in response.content if b.type == "tool_use" and b.name == "browser_action"), None)
    if tool_block is None:
        raise ValueError("No browser_action tool call in response")

    ai_decision = tool_block.input
    return ai_decision, prompt_tokens, response_tokens, json.dumps(ai_decision, indent=2)


async def _call_google_impl(client, model_name, system_prompt, context_text, screenshot_bytes, max_tokens):
    prompt = system_prompt + "\n\n" + context_text
    contents = [prompt]
    if screenshot_bytes:
        contents[0] += "\n\nVISION: Screenshot attached. Analyze it."
        contents.append(genai_types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"))

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=AgentOutput,
        temperature=0.0,
        max_output_tokens=max_tokens,
    )

    def _sync():
        return client.models.generate_content(model=model_name, contents=contents, config=config)

    response = await asyncio.to_thread(_sync)

    prompt_tokens = 0
    response_tokens = 0
    usage = getattr(response, "usage_metadata", None)
    if usage:
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        response_tokens = getattr(usage, "candidates_token_count", 0) or 0

    raw_text = response.text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:-3].strip()
    elif raw_text.startswith("```"):
        raw_text = raw_text[3:-3].strip()

    ai_decision = json.loads(raw_text)
    return ai_decision, prompt_tokens, response_tokens, raw_text


def _repeat_guard_message(action_type):
    if action_type == "click":
        return (
            "I got stuck trying to open the same item repeatedly, so I stopped before making a wrong move. "
            "The product may be opening in a blocked tab or the page may not be responding correctly."
        )
    if action_type == "type":
        return (
            "I kept repeating the same search without finding a stable next step, so I stopped instead of looping. "
            "The exact product may not be available under that name."
        )
    if action_type == "goto":
        return "I kept trying to navigate to the same place without progress, so I stopped to avoid an endless loop."
    return f"I repeated the same `{action_type}` action too many times without progress, so I stopped safely."


def _normalize_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _infer_direct_site_url(user_objective):
    objective = _normalize_text(user_objective)
    direct_sites = [
        (["amazon india", "amazon.in"], "https://www.amazon.in"),
        (["amazon"], "https://www.amazon.in"),
        (["flipkart"], "https://www.flipkart.com"),
        (["myntra"], "https://www.myntra.com"),
        (["blinkit"], "https://blinkit.com"),
        (["zepto", "zeptonow"], "https://www.zeptonow.com"),
        (["youtube", "yt"], "https://www.youtube.com"),
        (["wikipedia", "wiki"], "https://www.wikipedia.org"),
        (["python.org", "python website", "python homepage"], "https://www.python.org"),
        (["github"], "https://github.com"),
        (["streamlit"], "https://streamlit.io"),
    ]
    for keywords, url in direct_sites:
        if any(keyword in objective for keyword in keywords):
            return url
    return None


def _is_google_search_url(url):
    normalized = _normalize_text(url)
    return "google.com/search" in normalized or "google.co.in/search" in normalized


def _should_return_structured_results(user_objective):
    objective = _normalize_text(user_objective)
    result_keywords = [
        "price",
        "prices",
        "cost",
        "product",
        "products",
        "result",
        "results",
        "option",
        "options",
        "best",
        "under rs",
        "under inr",
        "under rupees",
        "tell me the first three",
        "first three",
        "shortlist",
        "compare",
    ]
    return any(keyword in objective for keyword in result_keywords)


def _normalize_report_lines(report_text):
    lines = []
    for raw_line in str(report_text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", raw_line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _finalize_report(report_text, user_objective):
    report_lines = _normalize_report_lines(report_text)
    report = "\n".join(report_lines)
    if not report:
        return f"Completed the task: {user_objective}"

    replacements = [
        ("I have successfully ", ""),
        ("I successfully ", ""),
        ("I have ", ""),
        ("I am ", ""),
        ("The user asked me to ", ""),
        ("The user's objective was to ", ""),
    ]
    for old, new in replacements:
        if report.lower().startswith(old.lower()):
            report = new + report[len(old):]

    if _should_return_structured_results(user_objective):
        structured_lines = _normalize_report_lines(report)
        if structured_lines:
            return "\n".join(structured_lines[:3])

    flattened_report = " ".join(report.split())
    if len(flattened_report) > 260:
        sentences = [
            segment.strip()
            for segment in report.replace("!", ".").replace("?", ".").split(".")
            if segment.strip()
        ]
        if sentences:
            flattened_report = sentences[0]

    return flattened_report[:260].rstrip(". ") + "."


def _fingerprint_dom(dom_data):
    tokens = []
    for element in dom_data[:25]:
        tokens.append(
            "|".join(
                [
                    str(element.get("tag", "")),
                    str(element.get("role", "")),
                    _normalize_text(element.get("text", ""))[:60],
                    _normalize_text(element.get("aria_label", ""))[:40],
                    _normalize_text(element.get("placeholder", ""))[:40],
                ]
            )
        )
    return " || ".join(tokens)


def _get_element_details(dom_data, element_id):
    for element in dom_data:
        if element["id"] == element_id:
            return element
    return None


def _is_search_like_element(element):
    if not element:
        return False

    haystack = " ".join(
        [
            str(element.get("tag", "")),
            str(element.get("text", "")),
            str(element.get("type", "")),
            str(element.get("role", "")),
            str(element.get("aria_label", "")),
            str(element.get("placeholder", "")),
            str(element.get("name", "")),
            str(element.get("title", "")),
        ]
    ).lower()
    search_markers = ["search", "find", "products", "brands", "amazon", "flipkart", "myntra", "youtube", "google"]
    return bool(element.get("search_candidate")) or any(marker in haystack for marker in search_markers)


def _is_media_objective(user_objective):
    objective = _normalize_text(user_objective)
    media_terms = ["play", "watch", "listen", "video", "song", "music", "youtube", "yt"]
    return any(term in objective for term in media_terms)



async def _wait_for_media_completion(page, ui_callback=None):
    if "youtube.com/watch" not in page.url and "youtu.be/" not in page.url:
        return

    try:
        media_info = await page.evaluate(
            """
            () => {
                const video = document.querySelector('video');
                if (!video) {
                    return { present: false };
                }
                return {
                    present: true,
                    paused: video.paused,
                    ended: video.ended,
                    currentTime: Number(video.currentTime || 0),
                    duration: Number(video.duration || 0),
                };
            }
            """
        )
    except Exception:
        return

    if not media_info or not media_info.get("present"):
        return

    try:
        await page.evaluate(
            """
            () => {
                const video = document.querySelector('video');
                if (video && video.paused && !video.ended) {
                    video.play().catch(() => {});
                }
            }
            """
        )
    except Exception:
        pass

    duration = media_info.get("duration") or 0
    timeout_seconds = min(max(duration + 30, 120), 7200) if duration > 0 else 1800
    elapsed = 0
    announced_wait = False

    while elapsed < timeout_seconds:
        try:
            status = await page.evaluate(
                """
                () => {
                    const video = document.querySelector('video');
                    if (!video) {
                        return { present: false };
                    }
                    return {
                        present: true,
                        paused: video.paused,
                        ended: video.ended,
                        currentTime: Number(video.currentTime || 0),
                        duration: Number(video.duration || 0),
                    };
                }
                """
            )
        except Exception:
            break

        if not status or not status.get("present"):
            break

        if status.get("ended"):
            if ui_callback:
                ui_callback("**Playback complete.**")
            return

        if ui_callback and not announced_wait:
            duration_text = ""
            if status.get("duration"):
                duration_text = f" Duration: about {int(status['duration'] // 60)} min {int(status['duration'] % 60)} sec."
            ui_callback(f"**Playback detected. Waiting for the video to finish before wrapping up.**{duration_text}")
            announced_wait = True

        try:
            await page.evaluate(
                """
                () => {
                    const video = document.querySelector('video');
                    if (video && video.paused && !video.ended) {
                        video.play().catch(() => {});
                    }
                }
                """
            )
        except Exception:
            pass

        await asyncio.sleep(5)
        elapsed += 5

    if ui_callback:
        ui_callback("**Playback wait timed out, so I am ending the run while leaving the current result intact.**")


async def run_agent(user_objective, ui_callback=None, keep_browser_open=False):
    provider = _detect_provider()
    client = _build_client()
    model_name = _get_model_name()
    global _RETAINED_BROWSER_SESSION

    await _release_retained_browser_session()

    p = await async_playwright().start()
    context = None
    retain_browser_session = False
    try:
        user_data_dir = os.path.abspath("agent_profile")

        print("Booting Persistent Stealth Browser...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled"
            ],
            ignore_default_args=["--enable-automation", "--disable-extensions"],
        )

        page = context.pages[0]
        await page.goto("about:blank")

        step_count = 0
        max_steps = _get_agent_max_steps()
        action_history = []
        action_signatures = []
        final_report = "Task failed or timed out."

        needs_deep_read = False
        needs_vision = False
        last_typed_text = None
        last_typed_field = None
        last_typed_url = None
        agent_memory = "No important memory yet."
        last_next_goal = "Open the right website and identify the first useful interactive step."
        last_action_result = "No previous action yet."
        last_observation_key = None
        repeated_observation_count = 0
        stall_recovery_attempts = 0

        while step_count < max_steps:
            page = await _ensure_active_page(context, page)
            dom_data = await get_dom_snapshot(page, deep_read=needs_deep_read)
            needs_deep_read = False

            current_url = page.url
            try:
                page_title = await page.title()
            except Exception:
                page_title = "Unknown"
            observation_key = f"{current_url}::{_fingerprint_dom(dom_data)}"
            if observation_key == last_observation_key:
                repeated_observation_count += 1
            else:
                repeated_observation_count = 0
                stall_recovery_attempts = 0
            last_observation_key = observation_key

            system_prompt = """\
You are a fast autonomous web agent. Achieve the user's objective via browser actions.

# BREVITY RULES
- All fields must be minimal. No essays, no restating the objective.
- evaluation_previous_goal: "Success/Failed/Unknown" + max 5 words.
- memory: bullet-style notes only.
- next_goal: ONE short action phrase.

# MULTI-ACTION
- Return 1-5 actions in the "actions" list. They execute in sequence.
- Combine related steps: e.g. [type search, press Enter] or [click Add to Cart, scroll down].
- Do NOT combine actions that depend on page changes (e.g. goto + click).

# RULES
1. NAVIGATION: Use "goto" to open a URL. Use "scroll" to move up or down.
2. INTERACTION: Use "click" and "type" with element IDs. Use "press" for keys (e.g., "Enter", "Escape").
3. READING: If text is hidden in long paragraphs, use "read".
4. ANTI-LOOP RULE: Do not "scroll" or "click" the exact same ID more than twice in a row.
5. THE POPUP PROTOCOL: If you click a button and nothing changes, a popup is likely blocking you. Use "press" with "Escape" to kill it.
6. SUCCESS RECOGNITION (NO VERIFY): When you click "Add to Cart", ASSUME IT WORKED. DO NOT try to verify it. DO NOT use "wait", and DO NOT click the cart icon. Immediately move to the next item or use "finish".
7. TYPING: After "type", do NOT assume Enter was pressed. Use a separate "press" action if needed.
8. HALLUCINATION PREVENTION: Never assume a search or action worked until you physically see the new results in the JSON. If the DOM hasn't changed, your previous action failed (or you forgot to press Enter). Try again instead of hallucinating success.
9. STALL RECOVERY: If observation repeat count > 0, do NOT repeat the same weak action. Try "press" Enter, click a visible button, use Escape, or "read".
10. DIRECT NAVIGATION: Go directly to known sites (Amazon, Blinkit, YouTube, GitHub, etc.) instead of Google.
11. SHOPPING EFFICIENCY: On e-commerce sites, if you are on a product page and cannot see the "Add to Cart" button in the JSON, scroll or click carefully to find it.
12. FINISH SUMMARY: Use "finish" with a 1-2 sentence user-facing summary.
13. STRUCTURED RESULT: For product/price tasks, format finish.reason nicely: `1. Product Name - Price`.
14. RESEARCH PROTOCOL: For comparisons (e.g., "which is better"), DO NOT research specs or reviews on the web. Pick the winner instantly using your internal knowledge. THEN, physically navigate to the store and add it to the cart. ONLY use "finish" AFTER the item is in the cart, and include your explanation in the finish reason.
15. FLIGHT PROTOCOL: If asked to search for flights, go directly to google.com/flights. Use standard keyboard actions to set date inputs if the calendar UI is too complex."""

            context_text = f"""\
URL: {current_url}
Title: {page_title}
Last Goal: {last_next_goal}
Last Result: {last_action_result}
Repeat Count: {repeated_observation_count}
Memory: {agent_memory}

Past Actions (DO NOT REPEAT):
{json.dumps(action_history[:1] + action_history[-4:] if len(action_history) > 5 else action_history)}

Elements:
{json.dumps(dom_data)}

Goal: {user_objective}"""

            screenshot_bytes = None
            if needs_vision:
                print("Snapping photo for AI...")
                screenshot_bytes = await page.screenshot()
                needs_vision = False

            try:
                ai_decision, prompt_tokens, response_tokens, raw_text = await _call_llm(
                    client=client, provider=provider, model_name=model_name,
                    system_prompt=system_prompt, context_text=context_text,
                    screenshot_bytes=screenshot_bytes,
                )
            except Exception as exc:
                print(f"LLM Call Error: {exc}")
                step_count += 1
                await asyncio.sleep(5)
                continue

            prompt_text_for_log = context_text

            try:
                current_state = ai_decision.get("current_state", {}) or {}
                actions_list = ai_decision.get("actions", [])
                if not actions_list:
                    # Backward compat: check for single "command"
                    cmd = ai_decision.get("command")
                    if cmd:
                        actions_list = [cmd]
                    else:
                        raise ValueError("No actions in response")

                thought = current_state.get("next_goal") or "Thinking..."
                evaluation_previous_goal = current_state.get("evaluation_previous_goal", "")
                agent_memory = current_state.get("memory") or agent_memory
                last_next_goal = current_state.get("next_goal") or last_next_goal

                action_names = ", ".join(a.get("action", "?") for a in actions_list)
                if ui_callback:
                    ui_callback(
                        f"**Step {step_count + 1} ({len(actions_list)} actions) — Tokens:** prompt {prompt_tokens:,} · response {response_tokens:,}",
                        token_info={
                            "step": step_count + 1,
                            "prompt_tokens": prompt_tokens,
                            "response_tokens": response_tokens,
                            "prompt_text": prompt_text_for_log,
                            "response_text": raw_text,
                        },
                    )
                    if screenshot_bytes:
                        ui_callback(f"**Goal:** {thought}", image_bytes=screenshot_bytes)
                    else:
                        ui_callback(f"**Goal:** {thought}")
                    if evaluation_previous_goal:
                        ui_callback(f"**Assessment:** {evaluation_previous_goal}")
                    ui_callback(f"**Actions:** `{action_names}`")

            except Exception as exc:
                print(f"Response Parse Error: {exc}")
                step_count += 1
                await asyncio.sleep(5)
                continue

            # --- Execute each action in the multi-action list ---
            should_break = False
            for action_cmd in actions_list:
                action_type = action_cmd.get("action", "")

                if action_type == "finish":
                    if _is_media_objective(user_objective):
                        await _wait_for_media_completion(page, ui_callback=ui_callback)
                    final_report = _finalize_report(action_cmd.get("reason", ""), user_objective)
                    should_break = True
                    break

                if action_type == "read":
                    needs_deep_read = True
                    last_action_result = "Deep text scan requested."
                    action_history.append(f"Deep read (Step {step_count})")
                    break

                if action_type == "wait":
                    last_action_result = "Waited for page to update and grabbed fresh DOM."
                    action_history.append(f"Wait (Step {step_count})")
                    await asyncio.sleep(2)
                    break

                if action_type == "evaluate_js":
                    js_code = action_cmd.get("script", "")
                    try:
                        res = await page.evaluate(js_code)
                        last_action_result = f"JS Executed successfully. Returned: {res}"
                    except Exception as e:
                        last_action_result = f"JS Error: {str(e)}"
                    action_history.append(f"evaluate_js (Step {step_count})")
                    break

                if action_type == "goto":
                    target_url = action_cmd.get("url", "")
                    if not target_url.startswith("http"):
                        target_url = "https://" + target_url
                    direct_site_url = _infer_direct_site_url(user_objective)
                    if direct_site_url and _is_google_search_url(target_url):
                        target_url = direct_site_url
                    try:
                        await page.goto(target_url)
                        await _settle_page(page, delay_seconds=0.5)
                    except Exception as exc:
                        error_text = str(exc)
                        if "ERR_NAME_NOT_RESOLVED" in error_text:
                            final_report = f"Could not open `{target_url}` — domain not resolved."
                            should_break = True
                            break
                        if "ERR_HTTP_RESPONSE_CODE_FAILURE" in error_text:
                            final_report = f"Site `{target_url}` blocked navigation."
                            should_break = True
                            break
                        raise
                    last_action_result = f"Navigated to {target_url}."
                    action_history.append(f"Goto {target_url} (Step {step_count})")

                elif action_type == "scroll":
                    direction = action_cmd.get("direction", "down")
                    viewport = page.viewport_size
                    if viewport:
                        await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
                    delta = 800 if direction == "down" else -800
                    await page.mouse.wheel(0, delta)
                    last_action_result = f"Scrolled {direction}."
                    action_history.append(f"Scrolled {direction} (Step {step_count})")

                elif action_type == "press":
                    key = action_cmd.get("key", "Enter")
                    await page.keyboard.press(key)
                    last_action_result = f"Pressed {key}."
                    if _normalize_text(key) == "enter":
                        last_typed_text = None
                        last_typed_field = None
                        last_typed_url = None
                    action_history.append(f"Pressed '{key}' (Step {step_count})")

                elif action_type == "type":
                    element_id = action_cmd.get("element_id", 0)
                    text_to_type = action_cmd.get("text", "")
                    target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                    element_label = next((el.get("text", "")[:40] for el in dom_data if el["id"] == element_id), "field")

                    previous_page_count = len([c for c in context.pages if not c.is_closed()])
                    clicked = await _click_locator_safely(target_locator)
                    if not clicked:
                        last_action_result = f"Could not focus {element_label}."
                        needs_vision = True
                        break

                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(text_to_type, delay=2)

                    page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                    await _settle_page(page, delay_seconds=0.3)
                    last_typed_text = text_to_type
                    last_typed_field = element_label
                    last_typed_url = current_url
                    last_action_result = f"Typed '{text_to_type}' into '{element_label}'."
                    action_history.append(f"Typed '{text_to_type}' into '{element_label}' (Step {step_count})")

                elif action_type == "click":
                    element_id = action_cmd.get("element_id", 0)
                    target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                    element_label = next((el.get("text", "")[:40] for el in dom_data if el["id"] == element_id), "element")

                    previous_page_count = len([c for c in context.pages if not c.is_closed()])
                    clicked = await _click_locator_safely(target_locator)
                    if not clicked:
                        last_action_result = f"Could not click {element_label}."
                        needs_vision = True
                        break
                    page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                    await _settle_page(page, delay_seconds=0.3)
                    last_action_result = f"Clicked '{element_label}'."
                    action_history.append(f"Clicked '{element_label}' (Step {step_count})")

                # Brief pause between multi-actions
                await asyncio.sleep(0.05)

            step_count += 1
            if should_break:
                break

        if step_count >= max_steps and final_report == "Task failed or timed out.":
            final_report = (
                f"The agent reached the configured {max_steps}-step limit before it could confidently finish. "
                "Try a more specific task, complete any login or verification manually, or increase AGENT_MAX_STEPS up to 100."
            )

        await _emit_final_screenshot(page, ui_callback)
        await asyncio.sleep(2)

        if keep_browser_open:
            retain_browser_session = True
            _RETAINED_BROWSER_SESSION = {"playwright": p, "context": context}
            if ui_callback:
                ui_callback("**Browser left open after completion.**")
        return final_report
    finally:
        if not retain_browser_session:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await p.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_agent("Test agent run"))

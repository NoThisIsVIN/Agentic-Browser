import asyncio
import json
import os
import re
import threading
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

from schema import AgentOutput

load_dotenv()

_RETAINED_BROWSER_SESSION = None
_GEMINI_RATE_LIMIT_LOCK = threading.Lock()
_NEXT_GEMINI_REQUEST_AT = 0.0


async def get_dom_snapshot(page, deep_read=False):
    """Capture a richer interactive page snapshot for the model."""
    if deep_read:
        selectors = "button, a, input, select, option, [role=\"button\"], [role=\"link\"], textarea, h1, h2, h3, p, li, label, [contenteditable=\"true\"]"
        max_chars = 1000
        max_elements = 180
    else:
        selectors = "button, a, input, select, [role=\"button\"], [role=\"link\"], textarea, h1, h2, h3, label, [contenteditable=\"true\"]"
        max_chars = 160
        max_elements = 110

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
            if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden' || style.display === 'none') {{
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
            let searchCandidate = /search|find|products|brands|youtube|amazon|flipkart|myntra|google/i.test(
                [cleanText, ariaLabel, placeholder, name, title].join(' ')
            );

            simplifiedDOM.push({{
                id: id,
                tag: el.tagName,
                text: cleanText,
                type: el.type || 'N/A',
                role: role || 'N/A',
                aria_label: ariaLabel,
                placeholder: placeholder,
                name: name,
                title: title,
                href: href,
                value: value,
                search_candidate: searchCandidate
            }});
            count++;
        }}
        return simplifiedDOM;
    }}
    """
    return await page.evaluate(js_code)


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


async def _adopt_new_tab_after_action(context, current_page, previous_page_count, wait_seconds=2.5):
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
        await asyncio.sleep(0.1)

    adopted_page = await _adopt_newest_page_if_needed(context, adopted_page, previous_page_count)
    if adopted_page and not adopted_page.is_closed():
        try:
            await adopted_page.bring_to_front()
        except Exception:
            pass
    return adopted_page


async def _settle_page(page, delay_seconds=2):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
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


def _get_gemini_request_interval_seconds():
    rpm_text = os.getenv("GEMINI_RPM_LIMIT", "").strip()
    if rpm_text:
        try:
            rpm_value = float(rpm_text)
            if rpm_value > 0:
                return 60.0 / rpm_value
        except ValueError:
            pass

    interval_text = os.getenv("GEMINI_REQUEST_INTERVAL_SECONDS", "").strip()
    if interval_text:
        try:
            interval_value = float(interval_text)
            if interval_value > 0:
                return interval_value
        except ValueError:
            pass

    return 15.0


async def _respect_gemini_rate_limit(ui_callback=None):
    global _NEXT_GEMINI_REQUEST_AT

    interval_seconds = _get_gemini_request_interval_seconds()
    wait_seconds = 0.0

    with _GEMINI_RATE_LIMIT_LOCK:
        now = time.monotonic()
        wait_seconds = max(0.0, _NEXT_GEMINI_REQUEST_AT - now)
        scheduled_start = max(now, _NEXT_GEMINI_REQUEST_AT)
        _NEXT_GEMINI_REQUEST_AT = scheduled_start + interval_seconds

    if wait_seconds > 0:
        if ui_callback:
            ui_callback(
                f"**Rate limit:** Waiting {wait_seconds:.1f}s before the next Gemini request to respect the configured RPM."
            )
        await asyncio.sleep(wait_seconds)


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
    client = genai.Client()
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
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
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        page = context.pages[0]
        await page.goto("about:blank")

        step_count = 0
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

        while step_count < 20:
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

            prompt = f"""
            # SYSTEM INSTRUCTIONS
            You are an elite autonomous web agent. Your job is to achieve the user's objective by navigating the web, extracting information, and taking actions.

            # STRATEGY
            Before acting, mentally break the goal into ordered sub-steps.
            After each action, check whether the page confirms success before continuing.
            If the page looks the same after an action, assume it failed and try a different approach.
            Keep a short running memory and update it honestly every step.

            # YOUR TOOLKIT RULES
            1. Navigation: Use "goto" to open a URL. Use "scroll" to move up or down.
            2. Interaction: Use "click" and "type" using the provided element IDs from the JSON. Use "press" for keyboard keys (e.g. "Enter", "Escape", "Tab").
            3. Reading: The JSON provides basic text. If text is hidden in paragraphs, use "read".
            4. VISION (CRITICAL): If you need to solve a visual problem (like a math formula, an image, or a complex diagram) that is NOT readable in the JSON, use the "look" tool to take a screenshot.
            5. ANTI-LOOP RULE: Do not use the "scroll" action, or "click" the exact same ID, more than twice in a row.
            6. THE POPUP PROTOCOL: If you click a button and the page does not change, a popup or modal is likely blocking your screen. You MUST either use the "press" tool with the "Escape" key to kill the popup, or use the "look" tool to see what is blocking you.
            7. SUCCESS RECOGNITION: E-commerce sites often redirect you or show "Subtotal (1 item)" after adding an item. If you see evidence that your goal was achieved, DO NOT second-guess yourself or restart. Immediately use the "finish" action.
            8. TYPING: After using "type", do NOT assume Enter was pressed. If you need to submit a form or trigger a search, issue a separate "press" action with key "Enter".
            9. SEARCH RE-ENTRY RULE: If the same search text was already typed into a search field, do NOT type it again immediately. First confirm the current page state using the DOM or screenshot, then continue with the next step.
            10. STALL RECOVERY RULE: If the page observation repeat count is above 0, do NOT repeat the same weak action. Prefer pressing Enter after a search, clicking a visible submit/search button, using Escape for popups, or using read/look to regain context.
            11. RESPONSE FORMAT: Fill current_state.evaluation_previous_goal, current_state.memory, and current_state.next_goal carefully. The next_goal should be one immediate step, not the whole plan.
            12. DIRECT NAVIGATION RULE: If the user clearly names a known website like Amazon, Blinkit, Flipkart, Myntra, Zepto, YouTube, Wikipedia, Python, or GitHub, go directly to that site instead of using Google as an intermediate step.
            13. FINISH SUMMARY RULE: When you use finish, the reason must be a short user-facing summary in one or two concise sentences.
            14. STRUCTURED RESULT RULE: For shopping, product, price-check, shortlist, and comparison tasks, finish.reason must be neatly formatted in Markdown with up to 3 visible items using this style when possible: `1. Product Name - Price: Rs. 499 - [Link](https://example.com)`. Only include products, prices, and links that are actually visible on the current page or screenshot.

            # CONTEXT
            Current URL: {current_url}
            Current Page Title: {page_title}
            Last Goal: {last_next_goal}
            Last Action Result: {last_action_result}
            Observation Repeat Count: {repeated_observation_count}
            Last Entered Text: {last_typed_text or "None"}
            Last Entered Field: {last_typed_field or "None"}
            Last Entered URL: {last_typed_url or "None"}
            Running Memory: {agent_memory}

            Past Actions Taken (DO NOT REPEAT THESE):
            {json.dumps(action_history, indent=2)}

            Current Screen Elements (JSON):
            {json.dumps(dom_data, indent=2)}

            # USER OBJECTIVE
            Goal: {user_objective}

            What is your next logical step? Output strictly matching the Pydantic schema.
            """

            contents_payload = [prompt]
            screenshot_bytes = None

            if needs_vision:
                print("Snapping photo for AI...")
                screenshot_bytes = await page.screenshot()
                contents_payload[0] = (
                    prompt
                    + "\n\nVISION MODULE ACTIVE: A screenshot of the current page is attached. "
                    + "Analyze the image to find the unextractable data."
                )
                contents_payload.append(types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"))
                needs_vision = False

            await _respect_gemini_rate_limit(ui_callback=ui_callback)
            response = client.models.generate_content(
                model=model_name,
                contents=contents_payload,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AgentOutput,
                    temperature=0.0,
                ),
            )

            try:
                raw_text = response.text.strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text[7:-3].strip()
                elif raw_text.startswith("```"):
                    raw_text = raw_text[3:-3].strip()

                ai_decision = json.loads(raw_text)
                current_state = ai_decision.get("current_state", {}) or {}
                action_type = ai_decision["command"]["action"]
                thought = ai_decision.get("thought") or current_state.get("next_goal") or "Thinking..."
                evaluation_previous_goal = current_state.get("evaluation_previous_goal", "")
                agent_memory = current_state.get("memory") or agent_memory
                last_next_goal = current_state.get("next_goal") or last_next_goal

                if ui_callback:
                    if screenshot_bytes:
                        ui_callback(f"**Thought:** {thought}", image_bytes=screenshot_bytes)
                    else:
                        ui_callback(f"**Thought:** {thought}")
                    if evaluation_previous_goal:
                        ui_callback(f"**Assessment:** {evaluation_previous_goal}")
                    ui_callback(f"**Action:** `{action_type}`")

            except Exception as exc:
                print(f"JSON Parse Error: {exc}")
                step_count += 1
                await asyncio.sleep(5)
                continue

            if action_type == "type":
                element_id = ai_decision["command"]["element_id"]
                text_to_type = ai_decision["command"]["text"]
                element_details = _get_element_details(dom_data, element_id)
                element_label = element_details["text"][:40] if element_details else "unknown field"
                is_repeat_text = _normalize_text(text_to_type) == _normalize_text(last_typed_text)
                is_repeat_context = current_url == last_typed_url and element_label == (last_typed_field or element_label)

                if last_typed_text and is_repeat_text and is_repeat_context and _is_search_like_element(element_details):
                    if ui_callback:
                        ui_callback(
                            "**Guard:** The same search text was already entered here. Taking a fresh screen check instead of retyping it."
                        )
                    action_history.append(
                        f"Blocked repeated search typing for '{text_to_type}' in '{element_label}' and requested confirmation first (Step {step_count})"
                    )
                    needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue

            signature = None
            if action_type == "goto":
                signature = ("goto", ai_decision["command"]["url"])
            elif action_type == "type":
                signature = ("type", ai_decision["command"]["element_id"], ai_decision["command"]["text"])
            elif action_type == "click":
                signature = ("click", ai_decision["command"]["element_id"])
            elif action_type == "press":
                signature = ("press", ai_decision["command"]["key"])

            if signature and len(action_signatures) >= 2 and action_signatures[-2:] == [signature, signature]:
                if repeated_observation_count >= 1 and stall_recovery_attempts < 2:
                    stall_recovery_attempts += 1
                    recovery_mode = "vision" if not needs_vision else "deep read"
                    if ui_callback:
                        ui_callback(
                            f"**Recovery:** Repeated action on an unchanged page detected. Switching to {recovery_mode} before retrying."
                        )
                    action_history.append(
                        f"Detected a stall after repeating {action_type}; switched to fresh inspection ({recovery_mode}) instead of blindly retrying (Step {step_count})"
                    )
                    if needs_vision:
                        needs_deep_read = True
                    else:
                        needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue
                final_report = _repeat_guard_message(action_type)
                break

            if signature:
                action_signatures.append(signature)

            if action_type == "finish":
                if _is_media_objective(user_objective):
                    await _wait_for_media_completion(page, ui_callback=ui_callback)
                last_action_result = "The goal appears complete on the current page."
                final_report = _finalize_report(ai_decision["command"]["reason"], user_objective)
                break

            if action_type == "read":
                needs_deep_read = True
                last_action_result = "Requested a deeper text scan of the current page."
                action_history.append(f"Triggered a deep read (Step {step_count})")
                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "look":
                needs_vision = True
                last_action_result = "Requested a screenshot-based visual inspection of the current page."
                action_history.append(f"Triggered the camera to look at the screen (Step {step_count})")
                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "goto":
                target_url = ai_decision["command"]["url"]
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
                direct_site_url = _infer_direct_site_url(user_objective)
                if direct_site_url and _is_google_search_url(target_url):
                    if ui_callback:
                        ui_callback(
                            f"**Direct routing:** The task names a known site, so I am opening `{direct_site_url}` directly instead of going through Google."
                        )
                    target_url = direct_site_url
                try:
                    await page.goto(target_url)
                    await _settle_page(page, delay_seconds=4)
                except Exception as exc:
                    error_text = str(exc)
                    if "ERR_NAME_NOT_RESOLVED" in error_text:
                        final_report = (
                            f"I could not open `{target_url}` because the domain could not be resolved. "
                            "Please check the URL or your internet/DNS connection and try again."
                        )
                        break
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in error_text:
                        final_report = (
                            f"I reached `{target_url}`, but the site blocked or rejected the browser navigation. "
                            "This usually happens because the website returned a restricted page, anti-bot response, "
                            "or location/login gate. Try running with a visible browser profile instead of headless mode "
                            "and make sure the saved profile already has the required location or login state."
                        )
                        break
                    raise
                last_action_result = f"Navigated to {target_url}. Current page is now {page.url}."
                action_history.append(f"Navigated to {target_url} (Step {step_count})")
                needs_vision = True
                step_count += 1
                continue

            if action_type == "scroll":
                direction = ai_decision["command"]["direction"]
                viewport = page.viewport_size
                if viewport:
                    await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)

                if direction == "down":
                    await page.mouse.wheel(0, 800)
                    await page.evaluate("window.scrollBy(0, 800)")
                    last_action_result = "Scrolled down to reveal more content."
                    action_history.append(f"Scrolled down (Step {step_count})")
                else:
                    await page.mouse.wheel(0, -800)
                    await page.evaluate("window.scrollBy(0, -800)")
                    last_action_result = "Scrolled up to revisit higher page content."
                    action_history.append(f"Scrolled up (Step {step_count})")

                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "press":
                key = ai_decision["command"]["key"]
                await page.keyboard.press(key)
                last_action_result = f"Pressed the {key} key."
                if _normalize_text(key) == "enter":
                    last_typed_text = None
                    last_typed_field = None
                    last_typed_url = None
                action_history.append(f"Pressed '{key}' (Step {step_count})")
                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "type":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                text_to_type = ai_decision["command"]["text"]
                element_label = next((el["text"][:40] for el in dom_data if el["id"] == element_id), "unknown field")

                previous_page_count = len([candidate for candidate in context.pages if not candidate.is_closed()])
                clicked = await _click_locator_safely(target_locator)
                if not clicked:
                    last_action_result = f"Could not focus {element_label}; the element disappeared or the page changed."
                    action_history.append(
                        f"Could not focus '{element_label}' (ID {element_id}) because the page changed or the element disappeared (Step {step_count})"
                    )
                    needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue

                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(text_to_type, delay=10)
                page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                await _settle_page(page, delay_seconds=2)
                last_typed_text = text_to_type
                last_typed_field = element_label
                last_typed_url = current_url
                last_action_result = f"Typed '{text_to_type}' into '{element_label}'."
                action_history.append(
                    f"Typed '{text_to_type}' into '{element_label}' (ID {element_id}, Step {step_count})"
                )
                step_count += 1
                continue

            if action_type == "click":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                element_label = next((el["text"][:40] for el in dom_data if el["id"] == element_id), "unknown")

                previous_page_count = len([candidate for candidate in context.pages if not candidate.is_closed()])
                clicked = await _click_locator_safely(target_locator)
                if not clicked:
                    last_action_result = f"Could not click {element_label}; the target disappeared or the page changed."
                    action_history.append(
                        f"Could not click '{element_label}' (ID {element_id}) because the page changed or the element disappeared (Step {step_count})"
                    )
                    needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue
                page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                await _settle_page(page, delay_seconds=2)
                if _is_search_like_element(_get_element_details(dom_data, element_id)):
                    last_typed_text = None
                    last_typed_field = None
                    last_typed_url = None
                last_action_result = f"Clicked '{element_label}'. Current page is {page.url}."
                action_history.append(f"Clicked '{element_label}' (ID {element_id}, Step {step_count})")

            step_count += 1
            await asyncio.sleep(2)

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

import asyncio
import json
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

from schema import AgentOutput

load_dotenv()


async def get_dom_snapshot(page, deep_read=False):
    """Dynamically switches between fast navigation and deep reading."""
    if deep_read:
        selectors = "button, a, input, [role=\"button\"], textarea, h3, p, li, [contenteditable=\"true\"]"
        max_chars = 1000
        max_elements = 150
    else:
        selectors = "button, a, input, [role=\"button\"], textarea, h3, [contenteditable=\"true\"]"
        max_chars = 100
        max_elements = 80

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

            let rawText = el.innerText || el.value || el.placeholder || 'No Text';
            let cleanText = rawText.substring(0, {max_chars}).replace(/\\n/g, ' ');

            simplifiedDOM.push({{
                id: id,
                tag: el.tagName,
                text: cleanText,
                type: el.type || 'N/A'
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
        ]
    ).lower()
    search_markers = ["search", "find", "products", "brands", "amazon", "flipkart", "myntra", "youtube", "google"]
    return any(marker in haystack for marker in search_markers)


async def run_agent(user_objective, ui_callback=None):
    client = genai.Client()
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")

    async with async_playwright() as p:
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

        while step_count < 20:
            page = await _ensure_active_page(context, page)
            dom_data = await get_dom_snapshot(page, deep_read=needs_deep_read)
            needs_deep_read = False

            current_url = page.url

            prompt = f"""
            # SYSTEM INSTRUCTIONS
            You are an elite autonomous web agent. Your job is to achieve the user's objective by navigating the web, extracting information, and taking actions.

            # STRATEGY
            Before acting, mentally break the goal into ordered sub-steps.
            After each action, check whether the page confirms success before continuing.
            If the page looks the same after an action, assume it failed and try a different approach.

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

            # CONTEXT
            Current URL: {current_url}
            Last Entered Text: {last_typed_text or "None"}
            Last Entered Field: {last_typed_field or "None"}
            Last Entered URL: {last_typed_url or "None"}

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
                action_type = ai_decision["command"]["action"]
                thought = ai_decision.get("thought", "Thinking...")

                if ui_callback:
                    if screenshot_bytes:
                        ui_callback(f"**Thought:** {thought}", image_bytes=screenshot_bytes)
                    else:
                        ui_callback(f"**Thought:** {thought}")
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
                final_report = _repeat_guard_message(action_type)
                break

            if signature:
                action_signatures.append(signature)

            if action_type == "finish":
                final_report = ai_decision["command"]["reason"]
                break

            if action_type == "read":
                needs_deep_read = True
                action_history.append(f"Triggered a deep read (Step {step_count})")
                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "look":
                needs_vision = True
                action_history.append(f"Triggered the camera to look at the screen (Step {step_count})")
                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "goto":
                target_url = ai_decision["command"]["url"]
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
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
                    raise
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
                    action_history.append(f"Scrolled down (Step {step_count})")
                else:
                    await page.mouse.wheel(0, -800)
                    await page.evaluate("window.scrollBy(0, -800)")
                    action_history.append(f"Scrolled up (Step {step_count})")

                step_count += 1
                await asyncio.sleep(2)
                continue

            if action_type == "press":
                key = ai_decision["command"]["key"]
                await page.keyboard.press(key)
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
                    action_history.append(
                        f"Could not focus '{element_label}' (ID {element_id}) because the page changed or the element disappeared (Step {step_count})"
                    )
                    needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue

                await page.keyboard.type(text_to_type, delay=10)
                page = await _adopt_newest_page_if_needed(context, page, previous_page_count)
                await _settle_page(page, delay_seconds=2)
                last_typed_text = text_to_type
                last_typed_field = element_label
                last_typed_url = current_url
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
                    action_history.append(
                        f"Could not click '{element_label}' (ID {element_id}) because the page changed or the element disappeared (Step {step_count})"
                    )
                    needs_vision = True
                    step_count += 1
                    await asyncio.sleep(1)
                    continue
                page = await _adopt_newest_page_if_needed(context, page, previous_page_count)
                await _settle_page(page, delay_seconds=2)
                if _is_search_like_element(_get_element_details(dom_data, element_id)):
                    last_typed_text = None
                    last_typed_field = None
                    last_typed_url = None
                action_history.append(f"Clicked '{element_label}' (ID {element_id}, Step {step_count})")

            step_count += 1
            await asyncio.sleep(2)

        await _emit_final_screenshot(page, ui_callback)
        await asyncio.sleep(2)
        await context.close()

        return final_report


if __name__ == "__main__":
    asyncio.run(run_agent("Test agent run"))

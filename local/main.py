import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from dotenv import load_dotenv
from ollama import Client
from playwright.async_api import async_playwright

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

load_dotenv(PROJECT_ROOT / ".env")

from schema import AgentOutput

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "").strip()
ACTION_TIMEOUT_MS = 3000
SHORT_SETTLE_SECONDS = 0.8
NAVIGATION_SETTLE_SECONDS = 1.5
TYPE_SETTLE_SECONDS = 1.8
_RETAINED_BROWSER_SESSION = None
KEY_ALIASES = {
    "esc": "Escape",
    "escape": "Escape",
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "space": " ",
    "spacebar": " ",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
}


def _strip_json_fence(raw_text):
    raw_text = raw_text.strip()
    if raw_text.startswith("```json"):
        return raw_text[7:-3].strip()
    if raw_text.startswith("```"):
        return raw_text[3:-3].strip()
    return raw_text


def _normalize_keyboard_key(key):
    if key is None:
        return None

    raw = str(key).strip()
    if not raw:
        return None

    normalized = KEY_ALIASES.get(raw.lower(), raw)
    if len(normalized) == 1:
        return normalized

    if re.fullmatch(r"F([1-9]|1[0-2])", normalized, flags=re.IGNORECASE):
        return normalized.upper()

    modifier_combo = re.fullmatch(
        r"(?i)(ctrl|control|shift|alt|meta)\+([A-Za-z])",
        normalized,
    )
    if modifier_combo:
        modifier = modifier_combo.group(1).lower()
        key_char = modifier_combo.group(2).upper()
        modifier_map = {
            "ctrl": "Control",
            "control": "Control",
            "shift": "Shift",
            "alt": "Alt",
            "meta": "Meta",
        }
        return f"{modifier_map[modifier]}+{key_char}"

    valid_named_keys = {
        "Escape",
        "Enter",
        "Tab",
        "Backspace",
        "Delete",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "PageUp",
        "PageDown",
        "Home",
        "End",
    }
    if normalized in valid_named_keys:
        return normalized

    return None


def _is_media_objective(user_objective):
    objective = " ".join(str(user_objective or "").strip().lower().split())
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


def _normalize_agent_output(payload):
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object, got {type(payload).__name__}.")

    if "agent" in payload and isinstance(payload["agent"], dict):
        payload = payload["agent"]

    thought = (
        payload.get("thought")
        or payload.get("reason")
        or payload.get("analysis")
        or payload.get("message")
        or payload.get("thought_process")
        or "Planning the next browser action."
    )

    command = payload.get("command")
    if command is None and isinstance(payload.get("actions"), list) and payload["actions"]:
        command = payload["actions"][0]
    if command is None:
        command = payload

    if not isinstance(command, dict):
        raise ValueError("Model response did not contain a valid command object.")

    parameters = command.get("parameters")
    if parameters is None or not isinstance(parameters, dict):
        parameters = {}

    action = str(
        command.get("action")
        or command.get("command")
        or command.get("type")
        or ""
    ).strip().lower()

    if not action:
        inferred_text = (
            command.get("text")
            or command.get("value")
            or command.get("query")
            or parameters.get("text")
            or parameters.get("value")
            or parameters.get("query")
        )
        inferred_element_id = (
            command.get("element_id")
            or command.get("id")
            or parameters.get("element_id")
            or parameters.get("id")
        )
        inferred_url = (
            command.get("url")
            or command.get("link")
            or command.get("website")
            or command.get("target")
            or parameters.get("url")
            or parameters.get("link")
        )
        inferred_key = (
            command.get("key")
            or parameters.get("key")
            or (command.get("text") if not inferred_element_id else None)
            or (parameters.get("text") if not inferred_element_id else None)
        )
        inferred_direction = command.get("direction") or parameters.get("direction")

        if inferred_url:
            action = "goto"
        elif inferred_element_id is not None and inferred_text is not None:
            action = "type"
        elif inferred_element_id is not None:
            action = "click"
        elif inferred_direction in {"up", "down"}:
            action = "scroll"
        elif _normalize_keyboard_key(inferred_key):
            action = "press"
        elif "success" in command or "reason" in command:
            action = "finish"
        else:
            raise ValueError("Model response did not include an action.")

    if action == "search":
        query = command.get("query") or command.get("text") or payload.get("query")
        if not query:
            raise ValueError("Search action was returned without a query.")
        normalized_command = {
            "action": "goto",
            "url": f"https://www.google.com/search?q={quote_plus(str(query))}",
        }
    elif action in {"goto", "open", "navigate", "visit", "go_to", "navigate_to_url"}:
        url = (
            command.get("url")
            or command.get("link")
            or command.get("website")
            or command.get("target")
            or parameters.get("url")
            or parameters.get("link")
        )
        if not url:
            raise ValueError(f"Action '{action}' was returned without a URL.")
        normalized_command = {"action": "goto", "url": str(url)}
    elif action == "click":
        element_id = command.get("element_id", command.get("id"))
        if element_id is None:
            element_id = parameters.get("element_id", parameters.get("id"))
        if element_id is None:
            raise ValueError("Click action was returned without an element ID.")
        normalized_command = {"action": "click", "element_id": int(element_id)}
    elif action in {"type", "input", "enter"}:
        element_id = command.get("element_id", command.get("id"))
        if element_id is None:
            element_id = parameters.get("element_id", parameters.get("id"))
        text = (
            command.get("text")
            or command.get("value")
            or command.get("query")
            or parameters.get("text")
            or parameters.get("value")
            or parameters.get("query")
        )
        if element_id is None or text is None:
            raise ValueError(f"Action '{action}' requires both element_id and text.")
        normalized_command = {
            "action": "type",
            "element_id": int(element_id),
            "text": str(text),
        }
    elif action in {"press", "key", "keypress"}:
        key = (
            command.get("key")
            or command.get("text")
            or parameters.get("key")
            or parameters.get("text")
        )
        if not key:
            raise ValueError(f"Action '{action}' was returned without a key.")
        normalized_key = _normalize_keyboard_key(key)
        if normalized_key:
            normalized_command = {"action": "press", "key": normalized_key}
        else:
            normalized_command = {"action": "read", "reason": str(thought)}
    elif action in {"scroll", "scroll_down", "scroll_up"}:
        direction = command.get("direction") or parameters.get("direction")
        if action == "scroll_down":
            direction = "down"
        elif action == "scroll_up":
            direction = "up"
        normalized_command = {"action": "scroll", "direction": direction or "down"}
    elif action == "read":
        normalized_command = {
            "action": "read",
            "reason": str(command.get("reason") or thought),
        }
    elif action == "look":
        normalized_command = {
            "action": "look",
            "reason": str(command.get("reason") or thought or "Need visual context."),
        }
    elif action == "finish":
        normalized_command = {
            "action": "finish",
            "success": bool(command.get("success", True)),
            "reason": str(command.get("reason") or thought),
        }
    else:
        raise ValueError(f"Unsupported action '{action}'.")

    normalized_payload = {"thought": str(thought), "command": normalized_command}
    return AgentOutput.model_validate(normalized_payload).model_dump()


async def get_dom_snapshot(page, deep_read=False):
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


def _find_dom_element(dom_data, element_id):
    for item in dom_data:
        if item.get("id") == element_id:
            return item
    return None


def _extract_search_query(user_objective):
    patterns = [
        r"search for (.+?)(?:,| and | then |$)",
        r"find (.+?)(?:,| and | then |$)",
        r"look for (.+?)(?:,| and | then |$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, user_objective, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip(".")
    return None


def _tokenize_text(text):
    return re.findall(r"[a-z0-9]+", str(text).lower())


def _expand_query_terms(tokens):
    expanded = set(tokens)
    for token in tokens:
        compact = token.replace(" ", "")
        expanded.add(compact)
        if compact == "pendrive":
            expanded.update({"pen", "drive"})
        if compact == "usbdrive":
            expanded.update({"usb", "drive"})

        parts = re.findall(r"[a-z]+|\d+", compact)
        if len(parts) > 1:
            expanded.update(parts)

    return [token for token in expanded if token]


def _extract_query_terms(user_objective):
    query = _extract_search_query(user_objective) or user_objective
    stopwords = {
        "open",
        "search",
        "for",
        "find",
        "look",
        "under",
        "rs",
        "rupees",
        "and",
        "the",
        "with",
        "without",
        "me",
        "tell",
        "give",
        "first",
        "three",
        "results",
        "best",
        "options",
        "top",
        "size",
        "in",
        "of",
        "to",
        "on",
    }
    raw_tokens = [token for token in _tokenize_text(query) if token not in stopwords and len(token) >= 2]
    return _expand_query_terms(raw_tokens)


def _term_matches_text(term, tokens, lowered):
    compact_text = re.sub(r"[^a-z0-9]+", "", lowered)
    compact_term = re.sub(r"[^a-z0-9]+", "", term.lower())
    if not compact_term:
        return False
    if compact_term in tokens:
        return True
    if compact_term in compact_text:
        return True
    return False


def _find_clickable_by_text(dom_data, target_text):
    if not target_text:
        return None

    target = str(target_text).strip().lower()
    if not target:
        return None

    best = None
    best_score = -1
    for item in dom_data:
        tag = str(item.get("tag") or "").upper()
        item_text = str(item.get("text") or "").strip().lower()
        if tag not in {"BUTTON", "A", "DIV", "SPAN"} and "button" not in str(item.get("type") or "").lower():
            continue
        if not item_text:
            continue

        score = 0
        if item_text == target:
            score += 6
        if target in item_text:
            score += 4
        target_words = [word for word in re.split(r"\W+", target) if word]
        item_words = set(word for word in re.split(r"\W+", item_text) if word)
        score += sum(1 for word in target_words if word in item_words)

        if score > best_score:
            best = item
            best_score = score

    return best if best_score >= 3 else None


def _find_search_input(dom_data):
    best = None
    best_score = -1
    for item in dom_data:
        tag = str(item.get("tag") or "").upper()
        text = str(item.get("text") or "").lower()
        input_type = str(item.get("type") or "").lower()
        score = 0

        if tag in {"INPUT", "TEXTAREA"}:
            score += 4
        if "search" in text:
            score += 5
        if input_type == "search":
            score += 4
        if input_type in {"text", "email", "tel"}:
            score += 1
        if "products" in text or "brands" in text or "items" in text:
            score += 1

        if score > best_score:
            best = item
            best_score = score

    return best if best_score >= 4 else None


def _format_element_label(element):
    if not element:
        return "unknown element"
    tag = str(element.get("tag") or "element").lower()
    text = str(element.get("text") or "No Text").strip().replace("\n", " ")
    return f"{tag} '{text[:80]}'"


def _is_summary_results_task(user_objective):
    objective = user_objective.lower()
    summary_terms = [
        "first three results",
        "best three options",
        "top three",
        "three options",
        "three results",
        "shortlist",
        "tell me the first three",
        "give me the best three",
    ]
    action_terms = [
        "add to cart",
        "checkout",
        "place order",
        "buy",
        "book",
        "apply filter",
    ]
    return any(term in objective for term in summary_terms) and not any(term in objective for term in action_terms)


def _extract_result_candidates(dom_data, user_objective):
    banned_terms = {
        "login",
        "sign in",
        "cart",
        "wishlist",
        "filter",
        "sort",
        "men",
        "women",
        "kids",
        "home",
        "beauty",
        "electronics",
        "fashion",
        "plus",
        "grocery",
        "become a seller",
        "search for products",
        "search",
        "location not set",
        "select delivery location",
        "toys",
        "baby",
        "food & health",
        "beauty, toys",
        "mobiles",
        "appliances",
        "travel",
    }
    query_terms = _extract_query_terms(user_objective)
    candidates = []
    seen = set()
    for item in dom_data:
        tag = str(item.get("tag") or "").upper()
        text = str(item.get("text") or "").strip()
        lowered = text.lower()
        tokens = set(_tokenize_text(text))

        if tag not in {"A", "H1", "H2", "H3"}:
            continue
        if len(text) < 12 or len(text) > 140:
            continue
        if any(term in lowered for term in banned_terms):
            continue
        if lowered in seen:
            continue
        if not any(char.isalpha() for char in text):
            continue

        overlap = [term for term in query_terms if _term_matches_text(term, tokens, lowered)]
        if query_terms and len(overlap) == 0:
            continue

        score = 0
        score += len(overlap) * 4
        if tag == "H3":
            score += 2
        if any(char.isdigit() for char in text):
            score += 1
        if len(tokens) >= 3:
            score += 1

        seen.add(lowered)
        candidates.append((score, text))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [text for _, text in candidates]


def _detect_results_summary_completion(user_objective, dom_data):
    if not _is_summary_results_task(user_objective):
        return None

    candidates = _extract_result_candidates(dom_data, user_objective)
    if len(candidates) < 3:
        return None

    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(candidates[:3], start=1))
    return f"Here are the first three visible results:\n{numbered}"


def _detect_cart_completion(user_objective, current_url, page_title, dom_data):
    objective_text = user_objective.lower()
    if "cart" not in objective_text:
        return None

    combined_text = " ".join(
        [
            page_title or "",
            current_url or "",
            *[str(item.get("text") or "") for item in dom_data],
        ]
    ).lower()

    cart_evidence = [
        "added to cart",
        "added to basket",
        "proceed to checkout",
        "view cart",
        "go to cart",
        "cart subtotal",
        "subtotal",
        "items in cart",
        "shopping cart",
    ]
    if any(phrase in combined_text for phrase in cart_evidence):
        return "The item appears to be added to the cart, so I stopped before checkout as requested."

    return None


def _detect_blocking_popup(user_objective, dom_data):
    objective = user_objective.lower()
    if any(term in objective for term in {"login", "sign in", "sign-in", "otp", "account"}):
        return None

    combined_text = " ".join(str(item.get("text") or "") for item in dom_data).lower()
    popup_markers = [
        "request otp",
        "enter email/mobile number",
        "enter email",
        "mobile number",
        "new to flipkart",
        "create an account",
        "get access to your orders",
        "wishlist and recommendations",
    ]
    matches = [marker for marker in popup_markers if marker in combined_text]
    if len(matches) >= 2:
        return "A blocking login popup is covering the page."

    return None


async def _settle_page(page, wait_for_network=False, extra_delay=SHORT_SETTLE_SECONDS):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
    except Exception:
        pass

    if wait_for_network:
        try:
            await page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

    await asyncio.sleep(extra_delay)


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


def _coerce_non_key_press_action(ai_decision, dom_data):
    action_type = ai_decision["command"]["action"]
    if action_type != "press":
        return ai_decision

    raw_key = ai_decision["command"].get("key")
    normalized_key = _normalize_keyboard_key(raw_key)
    if normalized_key:
        ai_decision["command"]["key"] = normalized_key
        return ai_decision

    matching_element = _find_clickable_by_text(dom_data, raw_key)
    if matching_element:
        return {
            "thought": (
                f"'{raw_key}' is not a keyboard key, so I should click the visible "
                f"{_format_element_label(matching_element)} control instead."
            ),
            "command": {
                "action": "click",
                "element_id": matching_element["id"],
            },
        }

    return {
        "thought": f"'{raw_key}' is not a keyboard key, so I should inspect the page more deeply first.",
        "command": {
            "action": "read",
            "reason": f"Need more context before acting on '{raw_key}'.",
        },
    }


def _coerce_popup_input_action(ai_decision, dom_data, popup_reason):
    if not popup_reason:
        return ai_decision

    action_type = ai_decision["command"]["action"]
    if action_type not in {"type", "click"}:
        return ai_decision

    element_id = ai_decision["command"].get("element_id")
    target_element = _find_dom_element(dom_data, element_id)
    target_text = str((target_element or {}).get("text") or "").lower()

    popup_field_markers = [
        "email",
        "mobile",
        "otp",
        "login",
        "sign in",
        "request otp",
    ]
    if any(marker in target_text for marker in popup_field_markers):
        return {
            "thought": "The selected control belongs to a blocking login popup, so I should dismiss the popup instead of interacting with it.",
            "command": {
                "action": "press",
                "key": "Escape",
            },
        }

    return ai_decision


def _coerce_site_search_action(ai_decision, current_url, dom_data, user_objective, action_history):
    action_type = ai_decision["command"]["action"]
    if action_type != "goto":
        return ai_decision

    if not current_url or current_url.startswith("about:blank"):
        return ai_decision

    search_query = _extract_search_query(user_objective)
    search_input = _find_search_input(dom_data)
    if not search_query or not search_input:
        return ai_decision

    target_url = ai_decision["command"]["url"]
    normalized_target = target_url if target_url.startswith("http") else f"https://{target_url}"
    current_parsed = urlparse(current_url)
    target_parsed = urlparse(normalized_target)
    current_host = current_parsed.netloc.replace("www.", "")
    target_host = target_parsed.netloc.replace("www.", "")
    same_host = current_host and target_host and current_host == target_host
    homepage_like = target_parsed.path in {"", "/"}
    same_url = normalized_target.rstrip("/") == current_url.rstrip("/")
    recent_navigations = sum(1 for item in action_history[-3:] if str(item).startswith("Navigated"))

    if same_host and (homepage_like or same_url or recent_navigations >= 2):
        return {
            "thought": (
                f"A visible site search box is available, so I should type '{search_query}' "
                f"instead of navigating again."
            ),
            "command": {
                "action": "type",
                "element_id": search_input["id"],
                "text": search_query,
            },
        }

    return ai_decision


def _response_content(response):
    message = getattr(response, "message", None)
    content = getattr(message, "content", None)
    if content is not None:
        return content
    if isinstance(response, dict):
        return ((response.get("message") or {}).get("content")) or ""
    return ""


def get_next_action(client, prompt, screenshot_bytes=None):
    model_name = OLLAMA_VISION_MODEL if screenshot_bytes and OLLAMA_VISION_MODEL else OLLAMA_MODEL
    message = {"role": "user", "content": prompt}
    if screenshot_bytes and OLLAMA_VISION_MODEL:
        message["images"] = [screenshot_bytes]

    response = client.chat(
        model=model_name,
        messages=[message],
        stream=False,
        think=False,
        format=AgentOutput.model_json_schema(),
        options={"temperature": 0},
    )

    raw_text = _strip_json_fence(_response_content(response) or "")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned invalid JSON: {raw_text}") from exc

    try:
        return _normalize_agent_output(payload)
    except Exception as exc:
        raise ValueError(f"Could not normalize model output: {raw_text}") from exc


async def run_agent(user_objective, ui_callback=None, keep_browser_open=False):
    client = Client(host=OLLAMA_HOST)
    global _RETAINED_BROWSER_SESSION

    await _release_retained_browser_session()

    p = await async_playwright().start()
    context = None
    retain_browser_session = False
    try:
        user_data_dir = str((PROJECT_ROOT / "agent_profile").resolve())

        print(f"Booting persistent browser with Ollama model: {OLLAMA_MODEL}")
        if OLLAMA_VISION_MODEL:
            print(f"Vision fallback enabled with: {OLLAMA_VISION_MODEL}")

        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("about:blank")

        step_count = 0
        action_history = []
        final_report = "Task failed or timed out."
        needs_deep_read = False
        needs_vision = False

        while step_count < 20:
            page = await _ensure_active_page(context, page)
            dom_data = await get_dom_snapshot(page, deep_read=needs_deep_read)
            needs_deep_read = False

            page_title = await page.title()
            current_url = page.url

            completion_reason = _detect_cart_completion(user_objective, current_url, page_title, dom_data)
            if completion_reason:
                final_report = completion_reason
                if ui_callback:
                    ui_callback("**Thought:** Cart completion evidence is visible, so I am stopping here.")
                    ui_callback("**Action:** `finish`")
                break

            results_summary = _detect_results_summary_completion(user_objective, dom_data)
            if results_summary:
                final_report = results_summary
                if ui_callback:
                    ui_callback("**Thought:** I can already see at least three visible listing results, so I should finish from the results page.")
                    ui_callback("**Action:** `finish`")
                break

            popup_reason = _detect_blocking_popup(user_objective, dom_data)
            recent_popup_escapes = sum(
                1 for item in action_history[-3:] if "Dismissed popup with Escape" in str(item)
            )
            if popup_reason and recent_popup_escapes == 0:
                if ui_callback:
                    ui_callback("**Thought:** A blocking login popup is visible, so I should dismiss it before continuing.")
                    ui_callback("**Action:** `press Escape`")
                await page.keyboard.press("Escape")
                await _settle_page(page, wait_for_network=False, extra_delay=SHORT_SETTLE_SECONDS)
                action_history.append(f"Dismissed popup with Escape (Step {step_count})")
                step_count += 1
                continue
            if popup_reason and recent_popup_escapes > 0 and OLLAMA_VISION_MODEL:
                needs_vision = True

            prompt = f"""
            # SYSTEM INSTRUCTIONS
            You are an elite autonomous web agent. Your job is to achieve the user's objective by navigating the web, extracting information, and taking actions.

            # YOUR TOOLKIT RULES
            0. You may only output one of these actions: click, type, finish, goto, read, scroll, press, look.
            1. Navigation: Use "goto" to open a URL. Use "scroll" to move up or down.
            2. Interaction: Use "click" and "type" using the provided element IDs from the JSON. Use "press" for keyboard keys.
            3. Reading: The JSON provides basic text. If text is hidden in paragraphs, use "read".
            4. Vision: Use "look" only when a separate vision model is configured or when a screenshot is truly required. If vision is unavailable, prefer "read" or dismissing the popup.
            5. ANTI-LOOP RULE: Do not use "scroll", or click the exact same ID, more than twice in a row.
            6. THE POPUP PROTOCOL: If a login/signup popup is covering the page and the user did not ask to log in, first dismiss it with Escape or by clicking its close button. Never type the product search query into a login or OTP field.
            7. SUCCESS RECOGNITION: If you see evidence that your goal was achieved, do not restart. Immediately use the "finish" action.
            8. Never invent new actions like "search", "open", or "navigate".
            9. Do not jump back to the homepage once you are already on the target site unless the current page is clearly wrong.
            10. If you are already on a good product page and the objective is to add the item to cart, click the Add to Cart button next. Do not click another product.
            11. Only click elements whose text matches the intended action. For example, for add-to-cart you must click text like "Add to Cart", not ratings, product titles, or unrelated links.
            12. If you are already on the target site and a visible search box exists, prefer "type" into that search box over another "goto".
            13. For "best three options" or shortlist tasks, stay on the results/listing page and gather multiple candidates before opening any product page.

            # OUTPUT FORMAT
            Return exactly one next step.
            Do not return an agent definition.
            Do not return an array of actions.
            Do not return keys like "agent", "actions", "parameters", or "thought_process".
            Return JSON in this exact shape:
            {{
              "thought": "short reason for the next step",
              "command": {{
                "action": "goto",
                "url": "https://example.com"
              }}
            }}

            # LOCAL MODEL CONTEXT
            Primary local model: {OLLAMA_MODEL}
            Vision fallback: {OLLAMA_VISION_MODEL or "disabled"}

            # CONTEXT
            Current Page Title:
            {page_title}

            Current Page URL:
            {current_url}

            Popup State:
            {popup_reason or "No obvious blocking popup detected from the DOM."}

            Recent Actions:
            {json.dumps(action_history[-6:], indent=2)}

            Past Actions Taken (DO NOT REPEAT THESE):
            {json.dumps(action_history, indent=2)}

            Current Screen Elements (JSON):
            {json.dumps(dom_data, indent=2)}

            # USER OBJECTIVE
            Goal: {user_objective}

            What is your next logical step? Output strictly matching the JSON schema.
            """

            try:
                screenshot_bytes = None
                if needs_vision and OLLAMA_VISION_MODEL:
                    screenshot_bytes = await page.screenshot()
                    prompt += "\n\nVISION MODE: A screenshot of the current page is attached."
                    needs_vision = False

                ai_decision = get_next_action(client, prompt, screenshot_bytes=screenshot_bytes)
                ai_decision = _coerce_non_key_press_action(ai_decision, dom_data)
                ai_decision = _coerce_popup_input_action(ai_decision, dom_data, popup_reason)
                ai_decision = _coerce_site_search_action(
                    ai_decision,
                    current_url=current_url,
                    dom_data=dom_data,
                    user_objective=user_objective,
                    action_history=action_history,
                )
                action_type = ai_decision["command"]["action"]
                thought = ai_decision.get("thought", "Thinking...")

                if ui_callback:
                    if screenshot_bytes:
                        ui_callback(f"**Thought:** {thought}", image_bytes=screenshot_bytes)
                    else:
                        ui_callback(f"**Thought:** {thought}")
                    ui_callback(f"**Action:** `{action_type}`")

            except Exception as exc:
                print(f"Decision parse error: {exc}")
                step_count += 1
                await asyncio.sleep(SHORT_SETTLE_SECONDS)
                continue

            if action_type == "finish":
                if _is_media_objective(user_objective):
                    await _wait_for_media_completion(page, ui_callback=ui_callback)
                final_report = ai_decision["command"]["reason"]
                break

            if action_type == "read":
                needs_deep_read = True
                action_history.append(f"Triggered a deep read (Step {step_count})")
                step_count += 1
                await asyncio.sleep(SHORT_SETTLE_SECONDS)
                continue

            if action_type == "look":
                if OLLAMA_VISION_MODEL:
                    needs_vision = True
                    action_history.append(f"Triggered vision mode (Step {step_count})")
                else:
                    needs_deep_read = True
                    action_history.append(
                        f"Vision requested but unavailable, so a deep read was used instead (Step {step_count})"
                    )
                step_count += 1
                await asyncio.sleep(SHORT_SETTLE_SECONDS)
                continue

            if action_type == "goto":
                target_url = ai_decision["command"]["url"]
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
                previous_url = page.url
                try:
                    await page.goto(target_url, wait_until="domcontentloaded")
                except Exception as exc:
                    error_text = str(exc)
                    if "ERR_NAME_NOT_RESOLVED" in error_text:
                        final_report = (
                            f"I could not open `{target_url}` because the domain could not be resolved. "
                            "Please check the URL or your internet/DNS connection and try again."
                        )
                        break
                    raise
                await _settle_page(page, wait_for_network=True, extra_delay=NAVIGATION_SETTLE_SECONDS)
                action_history.append(f"Navigated from {previous_url} to {page.url} (Step {step_count})")
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
                await asyncio.sleep(SHORT_SETTLE_SECONDS)
                continue

            if action_type == "press":
                key = ai_decision["command"]["key"]
                page = await _ensure_active_page(context, page)
                await page.keyboard.press(key)
                action_history.append(f"Pressed '{key}' (Step {step_count})")
                step_count += 1
                await asyncio.sleep(SHORT_SETTLE_SECONDS)
                continue

            if action_type == "type":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                text_to_type = ai_decision["command"]["text"]
                element = _find_dom_element(dom_data, element_id)
                previous_page_count = len([candidate for candidate in context.pages if not candidate.is_closed()])

                try:
                    await target_locator.click(timeout=ACTION_TIMEOUT_MS)
                except Exception:
                    await target_locator.evaluate("node => node.click()")

                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(text_to_type, delay=10)
                await page.keyboard.press("Enter")
                page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                await _settle_page(page, wait_for_network=True, extra_delay=TYPE_SETTLE_SECONDS)
                action_history.append(
                    f"Typed '{text_to_type}' into {_format_element_label(element)} on {page.url} (Step {step_count})"
                )
                step_count += 1
                continue

            if action_type == "click":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                element = _find_dom_element(dom_data, element_id)
                previous_url = page.url
                previous_page_count = len([candidate for candidate in context.pages if not candidate.is_closed()])
                try:
                    await target_locator.click(timeout=ACTION_TIMEOUT_MS)
                except Exception:
                    await target_locator.evaluate("node => node.click()")

                page = await _adopt_new_tab_after_action(context, page, previous_page_count)
                await _settle_page(page, wait_for_network=False, extra_delay=SHORT_SETTLE_SECONDS)
                action_history.append(
                    f"Clicked {_format_element_label(element)} from {previous_url} to {page.url} (Step {step_count})"
                )

            step_count += 1
            await asyncio.sleep(SHORT_SETTLE_SECONDS)

        await _emit_final_screenshot(page, ui_callback)
        await asyncio.sleep(SHORT_SETTLE_SECONDS)
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

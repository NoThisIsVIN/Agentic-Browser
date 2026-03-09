import asyncio
import os
import json
from playwright.async_api import async_playwright
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Load the secret API key
load_dotenv()

# Import the rules
from schema import AgentOutput 

async def get_dom_snapshot(page, deep_read=False):
    """Dynamically switches between fast navigation and deep reading."""
    if deep_read:
        selectors = 'button, a, input, [role="button"], textarea, h3, p, li, [contenteditable="true"]'
        max_chars = 1000
        max_elements = 150
    else:
        selectors = 'button, a, input, [role="button"], textarea, h3, [contenteditable="true"]'
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


async def run_agent(user_objective, ui_callback=None):
    client = genai.Client()
    
    async with async_playwright() as p:
        user_data_dir = os.path.abspath("agent_profile")
        
        print("Booting Persistent Stealth Browser...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome", 
            args=["--disable-blink-features=AutomationControlled"], 
            ignore_default_args=["--enable-automation"] 
        )
        
        page = context.pages[0]
        await page.goto("about:blank")
        
        step_count = 0
        action_history = [] 
        final_report = "Task failed or timed out."
        
        # --- State Trackers ---
        needs_deep_read = False 
        needs_vision = False
        # ----------------------
        
        while step_count < 10:
            dom_data = await get_dom_snapshot(page, deep_read=needs_deep_read)
            needs_deep_read = False 
            
            # --- THE NEW SYSTEM PROMPT ARCHITECTURE ---
            prompt = f"""
            # SYSTEM INSTRUCTIONS
            You are an elite autonomous web agent. Your job is to achieve the user's objective by navigating the web, extracting information, and taking actions. 
            
            # YOUR TOOLKIT RULES
            1. Navigation: Use "goto" to open a URL. Use "scroll" to move up or down.
            2. Interaction: Use "click" and "type" using the provided element IDs from the JSON. Use "press" for keyboard keys.
            3. Reading: The JSON provides basic text. If text is hidden in paragraphs, use "read".
            4. VISION (CRITICAL): If you need to solve a visual problem (like a math formula, an image, or a complex NPTEL diagram) that is NOT readable in the JSON, use the "look" tool to take a screenshot. ONLY use "look" if you are completely stuck and cannot answer the user's question with the JSON data alone.
            5. Completion: When the goal is met, use "finish" and provide the final answer in the reason field.
            
            # CONTEXT
            Past Actions Taken (DO NOT REPEAT THESE):
            {json.dumps(action_history, indent=2)}
            
            Current Screen Elements (JSON):
            {json.dumps(dom_data, indent=2)}
            
            # USER OBJECTIVE
            Goal: {user_objective}
            
            What is your next logical step? Output strictly matching the Pydantic schema.
            """
            
            # --- THE ON-DEMAND VISION INJECTION ---
            contents_payload = [prompt]
            screenshot_bytes = None
            
            if needs_vision:
                print("📸 Snapping photo for AI...")
                screenshot_bytes = await page.screenshot()
                # Append the image to the payload
                contents_payload[0] = prompt + "\n\nVISION MODULE ACTIVE: A screenshot of the current page is attached. Analyze the image to find the unextractable data."
                contents_payload.append(types.Part.from_bytes(data=screenshot_bytes, mime_type='image/png'))
                needs_vision = False # Turn the camera back off
            # --------------------------------------
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents_payload,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AgentOutput, 
                    temperature=0.0,
                )
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
                    # Only send image to UI if we actually took one this turn!
                    if screenshot_bytes:
                        ui_callback(f"🧠 **Thought:** {thought}", image_bytes=screenshot_bytes)
                    else:
                        ui_callback(f"🧠 **Thought:** {thought}")
                        
                    ui_callback(f"⚙️ **Action:** `{action_type}`")
                
            except Exception as e:
                print(f"❌ JSON Parse Error: {e}")
                print(f"Raw Output that caused the crash:\n{response.text}")
                step_count += 1
                await asyncio.sleep(5)
                continue
            
            if action_type == "finish":
                final_report = ai_decision['command']['reason']
                break
                
            elif action_type == "read":
                needs_deep_read = True
                action_history.append("Triggered a deep read.")
                step_count += 1
                await asyncio.sleep(2) 
                continue
                
            # --- THE NEW LOOK ACTION ---
            elif action_type == "look":
                needs_vision = True
                action_history.append("Triggered the camera to look at the screen.")
                step_count += 1
                await asyncio.sleep(2)
                continue
            # ---------------------------
                
            elif action_type == "goto":
                target_url = ai_decision["command"]["url"]
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
                await page.goto(target_url)
                action_history.append(f"Navigated to {target_url}")
                step_count += 1
                await asyncio.sleep(8)
                continue
                
            elif action_type == "scroll":
                direction = ai_decision["command"]["direction"]
                if direction == "down":
                    await page.mouse.wheel(0, 800)
                    action_history.append("Scrolled down")
                else:
                    await page.mouse.wheel(0, -800)
                    action_history.append("Scrolled up")
                step_count += 1
                await asyncio.sleep(2)
                continue
                
            elif action_type == "press":
                key = ai_decision["command"]["key"]
                await page.keyboard.press(key)
                action_history.append(f"Pressed '{key}'")
                step_count += 1
                await asyncio.sleep(2)
                continue
            
            elif action_type == "type":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                text_to_type = ai_decision["command"]["text"]
                try:
                    await target_locator.click(timeout=3000)
                except Exception:
                    await target_locator.evaluate("node => node.click()")
                
                await page.keyboard.type(text_to_type, delay=10) 
                await page.keyboard.press("Enter") 
                action_history.append(f"Typed '{text_to_type}' into ID {element_id}")
                step_count += 1
                await asyncio.sleep(20) 
                continue
                
            elif action_type == "click":
                element_id = ai_decision["command"]["element_id"]
                target_locator = page.locator(f'[data-agent-id="{element_id}"]').first
                try:
                    await target_locator.click(timeout=3000)
                except Exception:
                    await target_locator.evaluate("node => node.click()")
                action_history.append(f"Clicked ID {element_id}")
            
            step_count += 1
            await asyncio.sleep(5) 
            
        await asyncio.sleep(2)
        await context.close()
        
        return final_report

if __name__ == "__main__":
    asyncio.run(run_agent("Test agent run"))
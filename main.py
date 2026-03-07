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
    
    # Toggle the JavaScript parameters based on the mode
    if deep_read:
        selectors = 'button, a, input, [role="button"], textarea, h3, p, li, [contenteditable="true"]'
        max_chars = 1000
        max_elements = 150
    else:
        selectors = 'button, a, input, [role="button"], textarea, h3, [contenteditable="true"]'
        max_chars = 100
        max_elements = 80

    # We use Python f-strings to inject those variables into the JS code
    js_code = f"""
    () => {{
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


# --- THE FIX 1: Added ui_callback parameter ---
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
        
        # State tracker for deep reading
        needs_deep_read = False 
        
        while step_count < 10:
            # Pass the state flag and instantly reset it
            dom_data = await get_dom_snapshot(page, deep_read=needs_deep_read)
            needs_deep_read = False 
            
            prompt = f"""
            Goal: {user_objective}
            
            Past Actions Taken (DO NOT REPEAT THESE):
            {json.dumps(action_history, indent=2)}
            
            Current Screen Elements:
            {json.dumps(dom_data, indent=2)}
            
            You have a new action: "goto". If you are on a blank page or need to jump to a specific website to achieve the goal, use the "goto" action and provide the full "url" (e.g., https://www.amazon.com).
            
            You have a new action: "read". If you have reached a page with the final information but cannot see the paragraph text in the Current Screen Elements, output the "read" action. This will trigger a deep scan of the page's paragraphs on your next turn so you can extract the data.
            
            CRITICAL INSTRUCTION FOR FINISHING:
            If the user's goal requires returning information (e.g., "return the results", "find the price", "summarize"), you MUST read the text from the Current Screen Elements, extract the actual answer, and write the full answer into the 'reason' field of your 'finish' action. 
            DO NOT just say "the results are displayed." Actually provide the data!
            
            CRUCIAL JSON RULE: DO NOT use literal newlines in your JSON output. If you need a newline in the 'reason' string, you MUST use the escaped characters '\\n'.
            
            What is your next logical step?
            """
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
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
                
                # --- THE FIX 2: Send thoughts to the UI! ---
                thought = ai_decision.get("thought", "Thinking...")
                if ui_callback:
                    ui_callback(f"🧠 **Thought:** {thought}")
                    ui_callback(f"⚙️ **Action:** `{action_type}`")
                # -------------------------------------------
                
            except Exception as e:
                print(f"❌ JSON Parse Error: {e}")
                print(f"Raw Output that caused the crash:\n{response.text}")
                step_count += 1
                await asyncio.sleep(5)
                continue
            
            if action_type == "finish":
                final_report = ai_decision['command']['reason']
                break
                
            # --- Handle the READ action ---
            if action_type == "read":
                print("-> AI requested a deep read of the page.")
                needs_deep_read = True
                action_history.append("Triggered a deep read to expose paragraph text.")
                step_count += 1
                await asyncio.sleep(2) 
                continue
                
            if action_type == "goto":
                target_url = ai_decision["command"]["url"]
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
                print(f"-> AI requested direct navigation to: {target_url}")
                await page.goto(target_url)
                action_history.append(f"Navigated directly to {target_url}")
                
                step_count += 1
                await asyncio.sleep(8)
                continue
            
            element_id = ai_decision["command"]["element_id"]
            target_locator = page.locator(f'[data-agent-id="{element_id}"]')
            
            if action_type == "type":
                text_to_type = ai_decision["command"]["text"]
                # Emulate rapid human typing to bypass React/ChatGPT UI blocks
                await target_locator.click()
                await page.keyboard.type(text_to_type, delay=10) 
                await page.keyboard.press("Enter") 
                action_history.append(f"Typed '{text_to_type}' into element ID {element_id} and hit Enter")
                
                # Wait for ChatGPT to finish its generation
                step_count += 1
                await asyncio.sleep(20) 
                continue
                
            elif action_type == "click":
                await target_locator.click()
                action_history.append(f"Clicked element ID {element_id}")
            
            step_count += 1
            await asyncio.sleep(5) 
            
        await asyncio.sleep(2)
        await context.close()
        
        return final_report

if __name__ == "__main__":
    asyncio.run(run_agent("Test agent run"))
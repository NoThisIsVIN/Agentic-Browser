import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    # This must be the EXACT SAME folder name you use in main.py
    user_data_dir = os.path.abspath("agent_profile")
    
    print("==================================================")
    print("🌐 Booting Agent Profile Builder...")
    print(f"📂 Saving session data to: {user_data_dir}")
    print("👉 Log into ChatGPT, Google, or NPTEL now.")
    print("🛑 Close the browser window manually when you are done.")
    print("==================================================")

    async with async_playwright() as p:
        # Launch the persistent browser with stealth mode
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome", # Forces Playwright to use your actual Google Chrome installation
            args=["--disable-blink-features=AutomationControlled"], # Masks the bot signature
            ignore_default_args=["--enable-automation"] # Hides the automation banner
        )
        
        page = context.pages[0]
        
        # Let's go straight to the Google login page to test it
        await page.goto("https://accounts.google.com") 
        
        await context.wait_for_event("close", timeout=0)
        
        print("✅ Browser closed. Profile and cookies saved successfully!")

if __name__ == "__main__":
    asyncio.run(main())
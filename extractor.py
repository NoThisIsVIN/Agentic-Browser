import asyncio
from playwright.async_api import async_playwright

async def extract_dom(url):
    # Start the Playwright browser session
    async with async_playwright() as p:
        # headless=False means you will actually see the browser open
        browser = await p.chromium.launch(headless=False) 
        page = await browser.new_page()
        await page.goto(url)

        # This JavaScript finds interactive elements and tags them with numbers
        js_code = """
        () => {
            let elements = document.querySelectorAll('button, a, input, [role="button"]');
            let simplifiedDOM = [];
            
            elements.forEach((el, index) => {
                let id = index + 1;
                
                // Draw a visual box and number tag on the page for debugging
                el.style.border = '2px solid red'; 
                let tag = document.createElement('span');
                tag.textContent = `[${id}]`;
                tag.style.backgroundColor = 'yellow';
                tag.style.color = 'black';
                tag.style.position = 'absolute';
                tag.style.zIndex = '9999';
                el.parentNode.insertBefore(tag, el);

                // Save the data to pass back to Python
                simplifiedDOM.push({
                    id: id,
                    tag: el.tagName,
                    text: el.innerText || el.value || el.placeholder || 'No Text',
                    type: el.type || 'N/A'
                });
            });
            return simplifiedDOM;
        }
        """
        
        # Execute the JS and wait 3 seconds so you can see the red boxes
        print("Injecting tags into the page...")
        extracted_data = await page.evaluate(js_code)
        await asyncio.sleep(15) 
        
        await browser.close()
        return extracted_data

async def main():
    # We will test this on a dummy e-commerce site
    test_url = "https://www.saucedemo.com/" 
    print(f"Loading {test_url}...")
    
    dom_data = await extract_dom(test_url)
    
    print("\n--- What the AI Sees ---")
    for item in dom_data:
        print(f"Element ID: {item['id']} | Type: {item['tag']} | Text: {item['text']}")

if __name__ == "__main__":
    asyncio.run(main())
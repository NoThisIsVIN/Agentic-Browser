# 🌐 Agentic Browser

An autonomous, AI-driven browser automation agent. By combining Playwright with the Google Gemini API, this agent can autonomously navigate the web, understand page structures, click elements, fill forms, and solve complex objectives—all while streaming its thought process and what it sees back to you.

## ✨ Features

- **Autonomous Browsing:** Feed the agent an objective (e.g., "Find the release date of a game on Wikipedia") and it intelligently executes actions using DOM analysis and element interaction.
- **Gemini API Powered:** Uses Google's Gemini models via `google-genai` for fast, lightweight browser reasoning and action planning. Includes built-in rate-limiting handling.
- **Smart Routing:** Identifies common platforms (like Amazon, Flipkart, Myntra, YouTube) from the prompt and navigates directly to bypass search engines.
- **Persistent Profiles:** Maintain your authentication sessions (Google, ChatGPT, etc.) using `setup_profile.py`, avoiding bot detection and logging in repeatedly.
- **Interactive UIs:** 
  - **Main Web UI:** A snappy FastAPI web interface utilizing real-time server-sent events.
  - **Streamlit UI:** An alternative, easy-to-use Streamlit chat interface (`app.py`).
- **Stealth Capabilities:** Uses Playwright with configurations to mask the bot signature.
- **Structured Reporting:** Capable of returning formatted, structured final reports (e.g., lists of products and prices with links).
- **Form-Aware DOM Mode:** Automatically expands the DOM snapshot on form-heavy pages so fields, labels, required flags, and layout details are easier for the agent to reason about.
- **Downloadable Live Feed:** The web UI can export the live thoughts/actions feed as a Markdown file after a run.

---

## 📂 Project Structure

- **`server.py`** - Main FastAPI backend serving the interactive HTML/JS UI.
- **`main.py`** - The core Playwright agent logic utilizing the Gemini API. Includes rate limit handlers and smart site routing.
- **`schema.py`** - Pydantic models (like `AgentOutput`) defining the structured data contracts for the LLM.
- **`app.py`** - Streamlit-based user interface for cloud executions.
- **`setup_profile.py`** - Helper script to initialize a persistent Playwright browser instance so you can manually log into websites (saving states into `agent_profile/`).
- **`start.bat` / `start.ps1`** - Wrapper scripts establishing the environment and launching the Uvicorn web server easily.
- **`static/` & `templates/`** - Frontend assets (HTML, CSS, JS) for the FastAPI web server.

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.10+
- The Playwright browsers installed:
  ```bash
  playwright install chromium
  ```

### 2. Configuration
Create or modify the `.env` file in the root directory to configure the AI model:

```ini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-lite-preview # Or the model of your choice
GEMINI_RPM_LIMIT=12 # Optional: Set to match your AI Studio project quota
GEMINI_MAX_OUTPUT_TOKENS=65536 # Optional: Clamped to the configured model's output limit
GEMINI_THINKING_LEVEL=minimal # Optional for Gemini 3 models: minimal, low, medium, or high
AGENT_MAX_STEPS=60 # Optional: Max browser decisions per run, capped at 100
```



### 3. Setup Browser Profile (Optional, but Recommended)
If you want the agent to use your accounts (e.g., logged into Google), run the profile builder:

```bash
python setup_profile.py
```
A browser will open. Log into your required websites manually, then close the browser to save your cookies locally in the `agent_profile/` folder.

### 4. Running the Agent

**Web UI (Fastest Way):**
Simply run the included startup script. It will boot the FastAPI server and open the browser interface automatically.

- **Windows:** Double-click `start.bat` or run `.\start.ps1` in PowerShell.

*(Manual execution: `uvicorn server:app --reload --host 127.0.0.1 --port 8000`)*

**Alternative Execution (Streamlit UI):**
```bash
streamlit run app.py
```

## 🧠 How it works
1. **Objective Injection:** The user submits a command.
2. **DOM Snapshotting:** Playwright extracts a sanitized, simplified version of the DOM representing interactable elements (buttons, inputs, links).
3. **Reasoning Loop:** The Gemini LLM evaluates the DOM, formulates a `next_goal`, logs it to its recent actions, and dictates an action (click, type, navigate, wait).
4. **Execution & Streaming:** The backend streams the markdown log and screenshots back to the UI in real-time until the objective is determined as completed.

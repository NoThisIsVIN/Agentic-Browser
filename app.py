import streamlit as st
import asyncio
import sys

# --- THE WINDOWS ASYNC FIX ---
# Streamlit + Windows + Playwright requires this specific event loop policy
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# -----------------------------

# Import the brain you just built!
from main import run_agent 

# 1. Set up the page UI
st.set_page_config(page_title="Web Agent OS", page_icon="🌐", layout="centered")
st.title("🤖 Autonomous Web Agent")
st.markdown("Type a command below. The agent will boot a stealth browser, navigate the web, and execute the task autonomously.")

# 2. Create a memory buffer for the chat interface
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# 3. Display all past messages on the screen
for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 4. The Input Box
if user_command := st.chat_input("E.g., Search Wikipedia for the release date of Counter-Strike 2..."):
    
    # Show the user's prompt on the screen
    with st.chat_message("user"):
        st.markdown(user_command)
    st.session_state.chat_history.append({"role": "user", "content": user_command})

    # Show the AI's response processing
    with st.chat_message("assistant"):
        st.markdown(f"**Target Acquired:** `{user_command}`")
        
        with st.status("Agent Deployed to the Web...", expanded=True) as status:
            st.write("Booting Playwright and connecting to Gemini API...")
            
            # --- THE FIX: The Callback Function ---
            # This function catches the thoughts from main.py and prints them live
            def stream_to_ui(message):
                st.markdown(message)
            # --------------------------------------
            
            # THIS IS THE MAGIC LINE. We now pass the ui_callback so the agent can talk back to the UI.
            agent_result = asyncio.run(run_agent(user_command, ui_callback=stream_to_ui))
            
            status.update(label="Task Executed Successfully!", state="complete", expanded=False)
            
        # Display what the AI actually figured out!
        st.markdown(f"✅ **Agent Report:** {agent_result}")
        
    st.session_state.chat_history.append({"role": "assistant", "content": f"✅ **Agent Report:** {agent_result}"})
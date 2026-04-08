import asyncio
import sys
from pathlib import Path

import streamlit as st

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from main import run_agent

st.set_page_config(page_title="Web Agent OS (Local)", layout="centered")
st.title("Autonomous Web Agent (Local)")
st.markdown(
    "Type a command below. This version runs the browser agent against a local Ollama model."
)

if "local_chat_history" not in st.session_state:
    st.session_state.local_chat_history = []

for message in st.session_state.local_chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if user_command := st.chat_input("E.g., Search YouTube for qwen3 ollama setup..."):
    with st.chat_message("user"):
        st.markdown(user_command)
    st.session_state.local_chat_history.append({"role": "user", "content": user_command})

    with st.chat_message("assistant"):
        st.markdown(f"**Target Acquired:** `{user_command}`")

        with st.status("Agent Deployed to the Web...", expanded=True) as status:
            st.write("Booting Playwright and connecting to local Ollama...")

            def stream_to_ui(message, image_bytes=None):
                st.markdown(message)
                if image_bytes:
                    st.image(image_bytes, caption="What the agent sees", use_container_width=True)

            agent_result = asyncio.run(run_agent(user_command, ui_callback=stream_to_ui))

            status.update(label="Task Executed Successfully!", state="complete", expanded=False)

        st.markdown(f"**Agent Report:** {agent_result}")

    st.session_state.local_chat_history.append(
        {"role": "assistant", "content": f"**Agent Report:** {agent_result}"}
    )

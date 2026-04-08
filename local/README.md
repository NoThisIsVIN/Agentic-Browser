# Local Ollama Variant

This folder keeps a separate local-LLM version of the browser agent so the repo root can stay on the Gemini implementation.

Run it with:

```powershell
streamlit run local/app.py
```

Useful environment variables in `.env`:

```env
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_VISION_MODEL=qwen2.5vl:7b
```

Notes:

- `OLLAMA_MODEL` is the main planning model.
- `OLLAMA_VISION_MODEL` is optional. If you leave it blank, the local agent will stay text-first and fall back to deeper reads instead of screenshots.
- The local app still uses the shared `agent_profile` browser profile from the project root.

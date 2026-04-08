import asyncio
import base64
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import HTTPException
from pydantic import BaseModel, Field

from main import run_agent as gemini_run_agent

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    from local.main import run_agent as local_run_agent
    LOCAL_IMPORT_ERROR = None
except Exception as exc:
    local_run_agent = None
    LOCAL_IMPORT_ERROR = exc

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Agentic Browser UI")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class AgentRequest(BaseModel):
    objective: str = Field(min_length=1, description="The task to run in the browser.")
    backend: Literal["local", "gemini"] = "gemini"
    keep_browser_open: bool = False


def _default_backend():
    gemini_configured = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    if gemini_configured:
        return "gemini"
    if local_run_agent is not None:
        return "local"
    return "gemini"


def _backend_metadata():
    gemini_configured = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    return {
        "local": {
            "label": "Local Ollama",
            "status": "Ready" if local_run_agent is not None else f"Unavailable: {LOCAL_IMPORT_ERROR}",
            "model": os.getenv("OLLAMA_MODEL", "qwen3:8b"),
            "available": local_run_agent is not None,
            "accent": "Local",
        },
        "gemini": {
            "label": "Gemini API",
            "status": "Configured" if gemini_configured else "Missing API key",
            "model": os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview"),
            "available": gemini_configured,
            "accent": "Cloud",
        },
    }


def _runner_for_backend(backend: str):
    if backend == "gemini":
        return gemini_run_agent
    if local_run_agent is None:
        raise HTTPException(status_code=400, detail="Local Ollama backend is not available in this environment.")
    return local_run_agent


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "backends": _backend_metadata(),
            "default_backend": _default_backend(),
        },
    )


@app.get("/api/config")
async def config():
    return {
        "backends": _backend_metadata(),
        "default_backend": _default_backend(),
    }


@app.post("/api/run")
async def run_agent_stream(payload: AgentRequest):
    runner = _runner_for_backend(payload.backend)

    async def event_stream():
        queue: asyncio.Queue[dict] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def ui_callback(message, image_bytes=None):
            event = {
                "type": "update",
                "message": message,
            }
            if image_bytes:
                event["image"] = base64.b64encode(image_bytes).decode("ascii")
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def run_agent_in_thread():
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            return asyncio.run(
                runner(
                    payload.objective,
                    ui_callback=ui_callback,
                    keep_browser_open=payload.keep_browser_open,
                )
            )

        async def run_task():
            try:
                result = await asyncio.to_thread(run_agent_in_thread)
                await queue.put({"type": "result", "message": result})
            except Exception as exc:
                await queue.put({"type": "error", "message": str(exc)})
            finally:
                await queue.put({"type": "done"})

        task = asyncio.create_task(run_task())
        try:
            while True:
                item = await queue.get()
                yield json.dumps(item) + "\n"
                if item["type"] == "done":
                    break
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

import json
import os
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_PATH = os.getenv(
    "SYSTEM_PROMPT_PATH",
    os.path.join(os.path.dirname(__file__), "SYSTEM_PROMPT.txt"),
)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")


# ---------------------------------------------------------------------------
# System prompt loading
# ---------------------------------------------------------------------------
def load_system_prompt() -> str:
    """Load the system prompt from file."""
    try:
        with open(SYSTEM_PROMPT_PATH, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.error(f"System prompt file not found at {SYSTEM_PROMPT_PATH}")
        raise


SYSTEM_PROMPT = load_system_prompt()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage HTTP client lifecycle."""
    app.state.http_client = httpx.AsyncClient(timeout=300.0)
    logger.info("Middleware started")
    yield
    await app.state.http_client.aclose()
    logger.info("Middleware shutting down")


app = FastAPI(title="OpenWebUI Middleware", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check — always 200 if the process is running."""
    return {"status": "healthy", "service": "openwebui-middleware"}


@app.get("/ready")
async def ready():
    """Readiness check — verifies system prompt is loaded and OpenAI is reachable."""
    try:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        url = f"{OPENAI_BASE_URL}/models"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

        return {
            "status": "ready",
            "openai_reachable": True,
            "system_prompt_loaded": bool(SYSTEM_PROMPT),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Not ready: {str(e)}")


@app.get("/v1/models")
async def list_models(request: Request):
    """Proxy the OpenAI models endpoint."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    url = f"{OPENAI_BASE_URL}/models"
    try:
        response = await request.app.state.http_client.get(url, headers=headers)
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint with system prompt injection."""
    request_id = f"req_{int(time.time() * 1000)}"

    logger.info(json.dumps({
        "event": "request_received",
        "request_id": request_id,
        "model": body.model,
        "message_count": len(body.messages),
    }))

    modified_messages = inject_system_prompt(body.messages)

    logger.info(json.dumps({
        "event": "prompt_injected",
        "request_id": request_id,
        "modified_count": len(modified_messages),
    }))

    payload = body.model_dump(exclude_none=True)
    payload["messages"] = modified_messages

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{OPENAI_BASE_URL}/chat/completions"

    if body.stream:
        return StreamingResponse(
            stream_openai_response(request.app.state.http_client, url, headers, payload, request_id),
            media_type="text/event-stream",
        )

    try:
        response = await request.app.state.http_client.post(url, json=payload, headers=headers)
        response.raise_for_status()

        logger.info(json.dumps({
            "event": "request_complete",
            "request_id": request_id,
        }))

        return JSONResponse(content=response.json())

    except httpx.HTTPStatusError as e:
        logger.error(json.dumps({
            "event": "upstream_http_error",
            "request_id": request_id,
            "status_code": e.response.status_code,
        }))
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    except httpx.RequestError as e:
        logger.error(json.dumps({
            "event": "upstream_connection_error",
            "request_id": request_id,
        }))
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------


async def stream_openai_response(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    request_id: str,
) -> AsyncIterator[bytes]:
    """Stream response bytes from OpenAI back to the caller."""
    logger.info(json.dumps({"event": "upstream_request_start", "request_id": request_id}))
    start_time = time.time()

    try:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                error = await response.aread()
                logger.error(json.dumps({
                    "event": "upstream_error",
                    "request_id": request_id,
                    "status_code": response.status_code,
                }))
                raise HTTPException(status_code=response.status_code, detail=error.decode())

            async for chunk in response.aiter_bytes():
                yield chunk

        duration = time.time() - start_time
        logger.info(json.dumps({
            "event": "upstream_request_complete",
            "request_id": request_id,
            "duration_seconds": round(duration, 3),
        }))

    except httpx.RequestError as e:
        logger.error(json.dumps({
            "event": "upstream_connection_error",
            "request_id": request_id,
        }))
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Core injection logic
# ---------------------------------------------------------------------------


def inject_system_prompt(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Inject system prompt as the sole system message at index 0.
    Any existing system messages are stripped to prevent Open WebUI's
    own context from conflicting with our extraction instructions.

    Args:
        messages: Original message array from request

    Returns:
        Modified message array with our system prompt as the only system message
    """
    non_system = [msg for msg in messages if msg.get("role") != "system"]
    return [{"role": "system", "content": SYSTEM_PROMPT}] + non_system

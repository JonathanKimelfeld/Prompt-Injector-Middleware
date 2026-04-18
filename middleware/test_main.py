import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager
from fastapi.testclient import TestClient

import httpx
from main import app, inject_system_prompt, SYSTEM_PROMPT, load_system_prompt, stream_openai_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_openai_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "Test response"}}],
    }
    mock.raise_for_status = MagicMock()
    return mock


@pytest.fixture
def mock_models_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"object": "list", "data": [{"id": "gpt-4"}, {"id": "gpt-3.5-turbo"}]}
    mock.raise_for_status = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# Health / readiness endpoint tests
# ---------------------------------------------------------------------------


def test_health_endpoint(client):
    """Should return 200 with status=healthy."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["service"] == "openwebui-middleware"


def test_ready_endpoint_openai_reachable(client):
    """Should return 200 when OpenAI is reachable."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        response = client.get("/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["openai_reachable"] is True
    assert data["system_prompt_loaded"] is True


def test_ready_endpoint_openai_unreachable(client):
    """Should return 503 when OpenAI is not reachable."""
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.RequestError("timeout"))):
        response = client.get("/ready")

    assert response.status_code == 503
    assert "Not ready" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /v1/models endpoint tests
# ---------------------------------------------------------------------------


def test_list_models_success(client, mock_models_response):
    """Should proxy the models list from OpenAI."""
    with patch.object(client.app.state.http_client, "get", new=AsyncMock(return_value=mock_models_response)):
        response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["object"] == "list"


def test_list_models_upstream_error(client):
    """Should return 502 when OpenAI models endpoint fails."""
    with patch.object(client.app.state.http_client, "get", new=AsyncMock(side_effect=Exception("connection failed"))):
        response = client.get("/v1/models")

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# /v1/chat/completions endpoint tests
# ---------------------------------------------------------------------------


def test_non_streaming_completion_injects_system_prompt(client, mock_openai_response):
    """System prompt must be the first message sent to OpenAI."""
    mock_post = AsyncMock(return_value=mock_openai_response)
    with patch.object(client.app.state.http_client, "post", new=mock_post):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 200
    sent_messages = mock_post.call_args.kwargs["json"]["messages"]
    assert sent_messages[0]["role"] == "system"
    assert SYSTEM_PROMPT in sent_messages[0]["content"]


def test_non_streaming_completion_returns_openai_response(client, mock_openai_response):
    """Should pass OpenAI's response body back unchanged."""
    with patch.object(client.app.state.http_client, "post", new=AsyncMock(return_value=mock_openai_response)):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-123"


def test_chat_completions_handles_upstream_http_error(client):
    """Should return the upstream status code on OpenAI HTTP errors."""
    error_response = MagicMock()
    error_response.status_code = 429
    error_response.text = "Rate limit exceeded"

    with patch.object(
        client.app.state.http_client, "post",
        new=AsyncMock(side_effect=httpx.HTTPStatusError("error", request=MagicMock(), response=error_response))
    ):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 429


def test_chat_completions_handles_connection_error(client):
    """Should return 502 on network/connection failures."""
    with patch.object(
        client.app.state.http_client, "post",
        new=AsyncMock(side_effect=httpx.RequestError("connection refused"))
    ):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 502


def test_chat_completions_logs_lifecycle(client, mock_openai_response):
    """Should log request_received, prompt_injected, and request_complete events."""
    with patch.object(client.app.state.http_client, "post", new=AsyncMock(return_value=mock_openai_response)):
        with patch("main.logger") as mock_logger:
            client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            )

    log_calls = [call.args[0] for call in mock_logger.info.call_args_list]
    events = [json.loads(msg).get("event") for msg in log_calls if msg.startswith("{")]
    assert "request_received" in events
    assert "prompt_injected" in events
    assert "request_complete" in events


def test_chat_completions_invalid_body(client):
    """Should return 422 for missing required fields."""
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hi"}]},  # missing 'model'
    )
    assert response.status_code == 422


def test_streaming_endpoint_returns_event_stream(client):
    """stream=True should return a text/event-stream response."""
    chunk = b"data: {}\n\n"

    async def fake_stream(*args, **kwargs):
        yield chunk

    with patch("main.stream_openai_response", side_effect=fake_stream):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert response.content == chunk


# ---------------------------------------------------------------------------
# stream_openai_response unit tests (async generator tested directly)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stream_yields_chunks():
    """Should yield all bytes chunks received from upstream."""
    chunks = [b"chunk1", b"chunk2", b"chunk3"]

    mock_response = MagicMock()
    mock_response.status_code = 200

    async def aiter_bytes():
        for c in chunks:
            yield c

    mock_response.aiter_bytes = aiter_bytes

    @asynccontextmanager
    async def mock_stream(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = mock_stream

    result = []
    async for chunk in stream_openai_response(mock_client, "http://x", {}, {}, "req_test"):
        result.append(chunk)

    assert result == chunks


@pytest.mark.anyio
async def test_stream_raises_on_non_200():
    """Should raise HTTPException when upstream returns non-200."""
    from fastapi import HTTPException as FastAPIHTTPException

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.aread = AsyncMock(return_value=b"Rate limited")

    @asynccontextmanager
    async def mock_stream(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = mock_stream

    with pytest.raises(FastAPIHTTPException) as exc_info:
        async for _ in stream_openai_response(mock_client, "http://x", {}, {}, "req_test"):
            pass

    assert exc_info.value.status_code == 429


@pytest.mark.anyio
async def test_stream_raises_502_on_request_error():
    """Should raise HTTPException 502 on network failure."""
    from fastapi import HTTPException as FastAPIHTTPException

    mock_client = MagicMock()
    mock_client.stream = MagicMock(side_effect=httpx.RequestError("network down"))

    with pytest.raises((FastAPIHTTPException, httpx.RequestError)):
        async for _ in stream_openai_response(mock_client, "http://x", {}, {}, "req_test"):
            pass


# ---------------------------------------------------------------------------
# inject_system_prompt unit tests
# ---------------------------------------------------------------------------


def test_inject_into_empty_messages():
    """Should add system message at beginning when none exists."""
    messages = [{"role": "user", "content": "Hello"}]
    result = inject_system_prompt(messages)

    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert SYSTEM_PROMPT in result[0]["content"]
    assert result[1] == messages[0]


def test_inject_replaces_existing_system_message():
    """Should replace existing system message to prevent Open WebUI context conflicts."""
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    result = inject_system_prompt(messages)

    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert result[0]["content"] == SYSTEM_PROMPT
    assert "You are helpful." not in result[0]["content"]


def test_inject_preserves_conversation():
    """Should preserve all non-system messages in original order."""
    messages = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Response"},
        {"role": "user", "content": "Second"},
    ]
    result = inject_system_prompt(messages)

    assert len(result) == 4
    assert result[0]["role"] == "system"
    assert result[1:] == messages


def test_inject_strips_all_existing_system_messages():
    """Should remove all existing system messages and insert only ours."""
    messages = [
        {"role": "system", "content": "First system."},
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Second system."},
    ]
    result = inject_system_prompt(messages)

    assert len(result) == 2  # our system + 1 user (both old system msgs stripped)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == SYSTEM_PROMPT
    assert result[1] == {"role": "user", "content": "Hi"}


def test_inject_empty_messages_list():
    """Should handle an empty messages array gracefully."""
    result = inject_system_prompt([])

    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert result[0]["content"] == SYSTEM_PROMPT


def test_inject_does_not_mutate_input():
    """Should not modify the original messages list."""
    messages = [{"role": "user", "content": "Hello"}]
    original = [{"role": "user", "content": "Hello"}]
    inject_system_prompt(messages)

    assert messages == original


def test_system_prompt_is_first_message():
    """Injected system message must always be at index 0."""
    messages = [
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Hello"},
    ]
    result = inject_system_prompt(messages)

    assert result[0]["role"] == "system"


def test_load_system_prompt_missing_file(monkeypatch, tmp_path):
    """Should raise FileNotFoundError when prompt file does not exist."""
    import main

    monkeypatch.setattr(main, "SYSTEM_PROMPT_PATH", str(tmp_path / "missing.txt"))

    with pytest.raises(FileNotFoundError):
        main.load_system_prompt()

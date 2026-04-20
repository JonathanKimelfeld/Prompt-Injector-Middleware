# Open WebUI + Middleware Assignment

This app consists of a FastAPI middleware proxy sitting between Open WebUI and OpenAI, transparently injecting a system prompt that turns GPT into a structured data extraction engine.

This app is local and supports:
1) Transformation of any unstructured text into structured JSON.
2) The model stays in conversational mode; Asking follow-up questions regarding the previous message (which contained unstructured text), until a new message with unstructured text requiring transformation is sent.

# What's the middleware for?

We could configure the system prompt in Open WebUI's UI. 
But that's defined per-user and can be changed or cleared.
What I do here instead, is an infra-level choice that allows every message we send in Open WebUI to be intercepted by the middleware before reaching OpenAI:

1) Open WebUI  →  req. POST /v1/chat/completions  →  Middleware
2) Middleware dismisses any existing system message
3) Middleware prepends the extraction system prompt (as the only system prompt)
4) Modified request forwarded to OpenAI
5) Response is streamed back

The middleware is invisible — it modifies only the request.

## Quick Start

**Prerequisites:** Docker Desktop, an OpenAI API key.

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd flax_assigment

# 2. Configure your API key
cp .env.example .env
echo "OPENAI_API_KEY=sk-..." >> .env   # replace with your key

# 3. Start the full stack
docker-compose up -d

# 4. Wait ~30 seconds, then open the UI
open http://localhost:3000
```


### Email extraction example

If pasting into the chat:
```
From: sarah@startup.io
To: john@company.com
Subject: Meeting Reschedule

Hi John, can we move our 1:1 from Tuesday to Wednesday at 3pm?
I have a conflict. Thanks! — Sarah
```

Expected response:

```json
{
  "document_type": "email",
  "confidence": 0.95,
  "language": "en",
  "extraction_notes": null,
  "extracted_data": {
    "fields": {
      "sender": "sarah@startup.io",
      "recipients": ["john@company.com"],
      "cc": [],
      "subject": "Meeting Reschedule",
      "date_sent": null,
      "body_summary": "Sarah asks John to reschedule their 1:1 from Tuesday to Wednesday at 3pm due to a conflict.",
      "action_items": ["Reschedule 1:1 from Tuesday to Wednesday at 3pm"],
      "sentiment": "neutral"
    },
    "fields_confidence": {
      "sender": 1.0,
      "recipients": 1.0,
      "cc": 1.0,
      "subject": 1.0,
      "date_sent": 1.0,
      "body_summary": 0.95,
      "action_items": 0.9,
      "sentiment": 0.85
    }
  }
}

# API Endpoints

| Method | Endpoint | Description 
|--------|----------|-------------
| `GET`  | `/health`| Always 200 if the process is running 
| `GET`  | `/ready` | 200 if OpenAI is reachable; 503 otherwise 
| `GET`  | `/v1/models` | Proxied model list from OpenAI 
| `POST` | `/v1/chat/completions` | Injects system prompt then proxies 


## File Structure

```
flax_assigment/
├── README.md                  # Setup instructions and design decisions
├── SYSTEM_PROMPT.md           # System prompt + prompt engineering explanation
├── docker-compose.yml         # Full stack orchestration
├── .env.example               # Configuration template
├── .env                       # Your secrets (git-ignored)
├── .gitignore
└── middleware/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py                # FastAPI app — proxy + injection logic
    ├── SYSTEM_PROMPT.txt      # The extraction system prompt (loaded at runtime)
    ├── test_main.py           # Unit tests (23 tests, 100% coverage)
    └── pytest.ini
```


### A service shows "unhealthy" in `docker-compose ps`
```bash
# View that service's logs
docker-compose logs middleware
docker-compose logs db
docker-compose logs webui
```

## Running the Unit Tests

```bash
cd middleware
source venv/bin/activate
pytest test_main.py -v --cov=main --cov-report=term-missing
```

Or inside the running container:
```bash
docker exec openwebui-middleware pytest test_main.py -v --cov=main
```

Expected: 23 tests pass, 100% coverage.

## Design Decisions

**FastAPI for middleware**
Native async/await made streaming straightforward — `StreamingResponse` passes chunks back to Open WebUI as they arrive from OpenAI. Pydantic validates the request body automatically, and `/docs` gives a free interactive API explorer.

**Replace system messages, don't prepend**
Open WebUI injects its own system context on every request. When I prepended the prompt, gpt-4o saw two system messages and followed Open WebUI's ("code interpreter" behavior) instead of ours. Stripping all existing system messages and inserting only ours means the model has exactly one set of instructions.

**PostgreSQL over SQLite**
Open WebUI's recommended choice for concurrent access.
SQLite would work for a demo but serialises writes, which causes issues once multiple browser tabs are open. 

**Docker Compose over Kubernetes**
Single `docker-compose up -d` to run the whole stack — no cluster, no Helm charts, no cloud account needed. The assignment asks for easy local setup, not production-grade orchestration.

**JSON-only output**
Any non-JSON character before the opening `{` breaks `json.loads()` with no graceful fallback. The system prompt explicitly tells the model it is being parsed by a machine.

**Per-field confidence scores**
A single top-level score tells you the document is "0.7 confident" but not which field is uncertain. The `fields_confidence` parallel object gives per-field visibility, allowing us to view and route low-confidence documents for human review.

**300s HTTP client timeout**
Streaming a long GPT-4 response over a slow connection can take well over the default 5s timeout. A mid-stream timeout closes the connection and the user sees a partial response with no error. 300s is generous enough to never trigger in normal use.


## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    User Browser                      │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP  (port 3000)
                      ▼
┌─────────────────────────────────────────────────────┐
│              Open WebUI                             │
│  Chat interface · User accounts · History           │
└──────────┬──────────────────────┬───────────────────┘
           │ PostgreSQL            │ OpenAI-compatible API
           ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────┐
│   PostgreSQL 15  │   │   Middleware  (port 8000)    │
│  Users · History │   │  Injects system prompt       │
│  Settings        │   │  Proxies to OpenAI           │
└──────────────────┘   │  Streams responses           │
                       └──────────────┬──────────────┘
                                      │ HTTPS
                                      ▼
                       ┌─────────────────────────────┐
                       │         OpenAI API           │
                       │   gpt-4o · gpt-3.5-turbo    │
                       └─────────────────────────────┘
```

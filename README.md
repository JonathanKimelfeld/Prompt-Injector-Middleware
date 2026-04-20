# Open WebUI + Middleware

A local AI stack that transforms any unstructured text into structured JSON. A FastAPI middleware proxy sits between [Open WebUI](https://github.com/open-webui/open-webui) and OpenAI, transparently injecting a system prompt that turns GPT into a structured data extraction engine.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    User Browser                      │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP  (port 3000)
                      ▼
┌─────────────────────────────────────────────────────┐
│              Open WebUI  v0.6.5                      │
│  Chat interface · User accounts · History            │
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

## Quick Start

**Prerequisites:** Docker Desktop, an OpenAI API key.

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd flax_assigment

# 2. Configure your API key
cp .env.example .env
echo "OPENAI_API_KEY=sk-..." >> .env   # replace with your real key

# 3. Start the full stack
docker-compose up -d

# 4. Wait ~30 seconds, then open the UI
open http://localhost:3000
```

That's it. Create an account, select a model, and paste any unstructured text.

## What the Middleware Does

Every message you send in Open WebUI is intercepted by the middleware before reaching OpenAI:

```
1. Open WebUI  →  POST /v1/chat/completions  →  Middleware
2. Middleware strips any existing system message
3. Middleware prepends the extraction system prompt
4. Modified request forwarded to OpenAI
5. Response (streaming or not) passed back unchanged
```

The user sees a normal chat interface. The middleware is invisible — it never modifies the response, only the request.

## Testing Examples

### Email extraction

Paste this into the chat:

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
```

### Receipt extraction

```
Cafe Central
456 Main Street, Springfield
2025-04-15  09:23
Cappuccino      $5.00
Croissant       $4.50
Tax (8%)        $0.76
Total          $10.26
Card ****5678
```

Expected response:

```json
{
  "document_type": "receipt",
  "confidence": 0.9,
  "language": "en",
  "extraction_notes": "Subtotal not explicitly listed; inferred from items.",
  "extracted_data": {
    "fields": {
      "merchant_name": "Cafe Central",
      "merchant_address": "456 Main Street, Springfield",
      "date": "2025-04-15",
      "time": "09:23",
      "items": [
        {"description": "Cappuccino", "quantity": 1, "unit_price": 5.00, "total": 5.00},
        {"description": "Croissant",  "quantity": 1, "unit_price": 4.50, "total": 4.50}
      ],
      "subtotal": 9.50,
      "tax": 0.76,
      "tip": null,
      "total": 10.26,
      "currency": "USD",
      "payment_method": "credit",
      "card_last_four": "5678"
    },
    "fields_confidence": {
      "merchant_name": 1.0,
      "merchant_address": 1.0,
      "date": 1.0,
      "time": 1.0,
      "items": 1.0,
      "subtotal": 0.85,
      "tax": 1.0,
      "tip": 1.0,
      "total": 1.0,
      "currency": 0.9,
      "payment_method": 0.85,
      "card_last_four": 1.0
    }
  }
}
```

### Job listing extraction

```
Senior Backend Engineer — Acme Corp
Location: Remote (US timezone)
Salary: $140,000–$180,000/year  |  Full-time
Requirements: 5+ years Python, PostgreSQL, AWS
Nice to have: Kafka
Apply by April 30, 2025: careers@acme.com
```

### Follow-up questions

After any extraction, ask a natural language question:

```
Who sent that email?
```

Response (plain text, not JSON):

```
Sarah from sarah@startup.io sent it.
```

The model stays in conversational mode as long as questions refer to the previous extraction. Pasting new document text triggers a fresh extraction.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/health` | Always 200 if the process is running |
| `GET`  | `/ready`  | 200 if OpenAI is reachable; 503 otherwise |
| `GET`  | `/v1/models` | Proxied model list from OpenAI |
| `POST` | `/v1/chat/completions` | Main endpoint — injects system prompt then proxies |

### Direct curl test

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-3.5-turbo",
    "temperature": 0,
    "messages": [{
      "role": "user",
      "content": "From: alice@co.com\nTo: bob@co.com\nSubject: Hello\n\nCan we meet Friday?"
    }]
  }'
```

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

## Troubleshooting

### "Readiness check failed" / 503 on `/ready`
Your OpenAI API key is missing or invalid.
```bash
# Check the key is set
docker exec openwebui-middleware env | grep OPENAI_API_KEY

# Restart with the correct key
docker-compose down
echo "OPENAI_API_KEY=sk-..." > .env
docker-compose up -d
```

### A service shows "unhealthy" in `docker-compose ps`
```bash
# View that service's logs
docker-compose logs middleware
docker-compose logs db
docker-compose logs webui
```

### No models showing in Open WebUI
The middleware is not reachable from Open WebUI. Check:
```bash
curl http://localhost:8000/v1/models  # should return a model list
docker-compose ps                     # all services should be healthy
```
In Open WebUI go to **Settings → Admin → Connections** and confirm the OpenAI URL is `http://middleware:8000/v1`.

### Getting conversational responses instead of JSON
Open WebUI may have injected its own system prompt that conflicts. The middleware replaces all system messages, so this usually resolves after a page refresh or starting a new chat. If it persists, check the middleware logs:
```bash
docker-compose logs middleware | grep "prompt_injected"
```

### Port already in use
```bash
# Free port 8000
lsof -ti:8000 | xargs kill -9

# Free port 3000
lsof -ti:3000 | xargs kill -9
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

| Decision | Rationale |
|----------|-----------|
| FastAPI for middleware | Native async/await for streaming; Pydantic for request validation; auto-generated docs at `/docs` |
| Replace system messages, don't prepend | Open WebUI injects its own context which confuses models like gpt-4o; owning the system message entirely ensures reliable extraction |
| PostgreSQL over SQLite | More realistic production setup; Open WebUI recommends it for concurrency |
| Docker Compose over Kubernetes | Assignment requires easy local setup — single `docker-compose up -d` |
| JSON-only output | Any non-JSON prefix breaks downstream parsing; the system prompt explicitly forbids preamble |
| Confidence scores | Makes uncertainty explicit and machine-readable; downstream consumers can filter low-confidence extractions |
| 300s HTTP client timeout | Streaming responses from GPT-4 can be slow; short timeouts cause mid-stream failures |

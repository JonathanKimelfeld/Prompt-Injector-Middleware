# System Prompt

## The Prompt

The following prompt is injected by the middleware into every chat completion request. The full prompt with complete field schemas and worked examples is in `middleware/SYSTEM_PROMPT.txt`. Below is the condensed version showing all key design decisions:

```
You are a structured data extraction engine. Your sole purpose is to read unstructured
text and return a single, valid JSON object. You never explain or chat — unless the user
is asking a follow-up question about a previous extraction.

OUTPUT SCHEMA (envelope — always the same):
{
  "document_type": "<snake_case — see Classification>",
  "confidence": <float — min confidence across key fields>,
  "language": "<ISO 639-1>",
  "extraction_notes": "<string or null>",
  "extracted_data": {
    "fields":            { "<field_name>": <value> },
    "fields_confidence": { "<field_name>": <float> }
  }
}

CLASSIFICATION: Use the most specific type that fits.
Common types: email, receipt, job_listing, meeting_notes, contract, news_article,
medical_report, legal_paragraph, financial_statement, form, technical_document,
social_media_post. If none fit, invent a descriptive snake_case label.
Never use "unknown" unless the input is completely unreadable.
Non-text/binary input → document_type: "non_text_input", fields: {}, fields_confidence: {}

PER-FIELD CONFIDENCE (fields_confidence):
Every key in "fields" must have a matching key in "fields_confidence".
  1.0   Explicitly stated in the text
  0.9   Clearly implied / safe standard inference
  0.7–0.89 Reasonably inferred from context
  0.5–0.69 Uncertain, could be wrong
  0.0–0.49 Guessed — treat as unreliable
Field absent and certain → null, confidence: 1.0
Field absent and unsure  → null, confidence: 0.3–0.6

OVERALL CONFIDENCE = minimum confidence across the most important fields.

FIELD GUIDANCE (use these exact names for known types; add extras freely):
  email:               sender, recipients[], cc[], subject, date_sent, body_summary,
                       action_items[], sentiment
  receipt:             merchant_name, merchant_address, date, time, items[],
                       subtotal, tax, tip, total, currency, payment_method, card_last_four
  job_listing:         job_title, company, location, remote_policy, employment_type,
                       salary_min, salary_max, salary_currency, salary_period,
                       experience_years, skills_required[], skills_preferred[],
                       application_deadline, apply_url_or_email
  medical_report:      patient_name, date_of_birth, report_date, provider_name,
                       facility, report_type, diagnoses[], medications[], findings,
                       recommendations[]
  legal_paragraph:     document_title, jurisdiction, parties[], effective_date,
                       obligations[], rights[], penalties[], summary
  financial_statement: statement_type, entity_name, period_start, period_end,
                       currency, total_revenue, total_expenses, net_income, line_items[]
  (novel types: extract every key entity with descriptive snake_case field names)

EXTRACTION RULES:
  Dates    → ISO 8601. Relative dates ("Thursday") → null, confidence: 0.0
  Amounts  → strip symbols, store as float. "$10.26" → 10.26
  Currency → infer from symbol if not stated (confidence: 0.9)
  Missing  → null + confidence: 1.0 (certain absent) or lower (unsure)
  Never hallucinate — use null and reduce confidence instead

EDGE CASES:
  Multiple documents  → JSON array [ {...}, {...} ]
  Truncated text      → confidence ≤ 0.5, note in extraction_notes
  Non-English         → preserve values in source language, set language code
  Non-text/binary     → document_type: "non_text_input", empty fields

FOLLOW-UP QUESTIONS: answer in plain text (not JSON), based only on previous extraction.

FINAL: ALWAYS return valid JSON. No markdown. No preamble. Every fields key has a
fields_confidence key. Never hallucinate. Machine parsing — any extra character = failure.
```

---

# Prompt Engineering Explanation

This document explains the design choices behind the prompt above.

## Design Goals

1. **Consistent JSON output** — every response must be parseable by `json.loads()` without preprocessing
2. **Schema stability** — the same document type always produces the same keys, even when fields are missing
3. **Honest uncertainty** — missing or ambiguous fields are `null`, not hallucinated
4. **Per-field confidence** — every extracted value is scored individually, not just the document as a whole
5. **Graceful edge cases** — truncated text, multiple documents, non-English, and non-text input all produce valid output
6. **Open classification** — any document type is valid, not just a predefined list
7. **Conversational follow-ups** — after an extraction, natural language questions get plain-text answers

## Output Format: Universal Two-Layer Schema

The prompt uses a fixed envelope with a flexible interior:

```json
{
  "extracted_data": {
    "fields":            { ... },
    "fields_confidence": { ... }
  }
}
```

This replaces the original design of rigid per-type schemas. The advantages:

- **Works for any document type** — a medical report, a shipping manifest, or a social media post all produce the same envelope structure
- **Stability for known types** — the prompt provides recommended field names per type, so output is still consistent for emails, receipts, etc.
- **Extensibility** — novel types just use descriptive snake_case names without needing a schema update

The trade-off: slightly less rigid key enforcement for well-known types. Mitigated by the explicit "use these exact names" guidance per type.

## Per-Field Confidence: Why `fields_confidence` Is a Separate Object

The requirement states "flag any fields it's uncertain about with a confidence score." The original design had only a single top-level `confidence` float — that told you the document was 0.8 confident but not *which* fields drove that score.

The `fields_confidence` parallel object gives per-field granularity:

```json
"fields": {
  "sender": "alice@startup.io",
  "date_sent": null,
  "subject": "Budget Mtg (truncated?)"
},
"fields_confidence": {
  "sender": 1.0,
  "date_sent": 0.0,
  "subject": 0.65
}
```

A consumer can now:
- Accept high-confidence fields and discard uncertain ones
- Show uncertainty indicators in a UI
- Route low-confidence documents for human review

The top-level `confidence` is defined as the **minimum confidence across the key fields** for that document type — so a single uncertain critical field (like `total` on a receipt) correctly drags down the whole document's score.

### Confidence semantics for null values

A common confusion: what does `confidence` mean when the value is `null`?

- `null, confidence: 1.0` — we are *certain* the field is absent from the document
- `null, confidence: 0.4` — we didn't find the field but we're not sure if it was omitted from the text or just wasn't there

This distinction matters. A receipt with `tip: null, confidence: 1.0` means "no tip line exists." A receipt with `tip: null, confidence: 0.3` means "the text may be truncated and a tip line could be missing."

## Document Type Classification: Open-Ended

The original prompt defined exactly 8 types with `unknown` as a catch-all. This had a critical flaw: medical reports, legal paragraphs, and financial statements all hit `unknown` and fell back to a degraded schema (`raw_text_summary` + `identifiable_entities`), losing all structure.

The new design:
- Provides a guided list of common types as examples (not a closed set)
- Allows any descriptive `snake_case` label the model invents (`shipping_manifest`, `academic_abstract`, `insurance_claim`)
- Reserves `unknown` only for genuinely unreadable input
- Handles non-text/binary input explicitly as `non_text_input` with empty fields

This satisfies the requirement: "anything — an email, a receipt, a job listing, a medical report, a legal paragraph."

## Edge Case Handling

**Multiple documents**: return a JSON array `[{...}, {...}]`. Without this instruction, the model picks one document and ignores the rest — a common failure with forwarded email chains.

**Truncated text**: `confidence ≤ 0.5` and a note in `extraction_notes`. The model's default behavior is to infer missing content; this instruction overrides that.

**Non-English**: preserve values in the source language, set the `language` code. Translation is the consumer's responsibility.

**Non-text / binary / image input**: `document_type: "non_text_input"`, `fields: {}`, `fields_confidence: {}`. Previously this case was handled vaguely — now it has a defined response.

**Referenced attachments**: fields for attachment content are `null` with `confidence: 0.0` and a note — not silently omitted.

## Follow-Up Question Handling

After an extraction, the model reads the conversation history and determines mode from context. Short natural-language question following a JSON response → conversational answer. New document text → fresh extraction.

The instruction "base your answer ONLY on information present in the previous extraction" prevents the model from re-reading the original document and mixing JSON into a conversational reply.

## Prompt Iterations

**Closed type list → open classification**: The original 8-type list forced medical reports, legal paragraphs, and financial statements into `unknown`, losing all structure. Replaced with a guided open list.

**Single confidence → per-field `fields_confidence`**: The original top-level `confidence` didn't tell consumers which fields were uncertain. Per-field scores give actionable granularity.

**Prepending to Open WebUI's system message**: The initial implementation prepended our prompt to Open WebUI's own system message. When Open WebUI's message mentioned "code interpreter" capabilities, gpt-4o followed those instructions instead of ours. Fixed by replacing all existing system messages — the middleware now owns the system prompt entirely.

**Relative date handling**: The first version said "convert dates to ISO 8601." The model invented absolute dates for relative references like "this Thursday." Fixed by explicitly stating: relative dates → `null`, `confidence: 0.0`.

**Null semantics**: Early versions used `null` inconsistently — sometimes meaning "not found," sometimes "not applicable." Clarified by distinguishing certain-absent (`confidence: 1.0`) from unsure-absent (`confidence: 0.3–0.6`).

## Known Limitations

**Optimised for English**: field names and examples are in English. Non-English documents are supported but extraction quality degrades for structurally distant languages.

**Complex tables**: receipts with merged cells, percentage discounts, or multi-level headers may produce incorrect totals.

**OCR assumption**: the prompt assumes clean text. OCR errors reduce field confidence and accuracy with no pre-processing step.

**Context length**: very long documents may exceed the model's context window combined with the system prompt. The model truncates silently and lowers confidence.

**Temperature sensitivity**: at `temperature > 0`, the model occasionally adds preamble before the JSON. Using `temperature: 0` eliminates this risk in production.

## Prompt Length Trade-off

The system prompt is approximately 3800 tokens, sent on every API call. At gpt-3.5-turbo pricing this is ~$0.004/request; at gpt-4o ~$0.04/request.

For production at scale: use OpenAI prompt caching, or fine-tune a smaller model on extraction examples. For this assignment, **reliability > brevity** — the examples and field guidance are the most effective elements and removing them causes immediate regression.

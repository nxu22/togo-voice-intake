# Togo Voice Intake — CLAUDE.md

## What this project is

A phone-based AI intake agent for **Togo AI Automation** (Winnipeg AI automation consultancy).
Anyone can call a public phone number; the agent asks seven structured questions about the
caller's business and pain points, reads back a summary, captures the lead, and hangs up
politely. Captured leads are POSTed to an existing n8n webhook (→ email notification + Airtable CRM).

The agent **only asks and records**. It never pitches, quotes prices, names people, mentions
events or dates, or answers questions about the company. Off-script questions are captured
as messages for human follow-up.

## Architecture

```
Inbound call → Twilio number → Twilio Elastic SIP Trunk
  → LiveKit SIP (inbound trunk + dispatch rule)          [Phase 2 — human configures]
  → LiveKit Agents worker (Python, this repo)            [Phase 1 — you build this]
      ├─ Deepgram STT
      ├─ Claude (Anthropic API) — conversation + tool calls
      ├─ Cartesia TTS
      ├─ Silero VAD + turn detection
      └─ Tools: capture_lead, take_message
  → Call end → POST structured payload to n8n webhook (URL in .env)
```

## Tech stack (do not substitute)

- Python 3.11+, `livekit-agents` (latest stable) with the voice pipeline / AgentSession API
- STT: Deepgram plugin · LLM: Anthropic plugin (Claude Haiku-class model) · TTS: Cartesia plugin
- VAD: Silero plugin
- Rate-limit state: SQLite (single file, no server)
- HTTP: `httpx` for the n8n webhook POST
- Start from the official LiveKit Agents voice example structure, not from scratch

## Repo layout

```
togo-voice-intake/
├── CLAUDE.md              ← this file
├── .env                   ← secrets (NEVER read, print, or commit this)
├── .env.example           ← keys listed with empty values (you maintain this)
├── .claude/settings.json
├── agent/
│   ├── main.py            ← worker entrypoint
│   ├── prompts.py         ← loads system prompt from prompts/system_prompt.md
│   ├── tools.py           ← capture_lead, take_message
│   ├── limits.py          ← 3-layer rate limiting (see below)
│   └── postcall.py        ← webhook POST on call end
├── prompts/
│   └── system_prompt.md   ← the agent's persona + 7-question script (already written)
├── tests/                 ← pytest for limits.py, tools.py payload shape, postcall.py
└── README.md
```

## v1 requirements (all mandatory, not "phase 4 nice-to-haves")

1. **Seven-question intake flow** as defined in `prompts/system_prompt.md`, ending with a
   readback confirmation before hangup.
2. **capture_lead tool** — accumulates the 7 answers into one structured object:
   `{industry, biggest_time_sink, current_process, frequency, tools_used, desired_outcome,
   contact: {name, phone_or_email}}`. Contact info is REQUIRED — the agent must not end
   the call successfully without it (if refused, log as incomplete lead).
3. **take_message tool** — any off-script question or statement from the caller gets stored
   verbatim in a `messages[]` array on the same lead object.
4. **Post-call webhook** — on call end (any reason), POST to `N8N_WEBHOOK_URL`:
   `{call_id, started_at, duration_seconds, completed: bool, lead: {...}, messages: [...],
   transcript: [...]}`. Retry 3x with backoff; on final failure, write payload to
   `failed_webhooks/` as JSON so no lead is ever lost.
5. **Three-layer rate limiting** (in `limits.py`, checked BEFORE the pipeline starts):
   - **Daily cap:** max 50 calls/day AND estimated spend cap $10/day (assume $0.08/min).
     When exceeded: answer, speak one polite line ("Thanks for calling Togo AI Automation —
     our demo line has reached its daily limit. Please leave your details on our website,
     or call back tomorrow."), hang up. Do not start STT/LLM loop.
   - **Per-caller cap:** same caller ID max 3 calls/day. Polite one-liner + hangup.
   - **Per-call cap:** hard 10-minute limit → force transition to readback + polite goodbye,
     never an abrupt cut.
   - Counters reset at midnight America/Winnipeg. Store in SQLite.
6. **Daily summary** — at each webhook POST include running daily stats
   `{calls_today, minutes_today, leads_captured_today}` so n8n can build a daily report.
7. **Console testability** — `python agent/main.py console` must work for local mic testing.

## Lessons inherited from the Riverstone project (respect these)

- Endpointing too aggressive = agent interrupts callers mid-sentence. Start conservative;
  expose VAD/endpointing values as named constants at the top of main.py for easy tuning.
- Readback must happen BEFORE any goodbye/hangup path, including the 10-minute forced wrap-up.
- Never let the LLM invent facts. The system prompt forbids it; also keep temperature low.
- Post-call delivery must be reliable → hence the retry + disk fallback (outbox-style) above.

## Non-goals for v1 (do not build)

- No appointment booking, no calendar, no database of availability
- No RAG / knowledge base — the agent has nothing to look up
- No Mandarin/bilingual support yet (keep language handling isolated so it can be added:
  one place selects the system prompt + TTS voice)
- No browser/WebRTC widget yet (roadmap v2 — same worker will serve it)
- No outbound calling

## Definition of done (Phase 1)

Human runs console mode, completes a full 7-question call as a fake business owner,
hears an accurate readback, hangs up, and within seconds the n8n webhook receives a
correctly shaped payload. `pytest` passes. Rate-limit logic unit-tested including the
midnight reset and the $10 estimate math.

## Phases (context for you; Phase 2+ is mostly human work)

1. **Local agent (this sprint):** everything above, tested via console/Playground.
2. **Telephony:** Twilio number → Elastic SIP Trunk → LiveKit inbound trunk + dispatch
   rule. Human does console config; you may be asked to write small setup scripts using
   `livekit-cli` / LiveKit SIP API.
3. **Launch:** website copy + number goes live on togoaiautomation site.
4. **Later:** bilingual EN/中文, browser call widget, warm transfer.

## Working style

- Work autonomously. Don't ask for confirmation on file edits — permissions are configured.
- Write tests alongside code, not after.
- Keep `.env.example` in sync whenever you add a config value.
- Never read, print, log, or commit `.env` or any API key. Reference keys only via
  `os.environ` at runtime.
- Commit in small logical units with clear messages.

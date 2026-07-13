# Togo Voice Intake

A phone-based AI intake agent for **Togo AI Automation**. Callers reach a public number,
the agent asks seven structured questions about their business and pain points, reads the
answers back to confirm, and hangs up politely. Every call is POSTed to an n8n webhook,
which fans out to email and Airtable.

The agent only asks and records. It never pitches, quotes prices, or answers questions
about the company — off-script questions are captured verbatim for a human to follow up on.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

cp .env.example .env          # then fill in the real values
```

You need accounts and API keys for **LiveKit Cloud**, **Deepgram**, **Cartesia**, and
**Anthropic**, plus the n8n webhook URL. Nothing runs until `.env` has real values.

## Run it

Talk to the agent through your laptop microphone — no phone number or LiveKit room needed:

```bash
python agent/main.py console
```

Connect a worker to LiveKit so it can take real calls (Phase 2):

```bash
python agent/main.py dev      # or `start` in production
```

## Tests

```bash
pytest
```

Covers the rate-limit logic (including the midnight reset and the $10/day spend math),
the lead payload shape, webhook retry + disk fallback, and caller-ID handling in console
mode. No network calls, no API keys.

### Simulate a whole call without a microphone

```bash
python scripts/simulate_call.py          # print the captured lead + payload
python scripts/simulate_call.py --post    # also POST it to N8N_WEBHOOK_URL
```

Plays a scripted business owner through all seven questions against the real Claude
model and the real tools — no STT, no TTS, no audio. It checks that the seven questions
are asked, that `capture_lead` accumulates them, that the planted pricing question is
deflected into `take_message` rather than answered, and that the readback happens before
the goodbye. Costs a few cents of Anthropic usage.

This is the fastest way to check a prompt change didn't break the script. It cannot tell
you how the turn-taking *feels* — only a real console call can.

## How a call flows

```
Inbound call → Twilio → LiveKit SIP → this worker
  ├─ rate limits checked BEFORE any STT/LLM runs
  ├─ Deepgram STT · Claude (Haiku) · Cartesia TTS · Silero VAD
  ├─ tools: capture_lead (the 7 answers) · take_message (off-script questions)
  └─ on call end (any reason) → POST to N8N_WEBHOOK_URL
```

### Rate limiting

Three layers, all backed by one SQLite file (`RATE_LIMIT_DB_PATH`). Counters reset at
midnight America/Winnipeg — there's no cron job; "today" is just derived from the local
wall clock.

| Layer | Default | What happens when it trips |
|---|---|---|
| Daily calls | 50/day | Answer, speak one polite line, hang up. No STT/LLM is started. |
| Daily spend | $10/day (estimated at $0.08/min) | Same. |
| Per caller | 3/day per caller ID | Same, with a caller-specific line. |
| Per call | 10 minutes | Forced wrap-up: contact info first if missing, then readback, then a polite goodbye. Never an abrupt cut. |

Every limit is an environment variable — raise the daily cap from 50 to 100, or the budget
from $10 to $20, by editing `.env` and restarting. No code change.

### Nothing loses a lead

On call end the worker POSTs the payload to n8n and retries three times with exponential
backoff. If all three fail, the payload is written to `failed_webhooks/<call_id>.json` so
it can be replayed by hand. A webhook outage never takes down a call.

Each payload carries running daily stats (`calls_today`, `minutes_today`,
`leads_captured_today`) so n8n can build a daily report without querying anything.

A lead is only marked `completed` if all seven answers **and** contact info are present.
Calls that end without contact info are still delivered — flagged incomplete, so a human
can see what was missed.

## Tuning the conversation

Endpointing that's too aggressive makes the agent talk over people mid-sentence. All the
VAD and turn-taking values are named constants at the top of [agent/main.py](agent/main.py)
and start deliberately conservative:

- `MIN_ENDPOINTING_DELAY` — silence a caller must leave before we consider them done. Raise
  it if callers get cut off; lower it if the agent feels sluggish.
- `VAD_MIN_SILENCE_DURATION` — how much silence ends a speech segment. Generous, so a caller
  pausing to think isn't treated as finished.
- `MIN_INTERRUPTION_DURATION` / `MIN_INTERRUPTION_WORDS` — require real speech to interrupt,
  not a cough.

`LLM_TEMPERATURE` is low on purpose: the agent must never improvise facts.

## Adding a language later

Language selection is isolated to [agent/prompts.py](agent/prompts.py). To add Mandarin,
add one `LanguageProfile` to `LANGUAGES` and drop `prompts/system_prompt.zh.md` beside the
English one. Nothing else changes.

## Layout

```
agent/
  main.py      worker entrypoint, tuning constants, call lifecycle
  prompts.py   language selection (prompt + STT/TTS locale + voice)
  tools.py     CallState, Lead, capture_lead, take_message
  limits.py    three-layer rate limiting (SQLite)
  postcall.py  webhook POST with retries + disk fallback
prompts/
  system_prompt.md    the agent's persona and 7-question script
tests/
```

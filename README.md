# Togo Voice Intake

A self-hosted inbound voice agent that answers a real phone number, asks three
structured intake questions, reads the answers back to confirm, and posts the
lead to n8n. Built on LiveKit + Deepgram + Cartesia + Claude — no managed voice
platform in between.

The interesting problem was not the pipeline. It was knowing when the caller had
finished talking.

> **Cold starts.** The worker runs on LiveKit Cloud and is not kept warm, so the
> first call after an idle period takes several seconds before the agent speaks.
> Steady state is ~700ms time-to-first-token.

---

## What building this taught me about turn-taking

Four layers, added in this order. Each one because the previous layer failed on a
real call.

**1. Silero VAD** — `VAD_MIN_SILENCE_DURATION = 0.65s`

Catches the acoustic boundary. On its own it is too eager: a caller pausing to
think reads as finished.

**2. Semantic end-of-turn model** — `EOU_UNLIKELY_THRESHOLD = 0.35`

The endpointing delay is binary, not gradual. EOU probability ≥ 0.35 waits
`MIN_ENDPOINTING_DELAY` (1.3s); below it waits `MAX_ENDPOINTING_DELAY` (3.0s).

The threshold sits between two real call samples — **0.43** (caller genuinely
done) and **0.17** (caller mid-thought). It is not a default; it is two data
points.

`MIN_ENDPOINTING_DELAY` started at 0.8s and went to 1.3s after the first real
phone call, where callers were being cut off mid-sentence.

**3. Deepgram endpointing** — `STT_ENDPOINTING_MS = 40`

Deliberately low. This only controls how fast interim transcripts finalise; it
does not make turn decisions.

**4. Lexical unfinished-detection** — `agent/turns.py`

The semantic model scored **"The company's name is"** at **0.96** — confidently
certain the caller was done, mid-noun-phrase. No threshold tuning fixes that
class of error, because the model is not uncertain. It is wrong.

So a cheap lexical layer sits underneath it: if a turn ends on a comma, on a
dangling word (`the`, `is`, `and`, `um`…), or on a bare filler, the turn is held,
the fragment is buffered onto the next one, and the agent nudges gently after
`DANGLING_NUDGE_SECONDS = 4.0`. Capped at `MAX_CONSECUTIVE_HOLDS = 2` so a silent
caller cannot deadlock the conversation.

Exceptions are hardcoded — "I think so", "I guess so" end on a dangling word but
are complete answers.

---

## Latency work

Measured by hand and recorded in commits. There is **no persistent
instrumentation** — which means a model swap or a region change would require
redoing this measurement from scratch. That is a real gap, not a nitpick.

| Change | Effect |
| --- | --- |
| Strict tool schema **disabled** | TTFT p50 **1730ms → 897ms** |
| Warmed TLS connection at prewarm | first request **4–7s → <1s** |
| `PREEMPTIVE_GENERATION` | starts the LLM before turn commit, hiding TTFT |

Steady state: ~690ms p50 time-to-first-token; the cold first turn of a call is
~1000ms.

---

## How a call flows

```
Inbound call → Twilio → LiveKit SIP → this worker
  ├─ rate limits checked BEFORE any STT/LLM runs
  ├─ Deepgram STT · Claude Haiku · Cartesia TTS · Silero VAD
  ├─ tools: capture_lead (the intake answers) · take_message (off-script questions)
  └─ on call end (any reason) → POST to N8N_WEBHOOK_URL
```

The agent only asks and records. It never pitches, quotes prices, or answers
questions about the company — off-script questions are captured verbatim via
`take_message` for a human to follow up on. `LLM_TEMPERATURE` is low on purpose:
the agent must never improvise a fact about the business.

Interruption handling is configured, not implemented — `MIN_INTERRUPTION_DURATION
= 0.6s` and `MIN_INTERRUPTION_WORDS = 2` are passed to livekit-agents so a cough
does not stop the agent mid-sentence. The mechanism itself lives in the library.

---

## Rate limiting

Three layers over one SQLite file. Counters reset at midnight America/Winnipeg —
there is no cron job; "today" is derived from the local wall clock.

| Layer | Default | What happens when it trips |
| --- | --- | --- |
| Daily calls | 50/day | Answer, speak one polite line, hang up. No STT/LLM starts. |
| Daily spend | $10/day (est. $0.08/min) | Same. |
| Per caller | 3/day per caller ID | Same, with a caller-specific line. |
| Per call | 10 minutes | Forced wrap-up: contact info, then readback, then goodbye. Never an abrupt cut. |

Every limit is an environment variable. Raising the daily cap or the budget is an
`.env` edit and a restart, not a code change.

The checks run before any STT or LLM call starts, so a runaway does not cost
money before it is stopped.

---

## Nothing loses a lead

On call end the worker POSTs to n8n and retries three times with exponential
backoff. If all three fail the payload is written to
`failed_webhooks/<call_id>.json` for manual replay. A webhook outage never takes
down a call.

Each payload carries running daily stats (`calls_today`, `minutes_today`,
`leads_captured_today`) so n8n can build a daily report without querying
anything.

A lead is marked `completed` only if contact info is present alongside the two
essentials the script actually asks for — the business and what they want
automated. The optional fields (current process, tools, frequency) are recorded
only when a caller volunteers them and never block completeness. Calls that end
without contact info are still delivered, flagged incomplete, so a human can see
what was missed.

---

## Testing

```bash
pytest
```

Covers rate-limit logic (including the midnight reset and the spend math), lead
payload shape, webhook retry + disk fallback, and caller-ID handling in console
mode. No network calls, no API keys.

```bash
python scripts/simulate_call.py          # print the captured lead + payload
python scripts/simulate_call.py --post   # also POST to N8N_WEBHOOK_URL
```

Plays a scripted business owner through the whole intake against the real Claude
model and the real tools — no STT, no TTS, no audio. It checks that
`capture_lead` ends up with a complete lead, that a planted pricing question is
deflected into `take_message` rather than answered, that the readback happens
before the goodbye, and that the agent hangs up instead of narrating its own
plumbing. Costs a few cents of Anthropic usage.

This is the fastest way to check a prompt change did not break the script. **It
cannot tell you how the turn-taking feels** — only a real console call can. The
two failure modes this project actually has (cut-off callers, agent talking over
someone) are both invisible to it.

Tests are run by hand. There is no CI.

---

## Running it

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate     # .venv/Scripts/activate on Windows
pip install -r requirements.txt
cp .env.example .env          # then fill in real values
```

You need accounts and keys for LiveKit Cloud, Deepgram, Cartesia, and Anthropic,
plus the n8n webhook URL. Nothing runs until `.env` has real values.

```bash
python agent/main.py console   # talk through your laptop mic, no phone needed
python agent/main.py dev       # connect a worker to LiveKit for real calls
python agent/main.py start     # production
```

Deployment is a two-stage Docker build to LiveKit Cloud (`us-east`, agent name
`togo-intake`). Secrets are injected as LiveKit Cloud secrets, not baked into the
image.

---

## Adding a language

Language selection is isolated to `agent/prompts.py`. To add Mandarin: add one
`LanguageProfile` to `LANGUAGES` and drop `prompts/system_prompt.zh.md` beside
the English one. Nothing else changes.

---

## Layout

```
agent/
  main.py      worker entrypoint, tuning constants, call lifecycle
  prompts.py   language selection (prompt + STT/TTS locale + voice)
  tools.py     CallState, Lead, capture_lead, take_message
  turns.py     lexical unfinished-turn detection
  limits.py    three-layer rate limiting (SQLite)
  postcall.py  webhook POST with retries + disk fallback
prompts/
  system_prompt.md    persona and the 3-question script
tests/
scripts/
  simulate_call.py    full-script dry run, no audio
```

---

## Known limits

- **No persistent latency instrumentation.** The numbers above are hand-measured
  and recorded in commits. Re-measuring after a model or region change means
  redoing the work.
- **Cold start on every idle period.** Not kept warm; the first caller waits.
- **No CI.** Tests pass when someone remembers to run them.
- **Single tenant, single number.** No auth, no per-client isolation.
- **Interruption handling is library-provided.** Two thresholds are tuned; the
  mechanism is not mine.

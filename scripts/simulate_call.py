"""Drive a full intake call in text mode — no microphone, no STT, no TTS.

Uses livekit's simulation harness (AgentSession.run) against the REAL Claude model
and the REAL capture_lead / take_message tools, so it exercises everything except
the audio path. It is the cheapest way to check the seven-question script, the tool
accumulation, the readback, and the webhook payload.

    python scripts/simulate_call.py            # simulate, print payload, don't POST
    python scripts/simulate_call.py --post     # also POST to N8N_WEBHOOK_URL

Costs a small amount of Anthropic usage. --post creates a real (clearly fake) lead
in n8n, so it will send an email and write an Airtable row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, ConversationItemAddedEvent
from livekit.plugins import anthropic

from agent.limits import LimitConfig, RateLimiter
from agent.main import LLM_MODEL, LLM_TEMPERATURE
from agent.postcall import build_payload, deliver
from agent.prompts import get_language
from agent.tools import CallState, capture_lead, end_call, take_message

load_dotenv()

# A believable Winnipeg business owner. One off-script question is planted at turn 6
# to prove take_message fires and that the agent refuses to answer it.
CALLER_TURNS = [
    "Yeah, sounds good.",
    "Sure — I run a bakery, Sunrise Bakery, over on Corydon.",
    "Honestly? Taking phone orders. It eats my whole morning.",
    "Right now we just scribble them on a notepad by the register.",
    "Every single day. All day, really.",
    "We're on Square for payments, and I keep a spreadsheet in Excel.",
    "Hang on — how much does something like this actually cost?",
    "I'd want the orders to just land in a spreadsheet by themselves, no typing.",
    "Dana Reyes. Best number is 204-555-0101.",
    "Yes, that's all correct.",
]

GOODBYE_MARKERS = ("thanks for calling", "be in touch", "get back to you soon")
READBACK_MARKERS = ("let me make sure", "did i get that right", "got this right", "read that back")


def _say(role: str, text: str) -> None:
    colour = {"caller": "\033[36m", "agent": "\033[33m", "sys": "\033[90m"}.get(role, "")
    print(f"{colour}{role:>7}\033[0m  {text}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", action="store_true", help="POST the payload to n8n")
    args = parser.parse_args()

    language = get_language()
    state = CallState(
        call_id=f"sim-{uuid.uuid4().hex[:8]}",
        caller_id="simulation",
        started_at=datetime.now(timezone.utc),
    )

    session: AgentSession[CallState] = AgentSession[CallState](
        userdata=state,
        llm=anthropic.LLM(model=LLM_MODEL, temperature=LLM_TEMPERATURE),
    )

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        item = event.item
        if getattr(item, "role", None) in ("user", "assistant"):
            state.add_transcript(item.role, item.text_content or "")

    await session.start(
        Agent(
            instructions=language.system_prompt(),
            tools=[capture_lead, take_message, end_call],
        )
    )

    _say("sys", f"model={LLM_MODEL} temperature={LLM_TEMPERATURE}")
    print()

    # The agent speaks first.
    opening = await session.run(user_input="<call connected>")
    for event in opening.events:
        text = getattr(getattr(event, "item", None), "text_content", None)
        if text:
            _say("agent", text)

    agent_lines: list[str] = []
    for turn in CALLER_TURNS:
        _say("caller", turn)
        result = await session.run(user_input=turn)
        for event in result.events:
            item = getattr(event, "item", None)
            text = getattr(item, "text_content", None)
            if text and getattr(item, "role", None) == "assistant":
                agent_lines.append(text)
                _say("agent", text)
        print()

    # --- assertions ---------------------------------------------------------
    lead = state.lead
    joined = " ".join(agent_lines).lower()
    failures: list[str] = []

    if not lead.is_complete():
        missing = [k for k, v in lead.to_dict().items() if not v and k != "contact"]
        failures.append(f"lead incomplete; missing {missing or 'contact'}")
    if not lead.has_contact():
        failures.append("no contact info captured — the one required field")
    if not lead.messages:
        failures.append("take_message never fired for the pricing question")
    if any(word in joined for word in ("$", "per hour", "costs about", "our pricing")):
        failures.append("agent appears to have quoted a price — it must never do that")

    readback_at = next(
        (i for i, line in enumerate(agent_lines) if any(m in line.lower() for m in READBACK_MARKERS)),
        None,
    )
    goodbye_at = next(
        (i for i, line in enumerate(agent_lines) if any(m in line.lower() for m in GOODBYE_MARKERS)),
        None,
    )
    if readback_at is None:
        failures.append("no readback detected before the call ended")
    elif goodbye_at is not None and goodbye_at < readback_at:
        failures.append("agent said goodbye BEFORE the readback (Riverstone regression)")

    # The call stays open until the agent hangs up. If it never called end_call, a real
    # caller would be sitting on an open line listening to silence.
    if state.end_reason != "agent_goodbye":
        failures.append(
            f"agent never called end_call — the line would stay open "
            f"(end_reason={state.end_reason!r})"
        )

    # The caller must never hear the agent narrate its own plumbing.
    NARRATION = ("i'll end the call", "ending the call", "end the call now",
                 "let me record", "i'll note that down in the system", "using the")
    narrated = [line for line in agent_lines if any(n in line.lower() for n in NARRATION)]
    if narrated:
        failures.append(f"agent narrated a tool out loud: {narrated!r}")

    print("\033[1m--- captured lead ---\033[0m")
    print(json.dumps(lead.to_dict(), indent=2))
    print(f"\nmessages for follow-up: {lead.messages}")

    state.ended_at = datetime.now(timezone.utc)
    with RateLimiter("limits.console.db", config=LimitConfig()) as limiter:
        limiter.start_call(state.call_id, state.caller_id)
        limiter.end_call(state.call_id, state.duration_seconds())
        if lead.is_complete():
            limiter.mark_lead_captured(state.call_id)
        payload = build_payload(
            state,
            limiter.daily_stats(),
            completed=lead.is_complete(),
            end_reason=state.end_reason,
        )

    print("\n\033[1m--- webhook payload ---\033[0m")
    print(json.dumps({**payload, "transcript": f"<{len(payload['transcript'])} turns>"}, indent=2))

    if args.post:
        ok = await deliver(payload)
        print(f"\nwebhook POST: {'delivered' if ok else 'FAILED (written to failed_webhooks/)'}")

    print()
    if failures:
        print("\033[31m\033[1mFAILURES:\033[0m")
        for f in failures:
            print(f"  \033[31m✗\033[0m {f}")
        return 1
    print("\033[32m\033[1mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Togo AI Automation — phone intake agent worker.

Run locally with a microphone:   python agent/main.py console
Run against LiveKit (dev):       python agent/main.py dev
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# `python agent/main.py console` puts agent/ on sys.path, not the project root —
# so make the package importable before we import from it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    EndpointingOptions,
    InterruptionOptions,
    JobContext,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.plugins import anthropic, cartesia, deepgram, silero

from agent.limits import UNKNOWN_CALLER, LimitConfig, RateLimiter
from agent.postcall import build_payload, deliver
from agent.prompts import get_language
from agent.tools import CallState, capture_lead, take_message

load_dotenv()

logger = logging.getLogger("togo.agent")

# --- Tuning constants -------------------------------------------------------
# Endpointing that is too aggressive makes the agent talk over callers mid-sentence
# (the Riverstone lesson). These start deliberately conservative — raise the delays
# further if callers still get cut off; lower them if the agent feels sluggish.

# Silence the caller must leave before we consider their turn finished.
MIN_ENDPOINTING_DELAY = 0.8  # seconds
# Hard ceiling on how long we wait for the turn detector before replying anyway.
MAX_ENDPOINTING_DELAY = 6.0  # seconds

# Silero VAD: how much silence marks the end of speech. Generous, so that a caller
# pausing to think ("uh... let me see...") is not treated as done talking.
VAD_MIN_SILENCE_DURATION = 0.65  # seconds
VAD_MIN_SPEECH_DURATION = 0.05  # seconds
VAD_ACTIVATION_THRESHOLD = 0.5

# Deepgram's own endpointing. Kept low because the VAD + delays above do the real
# turn-taking work; this only controls how fast interim transcripts finalize.
STT_ENDPOINTING_MS = 40

# Interruptions: require real speech, not a cough or a doorbell.
MIN_INTERRUPTION_DURATION = 0.6  # seconds
MIN_INTERRUPTION_WORDS = 2

# Low temperature — the agent must never improvise facts (CLAUDE.md guardrail).
LLM_TEMPERATURE = 0.2
LLM_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

# How long to wait for the SIP participant before assuming we're in console mode.
PARTICIPANT_WAIT_SECONDS = 5.0

# Per-call time limit. We start the forced wrap-up early enough that the readback
# still happens before the hard limit — never an abrupt cut.
WRAPUP_MARGIN_SECONDS = 90
HANGUP_GRACE_SECONDS = 20

CONSOLE_CALLER_ID = "console"

WRAPUP_INSTRUCTIONS = (
    "The call is near its time limit. Skip any remaining questions. If you do not "
    "yet have a callback number or email, ask for it now — that is the one thing "
    "you must not end without. Then read back what you have recorded, confirm it, "
    "and close politely. Keep it brief."
)


def _caller_id_from(participant: rtc.RemoteParticipant | None) -> str:
    if participant is None:
        return CONSOLE_CALLER_ID
    attributes = participant.attributes or {}
    # LiveKit SIP puts the caller's number here; fall back to identity for web/SDK callers.
    return (
        attributes.get("sip.phoneNumber")
        or attributes.get("sip.from_number")
        or participant.identity
        or UNKNOWN_CALLER
    )


async def _wait_for_caller(ctx: JobContext) -> rtc.RemoteParticipant | None:
    """Resolve the caller, or None in console mode where there is no remote participant."""
    try:
        return await asyncio.wait_for(
            ctx.wait_for_participant(), timeout=PARTICIPANT_WAIT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.info("no remote participant appeared; treating this as a console session")
        return None


async def _hangup(ctx: JobContext) -> None:
    try:
        await ctx.delete_room()
    except Exception as exc:  # noqa: BLE001 — hangup must never raise into the caller
        logger.warning("failed to delete room on hangup: %s", exc)


async def _turn_away(ctx: JobContext, message: str, language) -> None:
    """Answer, speak one polite line, hang up. No STT and no LLM are started."""
    session: AgentSession = AgentSession(
        tts=cartesia.TTS(
            model=os.environ.get("CARTESIA_MODEL", "sonic-3"),
            language=language.tts_language,
            **({"voice": language.voice_id()} if language.voice_id() else {}),
        )
    )
    await session.start(Agent(instructions=""), room=ctx.room)
    handle = session.say(message, allow_interruptions=False)
    await handle.wait_for_playout()
    await session.aclose()
    await _hangup(ctx)


async def entrypoint(ctx: JobContext) -> None:
    language = get_language()
    config = LimitConfig.from_env()
    limiter = RateLimiter(
        os.environ.get("RATE_LIMIT_DB_PATH", "limits.db"), config=config
    )

    await ctx.connect()

    participant = await _wait_for_caller(ctx)
    caller_id = _caller_id_from(participant)
    call_id = ctx.room.name or f"call-{uuid.uuid4().hex[:12]}"
    ctx.log_context_fields = {"call_id": call_id, "caller_id": caller_id}

    # Layers 1 and 2: decide BEFORE building the pipeline.
    decision = limiter.check(caller_id)
    if not decision.allowed:
        logger.info("call from %s rejected: %s", caller_id, decision.reason)
        try:
            await _turn_away(ctx, decision.message or "", language)
        finally:
            limiter.close()
        return

    state = CallState(
        call_id=call_id, caller_id=caller_id, started_at=datetime.now(timezone.utc)
    )
    limiter.start_call(call_id, caller_id)

    session: AgentSession[CallState] = AgentSession[CallState](
        userdata=state,
        stt=deepgram.STT(
            model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            language=language.stt_language,
            endpointing_ms=STT_ENDPOINTING_MS,
        ),
        llm=anthropic.LLM(model=LLM_MODEL, temperature=LLM_TEMPERATURE),
        tts=cartesia.TTS(
            model=os.environ.get("CARTESIA_MODEL", "sonic-3"),
            language=language.tts_language,
            **({"voice": language.voice_id()} if language.voice_id() else {}),
        ),
        vad=silero.VAD.load(
            min_speech_duration=VAD_MIN_SPEECH_DURATION,
            min_silence_duration=VAD_MIN_SILENCE_DURATION,
            activation_threshold=VAD_ACTIVATION_THRESHOLD,
        ),
        turn_handling=TurnHandlingOptions(
            endpointing=EndpointingOptions(
                min_delay=MIN_ENDPOINTING_DELAY,
                max_delay=MAX_ENDPOINTING_DELAY,
            ),
            interruption=InterruptionOptions(
                min_duration=MIN_INTERRUPTION_DURATION,
                min_words=MIN_INTERRUPTION_WORDS,
            ),
        ),
    )

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        item = event.item
        if getattr(item, "role", None) in ("user", "assistant"):
            state.add_transcript(item.role, item.text_content or "")

    end_reason = "caller_hangup"

    async def _on_shutdown() -> None:
        state.ended_at = datetime.now(timezone.utc)
        limiter.end_call(call_id, state.duration_seconds())
        if state.lead.is_complete():
            limiter.mark_lead_captured(call_id)
        else:
            logger.warning(
                "incomplete lead for call %s (contact=%s)",
                call_id,
                state.lead.has_contact(),
            )

        payload = build_payload(
            state,
            limiter.daily_stats(),
            completed=state.lead.is_complete(),
            end_reason=end_reason,
        )
        try:
            await deliver(payload)
        finally:
            limiter.close()

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(
        Agent(instructions=language.system_prompt(), tools=[capture_lead, take_message]),
        room=ctx.room,
    )

    # Layer 3: force a readback + goodbye before the hard limit, then hang up.
    async def _enforce_time_limit() -> None:
        nonlocal end_reason
        wrapup_at = max(30, config.max_call_seconds - WRAPUP_MARGIN_SECONDS)
        await asyncio.sleep(wrapup_at)
        logger.info("call %s hit the wrap-up threshold; forcing readback", call_id)
        end_reason = "time_limit"
        try:
            handle = session.generate_reply(instructions=WRAPUP_INSTRUCTIONS)
            await asyncio.wait_for(
                handle.wait_for_playout(),
                timeout=WRAPUP_MARGIN_SECONDS + HANGUP_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("forced wrap-up did not finish in time; hanging up anyway")
        except Exception as exc:  # noqa: BLE001
            logger.warning("forced wrap-up failed: %s", exc)
        await _hangup(ctx)

    limit_task = asyncio.create_task(_enforce_time_limit())
    ctx.add_shutdown_callback(lambda: _cancel(limit_task))

    await session.generate_reply(
        instructions="Greet the caller with your opening line and ask if it's a good time."
    )


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: B014 — best effort cleanup
        pass


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

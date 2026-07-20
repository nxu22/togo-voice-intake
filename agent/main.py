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
    AgentStateChangedEvent,
    APIConnectOptions,
    ConversationItemAddedEvent,
    EndpointingOptions,
    InterruptionOptions,
    JobContext,
    JobProcess,
    PreemptiveGenerationOptions,
    StopResponse,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.inference.eot import TurnDetector
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import anthropic, cartesia, deepgram, silero

from agent.limits import UNKNOWN_CALLER, LimitConfig, RateLimiter
from agent.postcall import build_payload, deliver
from agent.prompts import get_language
from agent.tools import CallState, capture_lead, end_call, take_message
from agent.turns import looks_unfinished

load_dotenv()

logger = logging.getLogger("togo.agent")

# --- Tuning constants -------------------------------------------------------
# Endpointing that is too aggressive makes the agent talk over callers mid-sentence
# (the Riverstone lesson). These start deliberately conservative — raise the delays
# further if callers still get cut off; lower them if the agent feels sluggish.

# Silence the caller must leave before we consider their turn finished.
# Raised from 0.8s after a live call: Deepgram sometimes delivered the final
# transcript AFTER the turn had been committed (livekit warned "transcript arrives
# after turn has been committed... raise min_delay"), so the agent cut callers off.
# A bit more patience here waits out the slow STT instead of talking over them.
MIN_ENDPOINTING_DELAY = 1.3  # seconds

# The endpointing delay is BINARY, not graduated (audio_recognition.py): the wait is
# min_delay, unless end-of-turn probability < unlikely_threshold, in which case it is
# max_delay. So both of these landed in the same bucket:
#   "can I call you again? I wanna end the call" -> 0.43  (complete sentence, but UNSURE)
#   a caller still mid-thought                   -> 0.17  (genuinely unfinished)
# Moving max_delay alone can only trade one complaint for the other. The fix is to move
# the threshold BETWEEN them, so a complete-sounding sentence commits fast while a
# genuinely unfinished one is given real room to breathe.
EOU_UNLIKELY_THRESHOLD = 0.35
# Only pays out on a genuinely-unsure turn (<0.35) now, so we can afford to be patient.
MAX_ENDPOINTING_DELAY = 3.0  # seconds

# LLM connect options. livekit defaults to timeout=10s / retry_interval=2s, so ONE
# stalled request buys 12s+ of dead air before a retry is even attempted — the likely
# source of the 16.5s silence. Steady-state TTFT is ~700ms, so 6s is already a very
# generous ceiling, and we retry almost immediately.
LLM_TIMEOUT_SECONDS = 6.0
LLM_MAX_RETRY = 2
LLM_RETRY_INTERVAL = 0.2  # seconds

# If generation outlives this, say something so the caller knows the line is alive.
# The dead air that made a caller ask "Hello? Can you hear me?" is worse than a filler.
# Suppressed while the caller is mid-sentence — see IntakeAgent.
THINKING_FILLER_DELAY = 1.8  # seconds
THINKING_FILLERS = (
    "Got it — one moment.",
    "Okay, let me get that down.",
    "Right, just a second.",
)

# How long a caller may sit in silence mid-sentence before we check in. Lowered from
# 7s after a live call: when a caller trailed off ("...calling part,") the 7s of dead
# air read as "did the line drop?". 4s still gives a thinker room without a long silence.
DANGLING_NUDGE_SECONDS = 4.0
# Don't hold the line forever if the heuristic keeps mis-firing on the same caller.
MAX_CONSECUTIVE_HOLDS = 2
NUDGE_INSTRUCTIONS = (
    "The caller started an answer, trailed off mid-sentence, and has now been silent "
    "for several seconds. In one short, warm sentence, invite them to finish the "
    "thought. Do not repeat the question and do not rush them."
)

# Preemptive generation (livekit default: ON). We keep it on deliberately: it starts
# the LLM before the turn is committed, which hides most of the ~700ms TTFT.
# It is safe with our tools — livekit gates tool execution behind speech authorization
# (agent_activity.py: "start to execute tools (only after play())"), so a speculative
# turn that gets discarded never runs capture_lead and cannot corrupt a lead.
PREEMPTIVE_GENERATION = True

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

# The name this worker registers under for EXPLICIT dispatch. The LiveKit SIP dispatch
# rule references this exact string (see scripts/setup_livekit_sip.py), so an inbound
# call pulls in this agent and nothing else. Console mode ignores it. Keep the two in
# sync — that's why the setup script imports this constant rather than hardcoding it.
AGENT_NAME = "togo-intake"

# The Anthropic plugin defaults to strict tool schemas, which cost a schema-compilation
# round trip and measured ~2x the time-to-first-token on our tools (p50 1730ms -> 897ms).
# Our tools take plain optional strings and tolerate junk (see _clean in tools.py), so we
# don't need the strictness — on a phone call, latency is the feature.
LLM_STRICT_TOOL_SCHEMA = False

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


class IntakeAgent(Agent):
    """The intake agent, plus one rule the turn detector can't express: don't talk over
    a caller who is still thinking.

    The end-of-turn model scored "The company's name is" at 0.96 — confidently finished.
    When the words say otherwise, we discard the turn and keep listening rather than
    interjecting "I'm listening — go ahead" at someone mid-thought.
    """

    def __init__(self, *, instructions: str, tools: list) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._fragment: str = ""
        self._nudge_task: asyncio.Task[None] | None = None
        self._consecutive_holds = 0

    def _cancel_nudge(self) -> None:
        if self._nudge_task and not self._nudge_task.done():
            self._nudge_task.cancel()
        self._nudge_task = None

    async def _nudge_later(self) -> None:
        """If the caller genuinely goes quiet mid-sentence, check in — but not before
        we've given them real room to think. The complaint was an interjection at 0.8s;
        a caller thinking for 4s should hear nothing at all."""
        try:
            await asyncio.sleep(DANGLING_NUDGE_SECONDS)
        except asyncio.CancelledError:
            return  # they carried on — the common case
        logger.info("caller trailed off and stayed quiet; nudging gently")
        self.session.generate_reply(instructions=NUDGE_INSTRUCTIONS)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        self._cancel_nudge()
        state: CallState = self.session.userdata

        text = (new_message.text_content or "").strip()

        # Stitch a held-back fragment onto what they just said, so nothing is lost:
        # "The company's name is" + "Sunrise Bakery" -> one coherent answer.
        if self._fragment:
            text = f"{self._fragment} {text}".strip()
            new_message.content = [text]
            self._fragment = ""

        if looks_unfinished(text) and self._consecutive_holds < MAX_CONSECUTIVE_HOLDS:
            self._consecutive_holds += 1
            self._fragment = text
            state.awaiting_continuation = True
            logger.info("caller seems mid-sentence, holding the line: %r", text)
            self._nudge_task = asyncio.create_task(self._nudge_later())
            raise StopResponse()

        self._consecutive_holds = 0
        state.awaiting_continuation = False


def prewarm(proc: JobProcess) -> None:
    """Do the expensive, per-process work before a call arrives, not during one.

    Silero's ONNX model was previously loaded inside the entrypoint, i.e. on every
    single call while the caller waited. It only needs loading once per process.
    """
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=VAD_MIN_SPEECH_DURATION,
        min_silence_duration=VAD_MIN_SILENCE_DURATION,
        activation_threshold=VAD_ACTIVATION_THRESHOLD,
    )
    proc.userdata["llm"] = anthropic.LLM(
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        _strict_tool_schema=LLM_STRICT_TOOL_SCHEMA,
    )
    logger.info("prewarm complete: VAD loaded, LLM client ready")


async def _warm_llm_connection(model: anthropic.LLM) -> None:
    """Pay the TLS/connection setup before the caller is waiting on it.

    The first request on a fresh connection measured several seconds slower than
    subsequent ones. Run this concurrently with waiting for the caller so the
    handshake is already done by the time we generate the greeting.
    """
    try:
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="user", content="hi")
        stream = model.chat(chat_ctx=chat_ctx)
        async for _ in stream:
            break
        await stream.aclose()
        logger.info("LLM connection warmed")
    except Exception as exc:  # noqa: BLE001 — a cold connection is not worth failing a call
        logger.warning("LLM warm-up failed (harmless): %s", exc)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _first_string(*candidates: object) -> str | None:
    """Return the first candidate that is a genuinely usable string.

    Truthiness is not enough: in console mode livekit hands us an autospec mock
    whose every unpinned attribute is a truthy MagicMock, so `x or y` happily
    selects a mock. Only a non-empty str counts.
    """
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _caller_id_from(participant: rtc.RemoteParticipant | None) -> str:
    if participant is None:
        return CONSOLE_CALLER_ID

    attributes = getattr(participant, "attributes", None)
    if not isinstance(attributes, dict):  # a mock, or None, is not a dict
        attributes = {}

    # LiveKit SIP puts the caller's number here; fall back to identity for web/SDK callers.
    return (
        _first_string(
            attributes.get("sip.phoneNumber"),
            attributes.get("sip.from_number"),
            getattr(participant, "identity", None),
        )
        or UNKNOWN_CALLER
    )


async def _wait_for_caller(ctx: JobContext) -> rtc.RemoteParticipant | None:
    """Wait for the caller to appear, or give up and treat the call as anonymous.

    Note console mode DOES produce a participant — a MagicMock one — so this
    returning non-None says nothing about whether we're on a real call. Use
    ctx.is_fake_job() for that.
    """
    try:
        return await asyncio.wait_for(
            ctx.wait_for_participant(), timeout=PARTICIPANT_WAIT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.info("no participant appeared within %ss", PARTICIPANT_WAIT_SECONDS)
        return None


async def _hangup(ctx: JobContext, reason: str) -> None:
    """Drop the line and end the job.

    delete_room() disconnects a real SIP caller but deliberately no-ops in console
    mode, so shutdown() is what actually ends the session — without it a console
    call would keep listening forever after the goodbye.
    """
    try:
        await ctx.delete_room()
    except Exception as exc:  # noqa: BLE001 — hangup must never raise into the caller
        logger.warning("failed to delete room on hangup: %s", exc)
    ctx.shutdown(reason=reason)


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
    await _hangup(ctx, reason="rate limited")


async def entrypoint(ctx: JobContext) -> None:
    language = get_language()
    config = LimitConfig.from_env()

    # Console mode is a local mic test, not a caller. It gets its own counter file so
    # test calls never eat into the real line's 50-call / $10 daily budget.
    console = ctx.is_fake_job()
    db_path = (
        os.environ.get("CONSOLE_RATE_LIMIT_DB_PATH", "limits.console.db")
        if console
        else os.environ.get("RATE_LIMIT_DB_PATH", "limits.db")
    )
    limiter = RateLimiter(db_path, config=config)

    await ctx.connect()

    # Reuse the prewarmed VAD/LLM if this process has them; fall back for safety.
    llm_model: anthropic.LLM = ctx.proc.userdata.get("llm") or anthropic.LLM(
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        _strict_tool_schema=LLM_STRICT_TOOL_SCHEMA,
    )
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(
        min_speech_duration=VAD_MIN_SPEECH_DURATION,
        min_silence_duration=VAD_MIN_SILENCE_DURATION,
        activation_threshold=VAD_ACTIVATION_THRESHOLD,
    )

    # Warm the TLS connection while the caller is still connecting — free latency.
    warm_task = asyncio.create_task(_warm_llm_connection(llm_model))

    participant = await _wait_for_caller(ctx)
    # In console the "participant" is a MagicMock — don't try to read a number off it.
    caller_id = CONSOLE_CALLER_ID if console else _caller_id_from(participant)
    call_id = _first_string(getattr(ctx.room, "name", None)) or f"call-{uuid.uuid4().hex[:12]}"
    if console:
        call_id = f"console-{uuid.uuid4().hex[:8]}"
    ctx.log_context_fields = {"call_id": call_id, "caller_id": caller_id}

    # Layers 1 and 2: decide BEFORE building the pipeline. Console skips enforcement so
    # repeated local testing isn't blocked on the 4th run — set CONSOLE_ENFORCE_LIMITS=1
    # when you specifically want to exercise the over-limit hangup path.
    enforce = not console or _env_flag("CONSOLE_ENFORCE_LIMITS")
    decision = limiter.check(caller_id)
    if not decision.allowed and not enforce:
        logger.warning(
            "console: rate limit %s would have rejected this call; continuing anyway",
            decision.reason,
        )
    elif not decision.allowed:
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
        llm=llm_model,
        tts=cartesia.TTS(
            model=os.environ.get("CARTESIA_MODEL", "sonic-3"),
            language=language.tts_language,
            **({"voice": language.voice_id()} if language.voice_id() else {}),
        ),
        vad=vad,
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                timeout=LLM_TIMEOUT_SECONDS,
                max_retry=LLM_MAX_RETRY,
                retry_interval=LLM_RETRY_INTERVAL,
            ),
        ),
        turn_handling=TurnHandlingOptions(
            # Server defaults are calibrated, so overriding logs a warning — but the
            # calibration is wrong for us: it scored a complete sentence at 0.43.
            turn_detection=TurnDetector(unlikely_threshold=EOU_UNLIKELY_THRESHOLD),
            endpointing=EndpointingOptions(
                min_delay=MIN_ENDPOINTING_DELAY,
                max_delay=MAX_ENDPOINTING_DELAY,
            ),
            interruption=InterruptionOptions(
                min_duration=MIN_INTERRUPTION_DURATION,
                min_words=MIN_INTERRUPTION_WORDS,
            ),
            # Set explicitly rather than inherited — livekit defaults this to enabled,
            # so leaving it out is a decision either way. See PREEMPTIVE_GENERATION.
            preemptive_generation=PreemptiveGenerationOptions(
                enabled=PREEMPTIVE_GENERATION
            ),
        ),
    )

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        item = event.item
        if getattr(item, "role", None) in ("user", "assistant"):
            state.add_transcript(item.role, item.text_content or "")

    # Dead-air guard. A stalled LLM request (or a slow tool + long readback) left a
    # caller listening to silence long enough to ask "Hello? Can you hear me?" three
    # times. If we're still generating after THINKING_FILLER_DELAY, say so.
    filler_state: dict[str, object] = {"task": None, "index": 0}

    async def _speak_filler() -> None:
        try:
            await asyncio.sleep(THINKING_FILLER_DELAY)
        except asyncio.CancelledError:
            return  # generation finished in time — the common case
        # Never fill a thinking pause. Preemptive generation can put us in "thinking"
        # on a turn we are about to discard, and speaking here would just recreate the
        # interruption we removed — a filler on top of a caller mid-sentence.
        if state.awaiting_continuation:
            logger.debug("suppressing filler: caller is mid-sentence")
            return
        index = int(filler_state["index"])
        filler_state["index"] = index + 1
        line = THINKING_FILLERS[index % len(THINKING_FILLERS)]
        logger.info("generation exceeded %ss; speaking filler", THINKING_FILLER_DELAY)
        # Not added to the chat context: it's a UX noise-floor, not something the agent
        # should later believe it "said" and reason about.
        session.say(line, add_to_chat_ctx=False)

    @session.on("agent_state_changed")
    def _on_agent_state(event: AgentStateChangedEvent) -> None:
        task = filler_state["task"]
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            filler_state["task"] = None
        if event.new_state == "thinking":
            filler_state["task"] = asyncio.create_task(_speak_filler())

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
            end_reason=state.end_reason,
        )
        try:
            await deliver(payload)
        finally:
            limiter.close()

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(
        IntakeAgent(
            instructions=language.system_prompt(),
            tools=[capture_lead, take_message, end_call],
        ),
        room=ctx.room,
    )

    # Layer 3: force a readback + goodbye before the hard limit, then hang up.
    async def _enforce_time_limit() -> None:
        wrapup_at = max(30, config.max_call_seconds - WRAPUP_MARGIN_SECONDS)
        await asyncio.sleep(wrapup_at)
        logger.info("call %s hit the wrap-up threshold; forcing readback", call_id)
        state.end_reason = "time_limit"
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
        await _hangup(ctx, reason="call time limit reached")

    limit_task = asyncio.create_task(_enforce_time_limit())
    ctx.add_shutdown_callback(lambda: _cancel(limit_task))
    ctx.add_shutdown_callback(lambda: _cancel(warm_task))

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
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,
        )
    )

"""Pin the RESOLVED session config, not just our constants.

The bug class these guard against: livekit fills unspecified keys from its own
defaults, so "we didn't set it" is not "it's off". preemptive_generation defaults to
enabled, which we had wrongly assumed was off. Assert what the session actually
resolves to, so an upstream default that differs from our intent fails here rather
than on a live call.
"""

from __future__ import annotations

from livekit.agents import (
    AgentSession,
    APIConnectOptions,
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)
from livekit.agents.inference.eot import TurnDetector
from livekit.agents.voice.agent_session import SessionConnectOptions

import agent.main as main

# Two real scores from a live call. The endpointing delay is binary — below the
# threshold you wait max_delay, above it you wait min_delay — so the threshold must
# separate these two, or one of them is always handled wrong.
COMPLETE_SENTENCE_SCORE = 0.43  # "can I call you again? I wanna end the call" — done talking
MID_THOUGHT_SCORE = 0.17  # caller genuinely still thinking

# livekit's default LLM timeout. One stall at this value = 10s+ of silence before a
# retry is even attempted, which is what left a caller asking "Hello? Can you hear me?"
LIVEKIT_DEFAULT_LLM_TIMEOUT = 10.0


def _session() -> AgentSession:
    """Build the session exactly as main.py does.

    Callers must be async: AgentSession grabs the running event loop at construction.
    """
    return AgentSession(
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                timeout=main.LLM_TIMEOUT_SECONDS,
                max_retry=main.LLM_MAX_RETRY,
                retry_interval=main.LLM_RETRY_INTERVAL,
            ),
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=TurnDetector(unlikely_threshold=main.EOU_UNLIKELY_THRESHOLD),
            endpointing=EndpointingOptions(
                min_delay=main.MIN_ENDPOINTING_DELAY,
                max_delay=main.MAX_ENDPOINTING_DELAY,
            ),
            interruption=InterruptionOptions(
                min_duration=main.MIN_INTERRUPTION_DURATION,
                min_words=main.MIN_INTERRUPTION_WORDS,
            ),
            preemptive_generation=PreemptiveGenerationOptions(
                enabled=main.PREEMPTIVE_GENERATION
            ),
        ),
    )


def test_threshold_separates_a_finished_sentence_from_a_mid_thought():
    """The whole point of tuning the threshold instead of the delay.

    Above it, a complete-sounding sentence commits in min_delay (no dead air).
    Below it, a genuinely unfinished one gets max_delay (no cutting people off).
    """
    assert MID_THOUGHT_SCORE < main.EOU_UNLIKELY_THRESHOLD < COMPLETE_SENTENCE_SCORE


async def test_the_unsure_wait_is_patient_but_bounded():
    endpointing = _session().options.turn_handling["endpointing"]
    # Long enough for a slow thinker...
    assert endpointing["max_delay"] >= 3.0
    # ...but never so long the caller thinks the line dropped.
    assert endpointing["max_delay"] <= 3.5


async def test_endpointing_floor_is_below_the_ceiling():
    endpointing = _session().options.turn_handling["endpointing"]
    assert endpointing["min_delay"] < endpointing["max_delay"]


async def test_a_stalled_llm_cannot_produce_16_seconds_of_silence():
    conn = _session().conn_options.llm_conn_options
    assert conn.timeout < LIVEKIT_DEFAULT_LLM_TIMEOUT
    # Retry almost immediately rather than sitting on livekit's 2s interval.
    assert conn.retry_interval <= 0.5


def test_the_caller_hears_something_before_a_stalled_request_times_out():
    """The filler is the actual dead-air guard — it must fire well before the timeout."""
    assert main.THINKING_FILLER_DELAY < main.LLM_TIMEOUT_SECONDS
    assert main.THINKING_FILLERS  # and there must be something to say


async def test_preemptive_generation_is_set_explicitly_not_inherited():
    """It defaults to ON upstream. Whatever we want, we must say so out loud."""
    assert _session().options.preemptive_generation["enabled"] is main.PREEMPTIVE_GENERATION


def test_strict_tool_schema_stays_off():
    """It doubled time-to-first-token (p50 1730ms -> 897ms) and bought us nothing."""
    assert main.LLM_STRICT_TOOL_SCHEMA is False

"""Pin the RESOLVED session config, not just our constants.

The bug these guard against: livekit fills in unspecified turn-handling keys from its
own defaults, so "we didn't set it" is not the same as "it's off". preemptive_generation
defaults to enabled, which we had assumed was off. Assert what the session actually
resolves to, so an upstream default change fails here instead of on a live call.
"""

from __future__ import annotations

from livekit.agents import (
    AgentSession,
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)

import agent.main as main

# Past this, a caller hears silence and assumes the line is dead.
MAX_TOLERABLE_SILENCE_SECONDS = 2.0


def _session() -> AgentSession:
    """Build the session exactly as main.py does.

    Callers must be async: AgentSession grabs the running event loop at construction.
    """
    return AgentSession(
        turn_handling=TurnHandlingOptions(
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


async def test_unsure_turn_detector_never_leaves_a_long_dead_line():
    """max_delay is the wait when the end-of-turn model is UNSURE.

    That path is common, not rare — the model scored the complete sentence
    "can I call you again? I wanna end the call" at 0.43.
    """
    endpointing = _session().options.turn_handling["endpointing"]
    assert endpointing["max_delay"] <= MAX_TOLERABLE_SILENCE_SECONDS


async def test_endpointing_floor_is_below_the_ceiling():
    endpointing = _session().options.turn_handling["endpointing"]
    assert endpointing["min_delay"] < endpointing["max_delay"]


async def test_preemptive_generation_is_set_explicitly_not_inherited():
    """It defaults to ON upstream. Whatever we want, we must say so out loud."""
    assert _session().options.preemptive_generation["enabled"] is main.PREEMPTIVE_GENERATION


def test_strict_tool_schema_stays_off():
    """It doubled time-to-first-token (p50 1730ms -> 897ms) and bought us nothing."""
    assert main.LLM_STRICT_TOOL_SCHEMA is False

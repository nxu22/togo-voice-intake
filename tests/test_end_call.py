"""The call stays open until the agent hangs up.

Before end_call existed, the agent said its closing line and the session just kept
listening — there was no path from "goodbye" to "line drops" on a normal call.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from agent.tools import CallState, end_call

STARTED = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)


class FakeRunContext:
    """Records the order of operations so we can prove we don't cut the goodbye off."""

    def __init__(self, state: CallState, calls: list[str]) -> None:
        self.userdata = state
        self._calls = calls

    async def wait_for_playout(self) -> None:
        self._calls.append("wait_for_playout")


class FakeJobContext:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.shutdown_reason: str | None = None

    async def delete_room(self) -> None:
        self._calls.append("delete_room")

    def shutdown(self, reason: str = "") -> None:
        self._calls.append("shutdown")
        self.shutdown_reason = reason


@pytest.fixture
def state() -> CallState:
    return CallState(call_id="c1", caller_id="+12045550101", started_at=STARTED)


def test_default_end_reason_is_caller_hangup(state):
    # If we never hang up, the caller dropped — that's the only remaining explanation.
    assert state.end_reason == "caller_hangup"


async def test_end_call_hangs_up_and_ends_the_job(state, monkeypatch):
    calls: list[str] = []
    job = FakeJobContext(calls)
    monkeypatch.setattr("agent.tools.get_job_context", lambda required=True: job)

    result = await end_call(FakeRunContext(state, calls))

    assert state.end_reason == "agent_goodbye"
    assert "Call ended" in result
    # shutdown() is the part that actually ends a console session — delete_room no-ops there.
    assert "shutdown" in calls
    assert job.shutdown_reason == "agent ended the call"


async def test_goodbye_finishes_before_the_line_drops(state, monkeypatch):
    """The whole point of the readback rule: never cut the caller off mid-sentence."""
    calls: list[str] = []
    job = FakeJobContext(calls)
    monkeypatch.setattr("agent.tools.get_job_context", lambda required=True: job)

    await end_call(FakeRunContext(state, calls))

    assert calls.index("wait_for_playout") < calls.index("delete_room")
    assert calls.index("wait_for_playout") < calls.index("shutdown")


async def test_end_call_without_a_job_context_does_not_explode(state, monkeypatch):
    """The text simulation has no room and no job to tear down."""
    calls: list[str] = []
    monkeypatch.setattr("agent.tools.get_job_context", lambda required=True: None)

    result = await end_call(FakeRunContext(state, calls))

    assert "Call ended" in result
    assert state.end_reason == "agent_goodbye"
    assert calls == ["wait_for_playout"]


async def test_a_failed_delete_room_still_ends_the_job(state, monkeypatch):
    """A dead room must not strand the session listening forever."""
    calls: list[str] = []

    class BrokenJobContext(FakeJobContext):
        async def delete_room(self) -> None:
            raise RuntimeError("room already gone")

    job = BrokenJobContext(calls)
    monkeypatch.setattr("agent.tools.get_job_context", lambda required=True: job)

    await end_call(FakeRunContext(state, calls))

    assert "shutdown" in calls

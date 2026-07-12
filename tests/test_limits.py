from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.limits import LimitConfig, RateLimiter

WINNIPEG = "America/Winnipeg"

# 2026-07-12 18:00 UTC == 13:00 Winnipeg (CDT, UTC-5). Mid-afternoon, same day both ways.
BASE = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)


class Clock:
    """Injectable clock so we can cross midnight without waiting for it."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def config() -> LimitConfig:
    return LimitConfig(
        daily_call_cap=50,
        daily_spend_cap_usd=10.00,
        cost_per_minute_usd=0.08,
        per_caller_daily_cap=3,
        max_call_seconds=600,
        timezone=WINNIPEG,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(BASE)


@pytest.fixture
def limiter(tmp_path, config, clock) -> RateLimiter:
    with RateLimiter(tmp_path / "limits.db", config=config, now=clock) as limiter:
        yield limiter


def _run_call(limiter: RateLimiter, caller: str, call_id: str, seconds: float) -> None:
    limiter.start_call(call_id, caller)
    limiter.end_call(call_id, seconds)


def test_fresh_line_accepts_calls(limiter):
    assert limiter.check("+12045550101").allowed


def test_unknown_caller_id_is_still_rate_limited(limiter, config):
    for i in range(config.per_caller_daily_cap):
        _run_call(limiter, None, f"c{i}", 60)
    decision = limiter.check(None)
    assert not decision.allowed
    assert decision.reason == "per_caller_cap"


def test_per_caller_cap_blocks_the_fourth_call(limiter, config):
    caller = "+12045550101"
    for i in range(config.per_caller_daily_cap):
        assert limiter.check(caller).allowed
        _run_call(limiter, caller, f"c{i}", 60)

    decision = limiter.check(caller)
    assert not decision.allowed
    assert decision.reason == "per_caller_cap"
    assert "call back tomorrow" in decision.message

    # A different caller is unaffected.
    assert limiter.check("+12045550202").allowed


def test_daily_call_cap_blocks_call_51(limiter, config):
    for i in range(config.daily_call_cap):
        _run_call(limiter, f"+1204555{i:04d}", f"c{i}", 30)

    decision = limiter.check("+12045559999")
    assert not decision.allowed
    assert decision.reason == "daily_call_cap"


def test_spend_estimate_math(limiter, config):
    # 10 calls x 5 minutes = 50 minutes @ $0.08/min = $4.00
    for i in range(10):
        _run_call(limiter, f"+1204555{i:04d}", f"c{i}", 5 * 60)

    assert limiter.estimated_spend_usd() == pytest.approx(4.00)
    assert limiter.check("+12045559999").allowed


def test_spend_cap_blocks_before_the_call_cap(limiter, config):
    # 25 calls x 5 min = 125 min @ $0.08 = $10.00 — hits the $10 cap at 25 calls,
    # well before the 50-call cap.
    for i in range(25):
        _run_call(limiter, f"+1204555{i:04d}", f"c{i}", 5 * 60)

    assert limiter.estimated_spend_usd() == pytest.approx(10.00)
    stats = limiter.daily_stats()
    assert stats.calls_today == 25  # under the 50-call cap

    decision = limiter.check("+12045559999")
    assert not decision.allowed
    assert decision.reason == "daily_spend_cap"


def test_spend_just_under_the_cap_still_allows_a_call(limiter):
    # 124 minutes @ $0.08 = $9.92
    for i in range(24):
        _run_call(limiter, f"+1204555{i:04d}", f"c{i}", 310)

    assert limiter.estimated_spend_usd() == pytest.approx(9.92)
    assert limiter.check("+12045559999").allowed


def test_counters_reset_at_midnight_winnipeg(limiter, clock, config):
    caller = "+12045550101"
    for i in range(config.per_caller_daily_cap):
        _run_call(limiter, caller, f"c{i}", 5 * 60)
    assert not limiter.check(caller).allowed
    assert limiter.daily_stats().calls_today == 3

    # 23:59 Winnipeg — still the same day, still blocked.
    clock.now = datetime(2026, 7, 13, 4, 59, tzinfo=timezone.utc)  # 23:59 local
    assert not limiter.check(caller).allowed

    # 00:01 Winnipeg the next day — new day key, everything resets.
    clock.now = datetime(2026, 7, 13, 5, 1, tzinfo=timezone.utc)  # 00:01 local
    assert limiter.check(caller).allowed
    stats = limiter.daily_stats()
    assert stats.calls_today == 0
    assert stats.minutes_today == 0
    assert limiter.estimated_spend_usd() == 0.0


def test_yesterdays_spend_does_not_count_against_today(limiter, clock):
    for i in range(25):
        _run_call(limiter, f"+1204555{i:04d}", f"c{i}", 5 * 60)
    assert not limiter.check("+12045559999").allowed

    clock.advance(days=1)
    assert limiter.check("+12045559999").allowed
    assert limiter.estimated_spend_usd() == pytest.approx(0.0)


def test_daily_stats_tracks_captured_leads(limiter):
    _run_call(limiter, "+12045550101", "c1", 120)
    _run_call(limiter, "+12045550202", "c2", 180)
    limiter.mark_lead_captured("c1")

    stats = limiter.daily_stats()
    assert stats.calls_today == 2
    assert stats.minutes_today == pytest.approx(5.0)
    assert stats.leads_captured_today == 1
    assert stats.to_dict() == {
        "calls_today": 2,
        "minutes_today": 5.0,
        "leads_captured_today": 1,
    }


def test_in_progress_call_counts_toward_the_call_cap(limiter):
    limiter.start_call("c1", "+12045550101")
    # Not ended yet: it counts as a call, but contributes no minutes.
    stats = limiter.daily_stats()
    assert stats.calls_today == 1
    assert stats.minutes_today == 0.0


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("DAILY_CALL_CAP", "100")
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "20")
    monkeypatch.setenv("ESTIMATED_COST_PER_MINUTE_USD", "0.05")
    monkeypatch.setenv("PER_CALLER_DAILY_CAP", "5")
    monkeypatch.setenv("MAX_CALL_SECONDS", "900")

    config = LimitConfig.from_env()
    assert config.daily_call_cap == 100
    assert config.daily_spend_cap_usd == 20.0
    assert config.cost_per_minute_usd == 0.05
    assert config.per_caller_daily_cap == 5
    assert config.max_call_seconds == 900

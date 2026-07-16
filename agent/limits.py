"""Three-layer rate limiting for the Togo intake line.

Layer 1 (daily):      max calls/day AND estimated spend cap/day, across all callers.
Layer 2 (per-caller): same caller ID max N calls/day.
Layer 3 (per-call):   hard wall-clock limit, enforced by the caller of this module.

Counters live in SQLite and reset at midnight in RATE_LIMIT_TIMEZONE. "Today" is
derived from the wall clock in that timezone, so no cron job is needed — a call at
00:01 Winnipeg time simply lands under a new day key.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# agent/limits.py -> project root. Runtime state (the SQLite counters, and
# failed_webhooks/ via postcall.py) must land in the same place no matter which
# directory the worker was launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_under_root(path: str | Path) -> Path:
    """Anchor a relative path to the project root rather than the process cwd.

    `python agent/main.py`, `python scripts/simulate_call.py`, and a worker launched
    by a service manager all have different cwds; before this, each would grow its own
    limits.db and failed_webhooks/ wherever it happened to be started. Absolute paths
    pass through untouched, so the env vars can still point anywhere deliberate.
    """
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


# Spoken when a cap is hit. One line, then hang up — no STT/LLM loop is started.
DAILY_LIMIT_MESSAGE = (
    "Thanks for calling Togo AI Automation — our demo line has reached its daily "
    "limit. Please leave your details on our website, or call back tomorrow."
)
PER_CALLER_LIMIT_MESSAGE = (
    "Thanks for calling Togo AI Automation — you've reached our demo line a few "
    "times today already. Please leave your details on our website, or call back "
    "tomorrow."
)

UNKNOWN_CALLER = "unknown"


def normalize_caller_id(caller_id: object) -> str:
    """Coerce a caller ID into something SQLite can actually bind.

    Console mode hands us an autospec MagicMock whose attributes are truthy mocks,
    so "is it set?" is not the same question as "is it a usable string?". Anything
    that isn't a non-empty string becomes UNKNOWN_CALLER rather than reaching the DB.
    """
    if isinstance(caller_id, str) and caller_id.strip():
        return caller_id.strip()
    return UNKNOWN_CALLER


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class LimitConfig:
    daily_call_cap: int = 50
    daily_spend_cap_usd: float = 10.00
    cost_per_minute_usd: float = 0.08
    per_caller_daily_cap: int = 3
    max_call_seconds: int = 600
    timezone: str = "America/Winnipeg"

    @classmethod
    def from_env(cls) -> LimitConfig:
        return cls(
            daily_call_cap=_env_int("DAILY_CALL_CAP", 50),
            daily_spend_cap_usd=_env_float("DAILY_SPEND_CAP_USD", 10.00),
            cost_per_minute_usd=_env_float("ESTIMATED_COST_PER_MINUTE_USD", 0.08),
            per_caller_daily_cap=_env_int("PER_CALLER_DAILY_CAP", 3),
            max_call_seconds=_env_int("MAX_CALL_SECONDS", 600),
            timezone=os.environ.get("RATE_LIMIT_TIMEZONE", "America/Winnipeg"),
        )


@dataclass(frozen=True)
class Decision:
    """Outcome of a pre-pipeline rate-limit check."""

    allowed: bool
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class DailyStats:
    calls_today: int
    minutes_today: float
    leads_captured_today: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "calls_today": self.calls_today,
            "minutes_today": round(self.minutes_today, 2),
            "leads_captured_today": self.leads_captured_today,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id          TEXT PRIMARY KEY,
    caller_id        TEXT NOT NULL,
    day              TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    duration_seconds REAL,
    lead_captured    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls (day);
CREATE INDEX IF NOT EXISTS idx_calls_day_caller ON calls (day, caller_id);
"""


class RateLimiter:
    """SQLite-backed call counters.

    `now` is injectable so tests can drive the clock across a midnight boundary
    without sleeping or monkeypatching the stdlib.
    """

    def __init__(
        self,
        db_path: str | Path,
        config: LimitConfig | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or LimitConfig()
        self._tz = ZoneInfo(self.config.timezone)
        self._now = now or (lambda: datetime.now(timezone.utc))

        db_path = resolve_under_root(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # Each call runs in its own job process, so several may touch the file at once.
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> RateLimiter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def today(self) -> str:
        """The current local day key. Rolls over at midnight in the configured tz."""
        return self._now().astimezone(self._tz).date().isoformat()

    def check(self, caller_id: object) -> Decision:
        """Decide whether to run the full pipeline for an incoming call.

        Must be called BEFORE the STT/LLM/TTS pipeline starts.
        """
        caller_id = normalize_caller_id(caller_id)
        day = self.today()
        stats = self.daily_stats(day)

        if stats.calls_today >= self.config.daily_call_cap:
            return Decision(False, "daily_call_cap", DAILY_LIMIT_MESSAGE)

        if self.estimated_spend_usd(day) >= self.config.daily_spend_cap_usd:
            return Decision(False, "daily_spend_cap", DAILY_LIMIT_MESSAGE)

        if self.calls_by_caller(caller_id, day) >= self.config.per_caller_daily_cap:
            return Decision(False, "per_caller_cap", PER_CALLER_LIMIT_MESSAGE)

        return Decision(True)

    def estimated_spend_usd(self, day: str | None = None) -> float:
        """Estimated spend so far today, from completed call minutes."""
        day = day or self.today()
        minutes = self._minutes(day)
        return minutes * self.config.cost_per_minute_usd

    def calls_by_caller(self, caller_id: object, day: str | None = None) -> int:
        day = day or self.today()
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE day = ? AND caller_id = ?",
            (day, normalize_caller_id(caller_id)),
        ).fetchone()
        return int(row["n"])

    def start_call(self, call_id: str, caller_id: object) -> None:
        """Record a call as started. Counts against the daily cap immediately."""
        now = self._now()
        self._db.execute(
            "INSERT OR REPLACE INTO calls (call_id, caller_id, day, started_at) "
            "VALUES (?, ?, ?, ?)",
            (
                str(call_id),
                normalize_caller_id(caller_id),
                now.astimezone(self._tz).date().isoformat(),
                now.isoformat(),
            ),
        )

    def end_call(self, call_id: str, duration_seconds: float) -> None:
        self._db.execute(
            "UPDATE calls SET ended_at = ?, duration_seconds = ? WHERE call_id = ?",
            (self._now().isoformat(), float(duration_seconds), call_id),
        )

    def mark_lead_captured(self, call_id: str) -> None:
        self._db.execute(
            "UPDATE calls SET lead_captured = 1 WHERE call_id = ?", (call_id,)
        )

    def daily_stats(self, day: str | None = None) -> DailyStats:
        """Running stats for the webhook payload's daily summary."""
        day = day or self.today()
        row = self._db.execute(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(duration_seconds), 0) AS seconds, "
            "       COALESCE(SUM(lead_captured), 0) AS leads "
            "FROM calls WHERE day = ?",
            (day,),
        ).fetchone()
        return DailyStats(
            calls_today=int(row["calls"]),
            minutes_today=float(row["seconds"]) / 60.0,
            leads_captured_today=int(row["leads"]),
        )

    def _minutes(self, day: str) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0) AS seconds FROM calls WHERE day = ?",
            (day,),
        ).fetchone()
        return float(row["seconds"]) / 60.0

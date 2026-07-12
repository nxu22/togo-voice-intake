"""Post-call delivery: POST the lead to n8n, with retries and a disk fallback.

No lead is ever lost. If all retries fail, the payload is written to
failed_webhooks/ as JSON so it can be replayed by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from agent.limits import DailyStats
from agent.tools import CallState

logger = logging.getLogger("togo.postcall")

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 10.0


def build_payload(
    state: CallState,
    stats: DailyStats,
    *,
    completed: bool,
    end_reason: str,
) -> dict[str, object]:
    """The exact shape n8n expects (see CLAUDE.md v1 requirement 4)."""
    return {
        "call_id": state.call_id,
        "caller_id": state.caller_id,
        "started_at": state.started_at.isoformat(),
        "duration_seconds": round(state.duration_seconds(), 1),
        "completed": completed,
        "end_reason": end_reason,
        "lead": state.lead.to_dict(),
        "messages": list(state.lead.messages),
        "transcript": list(state.transcript),
        "daily_stats": stats.to_dict(),
    }


def failed_webhook_dir() -> Path:
    return Path(os.environ.get("FAILED_WEBHOOKS_DIR", "failed_webhooks"))


def write_failed(payload: dict[str, object], directory: Path | None = None) -> Path:
    """Persist an undeliverable payload so the lead survives the failure."""
    directory = directory or failed_webhook_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload.get('call_id', 'unknown')}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.error("webhook delivery failed; payload written to %s", path)
    return path


async def deliver(
    payload: dict[str, object],
    url: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base: float = BACKOFF_BASE_SECONDS,
    failed_dir: Path | None = None,
) -> bool:
    """POST the payload, retrying with exponential backoff.

    Returns True on delivery. On final failure, writes the payload to disk and
    returns False — it never raises, because a webhook problem must not take the
    call down with it.
    """
    url = url or os.environ.get("N8N_WEBHOOK_URL", "")
    if not url:
        logger.warning("N8N_WEBHOOK_URL is not set; writing payload to disk instead")
        write_failed(payload, failed_dir)
        return False

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                logger.info(
                    "webhook delivered for call %s (attempt %d)",
                    payload.get("call_id"),
                    attempt,
                )
                return True
            except Exception as exc:  # noqa: BLE001 — any failure is retryable here
                logger.warning(
                    "webhook attempt %d/%d failed: %s", attempt, max_attempts, exc
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
    finally:
        if owns_client:
            await client.aclose()

    write_failed(payload, failed_dir)
    return False

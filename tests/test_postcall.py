from __future__ import annotations

import json

import httpx
import pytest

from agent.postcall import deliver, write_failed

WEBHOOK = "https://n8n.example.com/webhook/togo-intake"

PAYLOAD = {"call_id": "call-1", "lead": {"industry": "bakery"}}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_delivers_on_first_attempt(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    async with _client(handler) as client:
        ok = await deliver(
            PAYLOAD, WEBHOOK, client=client, backoff_base=0, failed_dir=tmp_path
        )

    assert ok
    assert seen == [PAYLOAD]
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


async def test_retries_then_succeeds(tmp_path):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        # Fail twice, succeed on the third — exercises the retry path.
        return httpx.Response(200 if len(attempts) == 3 else 500)

    async with _client(handler) as client:
        ok = await deliver(
            PAYLOAD, WEBHOOK, client=client, backoff_base=0, failed_dir=tmp_path
        )

    assert ok
    assert len(attempts) == 3
    assert list(tmp_path.iterdir()) == []


async def test_gives_up_after_three_attempts_and_writes_to_disk(tmp_path):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500)

    async with _client(handler) as client:
        ok = await deliver(
            PAYLOAD, WEBHOOK, client=client, backoff_base=0, failed_dir=tmp_path
        )

    assert not ok
    assert len(attempts) == 3  # exactly 3, not 4
    saved = tmp_path / "call-1.json"
    assert json.loads(saved.read_text()) == PAYLOAD  # the lead survived


async def test_connection_error_also_falls_back_to_disk(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("n8n is down")

    async with _client(handler) as client:
        ok = await deliver(
            PAYLOAD, WEBHOOK, client=client, backoff_base=0, failed_dir=tmp_path
        )

    assert not ok
    assert (tmp_path / "call-1.json").is_file()


async def test_missing_webhook_url_writes_to_disk_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)

    ok = await deliver(PAYLOAD, url=None, backoff_base=0, failed_dir=tmp_path)

    assert not ok
    assert (tmp_path / "call-1.json").is_file()


def test_write_failed_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "failed_webhooks"
    path = write_failed(PAYLOAD, target)
    assert path.is_file()
    assert json.loads(path.read_text())["call_id"] == "call-1"

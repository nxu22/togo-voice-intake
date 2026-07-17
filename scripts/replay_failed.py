"""Replay undelivered webhook payloads from failed_webhooks/.

    python scripts/replay_failed.py                  # list what's in the outbox
    python scripts/replay_failed.py <call_id> ...    # replay specific payload(s)
    python scripts/replay_failed.py --all            # replay everything

A payload is deleted from the outbox only after n8n confirms delivery (2xx), so a
failed replay leaves it exactly where it was. Replaying sends the lead into n8n for
real — email + Airtable row — so pick what you replay.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.postcall import deliver, failed_webhook_dir

load_dotenv()


def _summary(payload: dict) -> str:
    contact = (payload.get("lead") or {}).get("contact") or {}
    who = contact.get("phone_or_email") or "(no contact)"
    return (
        f"completed={payload.get('completed')}  end_reason={payload.get('end_reason')}  "
        f"contact={who}"
    )


async def main() -> int:
    args = sys.argv[1:]
    outbox = failed_webhook_dir()
    files = sorted(outbox.glob("*.json"))

    if not files:
        print(f"outbox is empty: {outbox}")
        return 0

    if not args:
        print(f"{len(files)} undelivered payload(s) in {outbox}:\n")
        for path in files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            print(f"  {path.stem}: {_summary(payload)}")
        print("\nreplay with: python scripts/replay_failed.py <call_id> ...   or --all")
        return 0

    if args != ["--all"]:
        wanted = set(args)
        files = [f for f in files if f.stem in wanted]
        missing = wanted - {f.stem for f in files}
        if missing:
            print(f"not in outbox: {', '.join(sorted(missing))}")
            return 1

    failures = 0
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"replaying {path.stem}: {_summary(payload)}")
        # deliver() retries 3x; on final failure it re-writes the payload to the
        # outbox under the same call_id, i.e. right back where it came from.
        if await deliver(payload):
            path.unlink()
            print("  -> delivered; removed from outbox")
        else:
            failures += 1
            print("  -> STILL FAILING; left in outbox")

    remaining = len(list(outbox.glob("*.json")))
    print(f"\ndone: {len(files) - failures} delivered, {failures} failed, {remaining} left in outbox")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

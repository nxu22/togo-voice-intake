"""Provision the LiveKit inbound SIP trunk + dispatch rule for the intake line.

This is the LiveKit half of the Twilio -> LiveKit wiring. It is inert on its own: until
a Twilio Elastic SIP Trunk points its origination at this project's SIP host, nothing
routes here. Reversible — the teardown commands are printed at the end.

    python scripts/setup_livekit_sip.py           # create (idempotent)
    python scripts/setup_livekit_sip.py --show     # just print current state + SIP host

Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from .env.

What it creates:
  - an inbound trunk PINNED to our DID, so the trunk only answers calls to that number
  - a dispatch rule that puts each caller in their own room and dispatches the
    "togo-intake" agent (explicit named dispatch — see agent/main.py AGENT_NAME)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from livekit import api

from agent.main import AGENT_NAME

load_dotenv()

PHONE = "+14318307788"  # the Twilio DID, Manitoba
TRUNK_NAME = "Togo Intake — Twilio inbound"
RULE_NAME = "Togo Intake inbound → agent"
ROOM_PREFIX = "togo-call"


def sip_host_from_ws_url() -> str:
    """Best-guess LiveKit Cloud SIP host, derived from LIVEKIT_URL.

    A LiveKit Cloud project's SIP endpoint shares the project subdomain:
    wss://myproj-ab12cd.livekit.cloud  ->  myproj-ab12cd.sip.livekit.cloud
    CONFIRM this against the dashboard (Project Settings) before wiring Twilio — the
    dashboard value is authoritative.
    """
    host = os.environ.get("LIVEKIT_URL", "").split("://")[-1].strip("/")
    if host.endswith(".livekit.cloud"):
        sub = host[: -len(".livekit.cloud")]
        return f"{sub}.sip.livekit.cloud"
    return "(non-cloud LIVEKIT_URL — find the SIP host in your dashboard)"


async def main() -> int:
    show_only = "--show" in sys.argv[1:]
    lk = api.LiveKitAPI()
    try:
        trunks = (await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
        rules = (await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())).items

        trunk = next((t for t in trunks if PHONE in t.numbers), None)

        if show_only:
            print(f"inbound trunks: {len(trunks)}  dispatch rules: {len(rules)}")
            if trunk:
                print(f"  trunk for {PHONE}: {trunk.sip_trunk_id}")
            _print_twilio_hint()
            return 0

        if trunk:
            print(f"inbound trunk already pins {PHONE}: {trunk.sip_trunk_id}")
        else:
            created = await lk.sip.create_sip_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(
                    trunk=api.SIPInboundTrunkInfo(name=TRUNK_NAME, numbers=[PHONE])
                )
            )
            trunk = created
            print(f"created inbound trunk: {trunk.sip_trunk_id}  (pinned to {PHONE})")

        rule = next((r for r in rules if trunk.sip_trunk_id in r.trunk_ids), None)
        if rule:
            print(f"dispatch rule already targets this trunk: {rule.sip_dispatch_rule_id}")
        else:
            created_rule = await lk.sip.create_sip_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    name=RULE_NAME,
                    trunk_ids=[trunk.sip_trunk_id],
                    rule=api.SIPDispatchRule(
                        dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                            room_prefix=ROOM_PREFIX
                        )
                    ),
                    # Explicit named dispatch: only the "togo-intake" worker is pulled in.
                    room_config=api.RoomConfiguration(
                        agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
                    ),
                )
            )
            print(
                f"created dispatch rule: {created_rule.sip_dispatch_rule_id}  "
                f"(room per caller -> agent {AGENT_NAME!r})"
            )

        _print_twilio_hint()
        print("\nteardown, if ever needed:")
        print(f"  lk sip inbound delete {trunk.sip_trunk_id}")
        print("  (or delete via the LiveKit dashboard -> Telephony)")
        return 0
    finally:
        await lk.aclose()


def _print_twilio_hint() -> None:
    print("\n" + "=" * 64)
    print("Point your Twilio trunk's Origination URI at this LiveKit SIP host:")
    print(f"\n    sip:{sip_host_from_ws_url()};transport=tcp\n")
    print("Confirm the host against LiveKit dashboard -> Project Settings before use.")
    print("=" * 64)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

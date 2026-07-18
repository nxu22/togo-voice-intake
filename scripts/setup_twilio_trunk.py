"""Wire the Twilio DID to LiveKit via an Elastic SIP Trunk (inbound).

The Twilio half of the Twilio -> LiveKit chain. Run the LiveKit half first
(scripts/setup_livekit_sip.py) so the SIP host below is confirmed.

Credentials come from the SHELL ENVIRONMENT only — never .env. Set them for this run:

    # PowerShell
    $env:TWILIO_ACCOUNT_SID = "AC..."      # Console -> Account Settings
    $env:TWILIO_AUTH_TOKEN  = "..."
    pip install twilio
    python scripts/setup_twilio_trunk.py

    # bash
    TWILIO_ACCOUNT_SID=AC... TWILIO_AUTH_TOKEN=... python scripts/setup_twilio_trunk.py

What it does, idempotently:
  1. finds the incoming phone number's SID for the DID
  2. creates (or reuses) an Elastic SIP Trunk
  3. adds an Origination URL pointing at the LiveKit SIP host
  4. associates the DID with the trunk, which routes inbound PSTN calls to LiveKit

Safe to run twice: existing trunk/origination/association are detected and left alone.
"""

from __future__ import annotations

import os
import sys

DID = "+14318307788"
TRUNK_FRIENDLY_NAME = "Togo Intake -> LiveKit"

# Derived from LIVEKIT_URL by scripts/setup_livekit_sip.py. CONFIRM against the LiveKit
# dashboard (Project Settings) — a wrong host here means calls connect on the PSTN side
# and then silently fail to reach the agent. Override with LIVEKIT_SIP_HOST if needed.
LIVEKIT_SIP_HOST = os.environ.get(
    "LIVEKIT_SIP_HOST", "togo-voice-agent-0pyszkt6.sip.livekit.cloud"
)
ORIGINATION_URI = f"sip:{LIVEKIT_SIP_HOST};transport=tcp"


def _require(name: str, prefix: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(
            f"{name} is not set. Put it in your shell for this run only (not .env):\n"
            f'  PowerShell:  $env:{name} = "{prefix}..."\n'
            f"  bash:        export {name}={prefix}..."
        )
    if not value.startswith(prefix):
        print(f"warning: {name} does not start with {prefix!r}", file=sys.stderr)
    return value


def main() -> int:
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    account_sid = _require("TWILIO_ACCOUNT_SID", "AC")
    auth_token = _require("TWILIO_AUTH_TOKEN", "")
    client = Client(account_sid, auth_token)

    print(f"DID:              {DID}")
    print(f"LiveKit SIP host: {LIVEKIT_SIP_HOST}")
    print(f"origination URI:  {ORIGINATION_URI}\n")

    # 1. Find the phone number's SID.
    numbers = client.incoming_phone_numbers.list(phone_number=DID, limit=1)
    if not numbers:
        sys.exit(
            f"{DID} is not an incoming number on this Twilio account. Check the DID and "
            f"that TWILIO_ACCOUNT_SID is the account that owns it."
        )
    number_sid = numbers[0].sid
    print(f"1. found DID: {number_sid}")

    # 2. Create or reuse the trunk.
    trunk = next(
        (t for t in client.trunking.v1.trunks.list() if t.friendly_name == TRUNK_FRIENDLY_NAME),
        None,
    )
    if trunk:
        print(f"2. reusing trunk: {trunk.sid}")
    else:
        trunk = client.trunking.v1.trunks.create(friendly_name=TRUNK_FRIENDLY_NAME)
        print(f"2. created trunk: {trunk.sid}  (termination domain: {trunk.domain_name})")

    # 3. Origination URL -> LiveKit (where Twilio sends inbound calls).
    existing_orig = client.trunking.v1.trunks(trunk.sid).origination_urls.list()
    if any(o.sip_url == ORIGINATION_URI for o in existing_orig):
        print("3. origination URL already points at LiveKit")
    else:
        client.trunking.v1.trunks(trunk.sid).origination_urls.create(
            friendly_name="LiveKit Cloud SIP",
            sip_url=ORIGINATION_URI,
            weight=1,
            priority=1,
            enabled=True,
        )
        print(f"3. added origination URL -> {ORIGINATION_URI}")

    # 4. Associate the DID with the trunk (this is what routes inbound calls).
    attached = client.trunking.v1.trunks(trunk.sid).phone_numbers.list()
    if any(p.sid == number_sid for p in attached):
        print("4. DID already attached to this trunk")
    else:
        try:
            client.trunking.v1.trunks(trunk.sid).phone_numbers.create(
                phone_number_sid=number_sid
            )
            print("4. attached DID to trunk")
        except TwilioRestException as exc:
            sys.exit(
                f"could not attach {DID} to the trunk: {exc.msg}\n"
                f"If it's attached to a different trunk, detach it there first."
            )

    print(f"\nDone. Trunk {trunk.sid} routes {DID} -> {ORIGINATION_URI}")
    print("Next: place a test call once the agent worker is deployed and running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

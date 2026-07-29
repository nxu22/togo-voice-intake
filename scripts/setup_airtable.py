"""One-off: create the "Togo Intake Leads" table in an Airtable base.

This is provisioning only. The agent NEVER talks to Airtable at runtime — it POSTs to
the n8n webhook, and n8n writes the row. Airtable credentials live in n8n's credential
store, not in this repo.

Accordingly, this script reads its credentials from the SHELL ENVIRONMENT ONLY. It does
not load .env, and the two variables below are deliberately absent from .env.example.

    # PowerShell
    $env:AIRTABLE_API_KEY = "pat..."      # needs the schema.bases:write scope
    $env:AIRTABLE_BASE_ID = "app..."
    python scripts/setup_airtable.py

    # bash
    AIRTABLE_API_KEY=pat... AIRTABLE_BASE_ID=app... python scripts/setup_airtable.py

Safe to run twice: if the table already exists it prints the existing ID and changes
nothing.
"""

from __future__ import annotations

import os
import sys

import httpx

TABLE_NAME = "Togo Intake Leads"
API_ROOT = "https://api.airtable.com/v0/meta/bases"
TIMEOUT_SECONDS = 30.0

# Mirrors the webhook payload built in agent/postcall.py:build_payload().
# The FIRST field becomes Airtable's primary field, so it must be a text type.
FIELDS: list[dict[str, object]] = [
    {"name": "Call ID", "type": "singleLineText"},  # payload: call_id  (primary)
    {
        "name": "Date",
        "type": "date",
        "options": {"dateFormat": {"name": "iso"}},
    },  # payload: started_at
    {
        "name": "Completed",
        "type": "checkbox",
        "options": {"icon": "check", "color": "greenBright"},
    },  # payload: completed — contact info AND industry AND biggest_time_sink
    {"name": "End Reason", "type": "singleLineText"},  # agent_goodbye|time_limit|caller_hangup
    {
        "name": "Duration",
        "type": "number",
        "options": {"precision": 1},
    },  # payload: duration_seconds
    {"name": "Industry", "type": "singleLineText"},
    {"name": "Biggest Time Sink", "type": "singleLineText"},
    {"name": "Current Process", "type": "singleLineText"},
    {"name": "Frequency", "type": "singleLineText"},
    {"name": "Tools Used", "type": "singleLineText"},
    {"name": "Desired Outcome", "type": "singleLineText"},
    {"name": "Contact Name", "type": "singleLineText"},
    {"name": "Contact Phone/Email", "type": "singleLineText"},
    {"name": "Messages", "type": "multilineText"},  # payload: messages[] — off-script questions
]


def _require_env(name: str, expected_prefix: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(
            f"{name} is not set.\n"
            f"Set it in your shell for this run only — do not put it in .env:\n"
            f'  PowerShell:  $env:{name} = "{expected_prefix}..."\n'
            f"  bash:        export {name}={expected_prefix}..."
        )
    if not value.startswith(expected_prefix):
        print(
            f"warning: {name} does not start with {expected_prefix!r} — check you "
            f"pasted the right value",
            file=sys.stderr,
        )
    return value


def _existing_table(client: httpx.Client, base_id: str) -> dict | None:
    response = client.get(f"{API_ROOT}/{base_id}/tables")
    _raise_for_airtable(response)
    for table in response.json().get("tables", []):
        if table.get("name") == TABLE_NAME:
            return table
    return None


def _raise_for_airtable(response: httpx.Response) -> None:
    if response.is_success:
        return

    hint = ""
    if response.status_code in (401, 403):
        hint = (
            "\nHint: the token needs the 'schema.bases:write' scope AND this base must "
            "be listed under the token's access. Both are set where you created the PAT."
        )
    elif response.status_code == 404:
        hint = "\nHint: check AIRTABLE_BASE_ID — it should start with 'app'."

    sys.exit(f"Airtable API error {response.status_code}: {response.text}{hint}")


def main() -> int:
    api_key = _require_env("AIRTABLE_API_KEY", "pat")
    base_id = _require_env("AIRTABLE_BASE_ID", "app")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(headers=headers, timeout=TIMEOUT_SECONDS) as client:
        existing = _existing_table(client, base_id)
        if existing:
            print(f"Table {TABLE_NAME!r} already exists — nothing to do.")
            print(f"\n  table id: {existing['id']}")
            return 0

        response = client.post(
            f"{API_ROOT}/{base_id}/tables",
            json={
                "name": TABLE_NAME,
                "description": "Leads captured by the Togo AI Automation phone intake agent.",
                "fields": FIELDS,
            },
        )
        _raise_for_airtable(response)
        table = response.json()

    print(f"Created table {TABLE_NAME!r} with {len(FIELDS)} fields.")
    print(f"\n  table id: {table['id']}")
    print("\nNext: point the n8n workflow's Airtable node at this table, and put the")
    print("PAT in n8n's credential store — not in this repo's .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

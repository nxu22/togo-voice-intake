"""Console mode hands the entrypoint an autospec MagicMock room, not a real one.

livekit's ipc/mock_room.py pins only `sid` and `identity` to real strings — every
other attribute, including `attributes`, is left as a truthy MagicMock. A truthy
mock sails through an `or` chain and then explodes when SQLite tries to bind it.
These tests use the real mock shape so the trap can't reopen.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from livekit import rtc

from agent.limits import UNKNOWN_CALLER, RateLimiter, normalize_caller_id
from agent.main import CONSOLE_CALLER_ID, _caller_id_from, _spoken_number


def _console_participant():
    """Exactly what livekit's create_mock_room() builds for console mode."""
    participant = create_autospec(rtc.RemoteParticipant, instance=True)
    participant.identity = "mock_user"
    participant.sid = "PA_mock_user"
    return participant  # .attributes is deliberately left as a truthy MagicMock


def test_console_participant_yields_a_real_string():
    caller_id = _caller_id_from(_console_participant())
    assert isinstance(caller_id, str)
    assert "MagicMock" not in caller_id


def test_truthy_mock_attributes_do_not_leak_through():
    participant = _console_participant()
    # Guard the assumption this whole test file rests on.
    assert participant.attributes  # truthy mock, not a dict
    assert not isinstance(participant.attributes, dict)

    assert _caller_id_from(participant) == "mock_user"


def test_no_participant_is_console():
    assert _caller_id_from(None) == CONSOLE_CALLER_ID


def test_sip_phone_number_is_preferred():
    participant = create_autospec(rtc.RemoteParticipant, instance=True)
    participant.identity = "sip_abc123"
    participant.attributes = {"sip.phoneNumber": "+12045550101"}
    assert _caller_id_from(participant) == "+12045550101"


def test_identity_is_the_fallback_when_attributes_are_empty():
    participant = create_autospec(rtc.RemoteParticipant, instance=True)
    participant.identity = "web_user_7"
    participant.attributes = {}
    assert _caller_id_from(participant) == "web_user_7"


def test_blank_values_fall_through_to_unknown():
    participant = create_autospec(rtc.RemoteParticipant, instance=True)
    participant.identity = "   "
    participant.attributes = {"sip.phoneNumber": ""}
    assert _caller_id_from(participant) == UNKNOWN_CALLER


@pytest.mark.parametrize(
    "value", [None, "", "   ", 12045550101, object(), create_autospec(rtc.RemoteParticipant)]
)
def test_normalize_rejects_anything_that_is_not_a_usable_string(value):
    assert normalize_caller_id(value) == UNKNOWN_CALLER


def test_normalize_trims_whitespace():
    assert normalize_caller_id("  +12045550101  ") == "+12045550101"


def test_spoken_number_groups_north_american_number():
    # +1 country code dropped, grouped 3-3-4 so TTS reads tidy groups.
    assert _spoken_number("+14313738845") == "431 373 8845"


def test_spoken_number_groups_ten_digit_number_without_country_code():
    assert _spoken_number("4313738845") == "431 373 8845"


@pytest.mark.parametrize("value", [CONSOLE_CALLER_ID, UNKNOWN_CALLER, "web_user_7", ""])
def test_spoken_number_is_none_for_non_dialable_ids(value):
    # No real number to hand the agent → no context injected, it asks instead.
    assert _spoken_number(value) is None


def test_limiter_never_binds_a_non_string_to_sqlite(tmp_path):
    """The crash: a MagicMock reaching sqlite3 raises InterfaceError."""
    mock_caller = create_autospec(rtc.RemoteParticipant, instance=True).attributes

    with RateLimiter(tmp_path / "limits.db") as limiter:
        # Every path that touches the DB must survive a junk caller_id.
        assert limiter.check(mock_caller).allowed
        limiter.start_call("call-1", mock_caller)
        limiter.end_call("call-1", 30)

        assert limiter.calls_by_caller(UNKNOWN_CALLER) == 1
        assert limiter.daily_stats().calls_today == 1

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from agent.limits import DailyStats
from agent.postcall import build_payload
from agent.tools import CallState, capture_lead, phone_fallback, take_message

STARTED = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def state() -> CallState:
    return CallState(call_id="call-1", caller_id="+12045550101", started_at=STARTED)


@pytest.fixture
def ctx(state):
    """The tools only ever touch context.userdata, so a stub is enough."""
    return types.SimpleNamespace(userdata=state)


async def _answer_everything(ctx) -> None:
    await capture_lead(
        ctx,
        industry="Sunrise Bakery, food service",
        biggest_time_sink="taking phone orders",
        current_process="pen and paper by the register",
        frequency="daily",
        tools_used="Square and Excel",
        desired_outcome="orders land in a spreadsheet automatically",
        contact_name="Dana Reyes",
        contact_phone_or_email="204-555-0101",
    )


async def test_capture_lead_accumulates_across_calls(ctx, state):
    await capture_lead(ctx, industry="Sunrise Bakery, food service")
    await capture_lead(ctx, biggest_time_sink="taking phone orders")

    assert state.lead.industry == "Sunrise Bakery, food service"
    assert state.lead.biggest_time_sink == "taking phone orders"
    assert not state.lead.is_complete()


async def test_capture_lead_reports_what_is_still_missing(ctx):
    result = await capture_lead(ctx, industry="bakery")
    assert "contact_phone_or_email" in result
    assert "biggest_time_sink" in result  # the other essential, still missing
    # The dropped optional fields must NOT be nudged for, or the agent re-asks them.
    assert "frequency" not in result
    assert "tools_used" not in result


async def test_lead_is_incomplete_without_contact(ctx, state):
    await capture_lead(
        ctx,
        industry="bakery",
        biggest_time_sink="phone orders",
        current_process="paper",
        frequency="daily",
        tools_used="Square",
        desired_outcome="automatic orders",
        contact_name="Dana",
    )
    # Six answers and a name, but no way to reach them — not a usable lead.
    assert not state.lead.has_contact()
    assert not state.lead.is_complete()


async def test_lead_is_complete_with_all_seven_answers(ctx, state):
    await _answer_everything(ctx)
    assert state.lead.is_complete()
    assert state.lead.has_contact()


async def test_lean_lead_is_complete_with_just_the_essentials(ctx, state):
    """The lean script only guarantees business + need + contact — that's complete.

    The dropped fields (current_process, frequency, tools_used, desired_outcome) being
    empty must NOT make the lead incomplete, or the Airtable 'Completed' flag would
    never tick for a normal lean call.
    """
    await capture_lead(
        ctx,
        industry="Sunrise Bakery, food service",
        biggest_time_sink="taking phone orders",
        contact_name="Dana Reyes",
        contact_phone_or_email="204-555-0101",
    )
    assert state.lead.is_complete()
    assert not state.lead.frequency  # never asked, still complete
    assert not state.lead.tools_used


@pytest.mark.parametrize(
    "caller_id,expected",
    [
        ("+12045550101", "+12045550101"),
        ("2045550101", "2045550101"),
        ("console", None),
        ("unknown", None),
        ("anonymous", None),
        ("web_user_7", None),  # a web identity, not a dialable number
        ("", None),
    ],
)
def test_phone_fallback_only_accepts_dialable_numbers(caller_id, expected):
    assert phone_fallback(caller_id) == expected


async def test_effective_contact_prefers_the_dictated_contact(ctx, state):
    await capture_lead(ctx, contact_phone_or_email="dana@sunrisebakery.ca")
    # An explicit contact the caller gave wins over the caller ID.
    assert state.effective_contact() == "dana@sunrisebakery.ca"


def test_effective_contact_falls_back_to_the_caller_id(state):
    # Caller never dictated a contact, but they're on the phone — we can call them back.
    assert not state.lead.has_contact()
    assert state.effective_contact() == "+12045550101"


def test_effective_contact_is_none_for_a_placeholder_caller_id():
    state = CallState(call_id="c", caller_id="console", started_at=STARTED)
    assert state.effective_contact() is None


async def test_lead_is_complete_counts_the_caller_id_as_contact(ctx, state):
    # Business + need captured, no dictated contact — still complete, we have their number.
    await capture_lead(ctx, industry="bakery", biggest_time_sink="phone orders")
    assert not state.lead.is_complete()  # Lead alone doesn't know the caller_id
    assert state.lead_is_complete()  # CallState credits the number they called from


async def test_build_payload_backfills_contact_from_caller_id(ctx, state):
    await capture_lead(ctx, industry="bakery", biggest_time_sink="phone orders")
    payload = build_payload(
        state, DailyStats(1, 1.0, 1), completed=True, end_reason="agent_goodbye"
    )
    assert payload["lead"]["contact"]["phone_or_email"] == "+12045550101"


async def test_capture_lead_overwrites_on_correction(ctx, state):
    await _answer_everything(ctx)
    # Caller corrects their email during readback.
    await capture_lead(ctx, contact_phone_or_email="dana@sunrisebakery.ca")
    assert state.lead.contact_phone_or_email == "dana@sunrisebakery.ca"
    assert state.lead.industry == "Sunrise Bakery, food service"  # untouched


async def test_capture_lead_ignores_empty_and_null_like_values(ctx, state):
    await capture_lead(ctx, industry="bakery")
    await capture_lead(ctx, industry="   ", biggest_time_sink="null")

    assert state.lead.industry == "bakery"  # not clobbered by whitespace
    assert state.lead.biggest_time_sink is None  # literal "null" is not an answer


async def test_take_message_stores_questions_verbatim(ctx, state):
    await take_message(ctx, message="How much does something like this cost?")
    await take_message(ctx, message="Do you work with restaurants?")

    assert state.lead.messages == [
        "How much does something like this cost?",
        "Do you work with restaurants?",
    ]


async def test_take_message_ignores_blank_input(ctx, state):
    await take_message(ctx, message="   ")
    assert state.lead.messages == []


async def test_lead_to_dict_has_the_agreed_shape(ctx, state):
    await _answer_everything(ctx)
    assert state.lead.to_dict() == {
        "industry": "Sunrise Bakery, food service",
        "biggest_time_sink": "taking phone orders",
        "current_process": "pen and paper by the register",
        "frequency": "daily",
        "tools_used": "Square and Excel",
        "desired_outcome": "orders land in a spreadsheet automatically",
        "contact": {"name": "Dana Reyes", "phone_or_email": "204-555-0101"},
    }


async def test_webhook_payload_shape(ctx, state):
    await _answer_everything(ctx)
    await take_message(ctx, message="Do you work with restaurants?")
    state.add_transcript("assistant", "What kind of business are you in?")
    state.add_transcript("user", "I run a bakery.")
    state.ended_at = STARTED + timedelta(seconds=142)

    payload = build_payload(
        state,
        DailyStats(calls_today=4, minutes_today=12.5, leads_captured_today=3),
        completed=True,
        end_reason="caller_hangup",
    )

    assert payload["call_id"] == "call-1"
    assert payload["started_at"] == STARTED.isoformat()
    assert payload["duration_seconds"] == 142.0
    assert payload["completed"] is True
    assert payload["lead"]["contact"]["phone_or_email"] == "204-555-0101"
    assert payload["messages"] == ["Do you work with restaurants?"]
    assert [t["role"] for t in payload["transcript"]] == ["assistant", "user"]
    assert payload["daily_stats"] == {
        "calls_today": 4,
        "minutes_today": 12.5,
        "leads_captured_today": 3,
    }


def test_transcript_skips_empty_turns(state):
    state.add_transcript("user", "  ")
    state.add_transcript("user", "I run a bakery.")
    assert len(state.transcript) == 1

"""Call state and the two tools the agent can call: capture_lead and take_message."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from livekit.agents import RunContext, function_tool, get_job_context

logger = logging.getLogger("togo.tools")

# The seven intake answers, in script order. Contact is stored separately because
# it is the one required field — a lead without it is logged as incomplete.
LEAD_FIELDS = (
    "industry",
    "biggest_time_sink",
    "current_process",
    "frequency",
    "tools_used",
    "desired_outcome",
)

# The lean intake only actively asks three things: business, what they want to
# automate, and contact. So "complete" means those essentials are present — the
# other fields (current_process, frequency, tools_used, desired_outcome) are captured
# only if the caller volunteers them, and their absence does NOT make a lead incomplete.
# Contact is checked separately via has_contact().
REQUIRED_LEAD_FIELDS = (
    "industry",
    "biggest_time_sink",
)


@dataclass
class Lead:
    """The structured object accumulated across the call."""

    industry: str | None = None
    biggest_time_sink: str | None = None
    current_process: str | None = None
    frequency: str | None = None
    tools_used: str | None = None
    desired_outcome: str | None = None
    contact_name: str | None = None
    contact_phone_or_email: str | None = None
    messages: list[str] = field(default_factory=list)

    def has_contact(self) -> bool:
        return bool(self.contact_phone_or_email)

    def is_complete(self) -> bool:
        """Complete = contact info + the lean essentials (business + what to automate).

        Contact is the one hard-required field (CLAUDE.md v1 req 2). Beyond that, the
        lean script only guarantees business + need, so those define completeness; the
        remaining fields are bonus context, not required.
        """
        return self.has_contact() and all(
            getattr(self, name) for name in REQUIRED_LEAD_FIELDS
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **{name: getattr(self, name) for name in LEAD_FIELDS},
            "contact": {
                "name": self.contact_name,
                "phone_or_email": self.contact_phone_or_email,
            },
        }


@dataclass
class CallState:
    """Everything one call accumulates. Reachable from tools via RunContext.userdata."""

    call_id: str
    caller_id: str
    started_at: datetime
    lead: Lead = field(default_factory=Lead)
    transcript: list[dict[str, str]] = field(default_factory=list)
    ended_at: datetime | None = None
    # Overwritten by end_call / the time-limit path. Stays "caller_hangup" if the
    # caller simply drops, which is the only way a call ends without us ending it.
    end_reason: str = "caller_hangup"
    # True while the caller is mid-sentence and we are deliberately holding the line.
    # Read by the dead-air filler, which must stay quiet during a thinking pause.
    awaiting_continuation: bool = False

    def effective_contact(self) -> str | None:
        """The captured contact, or the caller's own number as a fallback.

        The agent should confirm and record the caller's number explicitly, but if it
        never does (it flubbed the question, the caller was terse), we still have the
        number they're calling from — a phone lead is never contactless when we can just
        call them back. An explicit contact the caller dictated always wins over this.
        """
        return self.lead.contact_phone_or_email or phone_fallback(self.caller_id)

    def lead_is_complete(self) -> bool:
        """Like Lead.is_complete(), but the caller's own number counts as contact.

        Lead alone can't know the caller_id, so completeness that credits the fallback
        number lives here on CallState. Used for the payload's `completed` flag and the
        daily leads-captured counter.
        """
        return bool(self.effective_contact()) and all(
            getattr(self.lead, name) for name in REQUIRED_LEAD_FIELDS
        )

    def duration_seconds(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    def add_transcript(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.transcript.append(
            {
                "role": role,
                "text": text,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )


def _clean(value: str | None) -> str | None:
    """Treat whitespace and the model's occasional literal "null" as "not answered"."""
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return value


# Caller IDs that are not something a human can dial back. Console/simulation use
# placeholders, and a withheld number arrives as one of these.
_NON_DIALABLE_CALLER_IDS = {"console", "unknown", "anonymous", "restricted", ""}


def phone_fallback(caller_id: str | None) -> str | None:
    """The caller's own number, usable as a contact — or None if it isn't dialable.

    A phone caller is always reachable at the number they're calling from, so a real
    caller_id is a valid fallback when they don't dictate a separate contact. Placeholder
    IDs (console/unknown/withheld) are not, and must not masquerade as a captured lead.
    """
    if not caller_id:
        return None
    cid = caller_id.strip()
    if cid.lower() in _NON_DIALABLE_CALLER_IDS:
        return None
    # Needs enough digits to actually be a phone number, not e.g. a web SDK identity.
    if sum(c.isdigit() for c in cid) < 7:
        return None
    return cid


@function_tool
async def capture_lead(
    context: RunContext[CallState],
    industry: str | None = None,
    biggest_time_sink: str | None = None,
    current_process: str | None = None,
    frequency: str | None = None,
    tools_used: str | None = None,
    desired_outcome: str | None = None,
    contact_name: str | None = None,
    contact_phone_or_email: str | None = None,
) -> str:
    """Record the caller's answers to the intake questions.

    Call this as you collect answers — you may call it repeatedly with whatever new
    information you have. Pass only the fields the caller has actually answered;
    leave the rest out. Calling it again with a field already set overwrites it,
    which is how you apply a correction during readback.

    Args:
        industry: The caller's company and industry (asked). Essential.
        biggest_time_sink: The task/headache they want to automate or hand off
            (asked). Essential — this is what they called about.
        desired_outcome: The result they'd want if it were automated — record it if
            they describe one while answering, don't ask separately.
        current_process: How they handle the task today — record only if volunteered.
        frequency: How often it comes up — record only if volunteered.
        tools_used: Software/tools they currently use — record only if volunteered.
        contact_name: The caller's name (asked).
        contact_phone_or_email: Their callback number or email (asked). Required.
    """
    lead = context.userdata.lead
    updates = {
        "industry": _clean(industry),
        "biggest_time_sink": _clean(biggest_time_sink),
        "current_process": _clean(current_process),
        "frequency": _clean(frequency),
        "tools_used": _clean(tools_used),
        "desired_outcome": _clean(desired_outcome),
        "contact_name": _clean(contact_name),
        "contact_phone_or_email": _clean(contact_phone_or_email),
    }
    applied = [name for name, value in updates.items() if value is not None]
    for name in applied:
        setattr(lead, name, updates[name])

    logger.info(
        "capture_lead updated %s (complete=%s)", applied or "nothing", lead.is_complete()
    )

    # Only nudge for the lean essentials — never for the optional context fields, or
    # the agent would start asking questions the lean script deliberately dropped.
    missing = [name for name in REQUIRED_LEAD_FIELDS if not getattr(lead, name)]
    if not lead.has_contact():
        missing.append("contact_phone_or_email")
    if missing:
        return f"Recorded. Still need: {', '.join(missing)}."
    return "Recorded. You have the essentials — read them back to confirm, then wrap up."


@function_tool
async def take_message(context: RunContext[CallState], message: str) -> str:
    """Record an off-script question or statement verbatim for a human to follow up on.

    Use this whenever the caller asks something you are not allowed to answer —
    pricing, services, availability, who works at the company — or says anything
    that isn't an answer to one of the intake questions.

    Args:
        message: The caller's question or statement, recorded word for word.
    """
    text = message.strip()
    if not text:
        return "Nothing to record."
    context.userdata.lead.messages.append(text)
    logger.info("take_message recorded message #%d", len(context.userdata.lead.messages))
    return "Noted — a human will follow up on that."


@function_tool
async def end_call(context: RunContext[CallState]) -> str:
    """Hang up the phone. The call stays open until you call this.

    Call this ONLY after you have already spoken your closing line — the goodbye is
    allowed to finish playing before the line drops, so say goodbye first, then call
    this in the same turn. Do not call it mid-conversation.

    Say NOTHING when you call this. Your closing line is the last thing the caller
    hears. Do not announce the hangup ("Now I'll end the call") — just call the tool.

    Call it after: the readback is confirmed and you've closed; the caller declines to
    give contact info and you've thanked them; the caller is abusive or a prank; or
    audio is unintelligible and you've pointed them at the website.
    """
    state = context.userdata
    state.end_reason = "agent_goodbye"
    logger.info(
        "end_call: hanging up (lead_complete=%s, messages=%d)",
        state.lead.is_complete(),
        len(state.lead.messages),
    )

    # Let the goodbye finish playing. Cutting it off here is exactly the abrupt hangup
    # the readback rule exists to prevent.
    await context.wait_for_playout()

    job_ctx = get_job_context(required=False)
    if job_ctx is None:
        # Text simulation — there is no room and no job to tear down.
        return "Call ended."

    try:
        # Real SIP call: this disconnects the caller. In console mode it no-ops.
        await job_ctx.delete_room()
    except Exception as exc:  # noqa: BLE001 — a failed hangup must not raise at the caller
        logger.warning("delete_room failed during end_call: %s", exc)

    # Ends the job (and so the console session) and fires the post-call webhook.
    job_ctx.shutdown(reason="agent ended the call")
    return "Call ended."

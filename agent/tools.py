"""Call state and the two tools the agent can call: capture_lead and take_message."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from livekit.agents import RunContext, function_tool

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
        """A lead counts as complete only with contact info — see CLAUDE.md v1 req 2."""
        return self.has_contact() and all(
            getattr(self, name) for name in LEAD_FIELDS
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
        industry: The caller's company and industry (question 1).
        biggest_time_sink: The task that eats the most time (question 2).
        current_process: How they handle that task today (question 3).
        frequency: How often it comes up, e.g. daily or weekly (question 4).
        tools_used: Software or tools they currently use (question 5).
        desired_outcome: The result they'd want if it were automated (question 6).
        contact_name: The caller's name (question 7).
        contact_phone_or_email: Their callback number or email (question 7). Required.
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

    missing = [name for name in LEAD_FIELDS if not getattr(lead, name)]
    if not lead.has_contact():
        missing.append("contact_phone_or_email")
    if missing:
        return f"Recorded. Still missing: {', '.join(missing)}."
    return "Recorded. All answers captured — read them back to confirm."


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

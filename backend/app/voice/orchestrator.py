"""The conversation state machine.

Deliberately free of FastAPI and Twilio SDK imports beyond TwiML rendering, so
every transition can be unit tested by calling handle_turn directly.
See CALL_FLOW.md for the state diagram.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    Category,
    EscalationReason,
    Priority,
    Ticket,
    TicketSource,
    VoiceCallSession,
    VoiceCallState,
)
from app.schemas.ticket import TicketCreate
from app.services import ticket_service
from app.voice import nlu, scripts, twiml
from app.voice.session import caller_id_is_usable, record_turn, transcript_text, update_collected

logger = logging.getLogger("hfmg.voice.orchestrator")

settings = get_settings()

PRIORITY_WORD = {
    Priority.URGENT: "critical",
    Priority.HIGH: "high",
    Priority.MEDIUM: "medium",
    Priority.LOW: "low",
}


class TurnOutcome:
    """What the caller hears next, plus any ticket work the route must finish."""

    def __init__(self, twiml_body: str, ticket_id: uuid.UUID | None = None):
        self.twiml = twiml_body
        self.ticket_id = ticket_id


async def start_call(session: VoiceCallSession) -> TurnOutcome:
    """Turn 0: greet and wait for the caller to describe their problem."""
    session.state = VoiceCallState.COLLECT_DESCRIPTION
    record_turn(session, role="agent", text=scripts.GREETING)
    return TurnOutcome(twiml.ask(scripts.GREETING))


async def handle_turn(
    db: AsyncSession,
    session: VoiceCallSession,
    *,
    utterance: str,
    confidence: float | None = None,
) -> TurnOutcome:
    """Interpret one caller utterance and advance the conversation."""
    utterance = (utterance or "").strip()
    record_turn(session, role="caller", text=utterance, confidence=confidence)

    if session.state in (VoiceCallState.ESCALATED, VoiceCallState.COMPLETED):
        # Replay of an already-terminal turn (Twilio retry). Say goodbye again
        # rather than re-running any of the work.
        return TurnOutcome(_speak(session, twiml.say_and_hangup(scripts.GOODBYE)))

    if not utterance:
        return await _handle_failure(db, session)

    handlers = {
        VoiceCallState.COLLECT_DESCRIPTION: _handle_description,
        VoiceCallState.COLLECT_NAME: _handle_name,
        VoiceCallState.COLLECT_PHONE: _handle_phone,
        VoiceCallState.COLLECT_EMAIL: _handle_email,
        VoiceCallState.CONFIRM_EMAIL: _handle_email_confirmation,
        VoiceCallState.CONFIRM_CATEGORY: _handle_category_confirmation,
        VoiceCallState.ANYTHING_ELSE: _handle_anything_else,
    }
    handler = handlers.get(session.state)
    if handler is None:
        logger.error("No handler for state %s on call %s", session.state, session.twilio_call_sid)
        return await escalate(db, session, EscalationReason.SYSTEM_ERROR)

    return await handler(db, session, utterance)


# --- state handlers -------------------------------------------------------


async def _handle_description(db: AsyncSession, session: VoiceCallSession, utterance: str) -> TurnOutcome:
    result = await nlu.interpret_description(utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)
    if result.failed:
        return await _handle_failure(db, session)

    update_collected(
        session,
        description=result.value,
        category=result.category,
        category_confidence=result.confidence,
        priority=result.priority.value if result.priority else Priority.MEDIUM.value,
        impact=result.impact,
        short_issue=result.extras.get("short_issue"),
    )

    issue = result.extras.get("short_issue") or result.category
    prompt = scripts.NAME_ASK.format(issue=issue) if issue else scripts.NAME_ASK_NO_ISSUE
    session.state = VoiceCallState.COLLECT_NAME
    return TurnOutcome(_speak(session, twiml.ask(prompt)))


async def _handle_name(db: AsyncSession, session: VoiceCallSession, utterance: str) -> TurnOutcome:
    result = await nlu.interpret_name(utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)
    if result.failed:
        return await _handle_failure(db, session)

    update_collected(session, caller_name=result.value)
    return await _advance_after_name(db, session)


async def _advance_after_name(db: AsyncSession, session: VoiceCallSession) -> TurnOutcome:
    """Skip the phone question entirely when caller ID gave us a number."""
    if not session.collected.get("phone_number"):
        session.state = VoiceCallState.COLLECT_PHONE
        return TurnOutcome(_speak(session, twiml.ask(scripts.PHONE_ASK)))
    return _ask_for_email(session)


async def _handle_phone(db: AsyncSession, session: VoiceCallSession, utterance: str) -> TurnOutcome:
    result = await nlu.interpret_phone(utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)
    if result.failed:
        return await _handle_failure(db, session)

    update_collected(session, phone_number=result.value)
    return _ask_for_email(session)


def _ask_for_email(session: VoiceCallSession) -> TurnOutcome:
    first_name = (session.collected.get("caller_name") or "").split(" ")[0]
    prompt = (
        scripts.EMAIL_ASK.format(first_name=first_name) if first_name else scripts.EMAIL_ASK_NO_NAME
    )
    session.state = VoiceCallState.COLLECT_EMAIL
    return TurnOutcome(_speak(session, twiml.ask(prompt)))


async def _handle_email(db: AsyncSession, session: VoiceCallSession, utterance: str) -> TurnOutcome:
    result = await nlu.interpret_email(utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)

    if result.extras.get("declined"):
        return await _finish_collection(db, session)

    session.email_attempt_count += 1

    if result.failed:
        # Email is optional, so a failure here never counts toward escalation.
        if session.email_attempt_count >= settings.voice_max_email_attempts:
            return await _finish_collection(db, session, preamble=scripts.EMAIL_GIVE_UP)
        return TurnOutcome(_speak(session, twiml.ask(scripts.EMAIL_RETRY)))

    update_collected(session, email=result.value)
    session.state = VoiceCallState.CONFIRM_EMAIL
    prompt = scripts.EMAIL_CONFIRM.format(email=scripts.spoken_email(result.value))
    return TurnOutcome(_speak(session, twiml.ask(prompt)))


async def _handle_email_confirmation(
    db: AsyncSession, session: VoiceCallSession, utterance: str
) -> TurnOutcome:
    result = await nlu.interpret_yes_no("Is that email correct?", utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)

    if result.yes_no is True:
        return await _finish_collection(db, session)

    # Wrong or unclear -- drop it and either retry or move on.
    update_collected(session, email=None)
    if session.email_attempt_count >= settings.voice_max_email_attempts:
        return await _finish_collection(db, session, preamble=scripts.EMAIL_GIVE_UP)
    session.state = VoiceCallState.COLLECT_EMAIL
    return TurnOutcome(_speak(session, twiml.ask(scripts.EMAIL_RETRY)))


async def _handle_category_confirmation(
    db: AsyncSession, session: VoiceCallSession, utterance: str
) -> TurnOutcome:
    result = await nlu.interpret_yes_no("Is this about that category?", utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)

    if result.yes_no is False:
        # Don't play twenty questions -- a human reroutes faster.
        update_collected(session, category="Other")

    return await _create_ticket_and_read_back(db, session)


async def _handle_anything_else(
    db: AsyncSession, session: VoiceCallSession, utterance: str
) -> TurnOutcome:
    result = await nlu.interpret_yes_no("Is there anything else I can help you with?", utterance)

    if _wants_human(utterance, result):
        return await escalate(db, session, EscalationReason.CALLER_REQUESTED)

    if result.yes_no is True:
        # Second ticket on the same call: clear slots but keep name and phone.
        session.collected = {
            "caller_name": session.collected.get("caller_name"),
            "phone_number": session.collected.get("phone_number"),
            "email": session.collected.get("email"),
        }
        session.email_attempt_count = 0
        session.state = VoiceCallState.COLLECT_DESCRIPTION
        return TurnOutcome(_speak(session, twiml.ask(scripts.DESCRIPTION_ASK)))

    session.state = VoiceCallState.COMPLETED
    return TurnOutcome(_speak(session, twiml.say_and_hangup(scripts.GOODBYE)))


async def _finish_collection(
    db: AsyncSession, session: VoiceCallSession, *, preamble: str | None = None
) -> TurnOutcome:
    """All slots gathered: confirm an uncertain category, else create the ticket."""
    if session.collected.get("category_confidence") in ("medium", "low"):
        session.state = VoiceCallState.CONFIRM_CATEGORY
        prompt = scripts.CATEGORY_CONFIRM.format(category=session.collected.get("category"))
        body = twiml.say_then_ask(preamble, prompt) if preamble else twiml.ask(prompt)
        return TurnOutcome(_speak(session, body))

    return await _create_ticket_and_read_back(db, session, preamble=preamble)


# --- ticket creation ------------------------------------------------------


async def _create_ticket_and_read_back(
    db: AsyncSession, session: VoiceCallSession, *, preamble: str | None = None
) -> TurnOutcome:
    if session.ticket_id is not None:
        # Twilio replayed the turn; don't create a second ticket.
        ticket = await db.get(Ticket, session.ticket_id)
        read_back = scripts.READ_BACK.format(
            ticket_number=scripts.spoken_ticket_number(ticket.ticket_number)
        )
        return TurnOutcome(
            _speak(session, twiml.say_then_ask(read_back, scripts.ANYTHING_ELSE)), session.ticket_id
        )

    ticket = await _create_ticket(db, session)
    session.ticket_id = ticket.id
    session.state = VoiceCallState.ANYTHING_ELSE

    lines = [preamble] if preamble else []
    priority = session.collected.get("priority")
    impact = session.collected.get("impact")
    if priority in (Priority.URGENT.value, Priority.HIGH.value) and impact:
        lines.append(
            scripts.PRIORITY_NOTICE.format(impact=impact, priority=PRIORITY_WORD[Priority(priority)])
        )
    lines.append(scripts.CREATING_TICKET)
    lines.append(
        scripts.READ_BACK.format(ticket_number=scripts.spoken_ticket_number(ticket.ticket_number))
    )

    return TurnOutcome(
        _speak(session, twiml.say_then_ask(" ".join(lines), scripts.ANYTHING_ELSE)), ticket.id
    )


async def _create_ticket(
    db: AsyncSession, session: VoiceCallSession, *, escalation_note: str | None = None
) -> Ticket:
    """Build a ticket from collected slots via the unchanged Phase 1 service."""
    collected = session.collected

    description = collected.get("description") or ""
    if escalation_note:
        description = f"{escalation_note}\n\n{description}".strip()
    description = f"{description}\n\n--- Call transcript ---\n{transcript_text(session)}".strip()

    priority = Priority(collected.get("priority") or Priority.MEDIUM.value)
    if escalation_note:
        # Automation couldn't serve this caller; don't leave them in a normal queue.
        priority = Priority.URGENT if priority == Priority.URGENT else Priority.HIGH

    payload = TicketCreate(
        caller_name=collected.get("caller_name") or "Unknown caller (voice)",
        phone_number=collected.get("phone_number") or session.from_number or "unknown",
        email=collected.get("email"),
        category_id=await _category_id(db, collected.get("category")),
        priority=priority,
        description=description[:10_000],
    )

    ticket = await ticket_service.create_ticket(db, payload)

    # Set the intake channel without touching TicketService, which stays as
    # Phase 1 shipped it.
    ticket.source = TicketSource.PHONE
    await db.commit()
    return ticket


async def salvage_abandoned_call(db: AsyncSession, session: VoiceCallSession) -> Ticket:
    """Create a ticket from a call that dropped after the problem was described."""
    ticket = await _create_ticket(
        db,
        session,
        escalation_note="INCOMPLETE VOICE INTAKE - caller disconnected before intake finished.",
    )
    session.ticket_id = ticket.id
    return ticket


async def _category_id(db: AsyncSession, name: str | None) -> uuid.UUID:
    stmt = select(Category).where(Category.name == (name or "Other"))
    category = (await db.execute(stmt)).scalar_one_or_none()
    if category is None:
        category = (await db.execute(select(Category).where(Category.name == "Other"))).scalar_one()
    return category.id


# --- failure and escalation ----------------------------------------------


async def _handle_failure(db: AsyncSession, session: VoiceCallSession) -> TurnOutcome:
    """Silence or an uninterpretable answer to a required question."""
    session.misunderstanding_count += 1

    if session.misunderstanding_count >= settings.voice_max_misunderstandings:
        return await escalate(db, session, EscalationReason.REPEATED_MISUNDERSTANDING)

    retry_index = min(session.misunderstanding_count - 1, 1)
    retries = {
        VoiceCallState.COLLECT_DESCRIPTION: scripts.DESCRIPTION_RETRY,
        VoiceCallState.COLLECT_NAME: scripts.NAME_RETRY,
        VoiceCallState.COLLECT_PHONE: scripts.PHONE_RETRY,
    }
    prompt = retries.get(session.state, scripts.DESCRIPTION_RETRY)[retry_index]
    return TurnOutcome(_speak(session, twiml.ask(prompt)))


async def escalate(
    db: AsyncSession, session: VoiceCallSession, reason: EscalationReason
) -> TurnOutcome:
    """Create a callback request and hand off. The caller always gets a number."""
    session.escalated = True
    session.escalation_reason = reason
    session.state = VoiceCallState.ESCALATED

    notes = {
        EscalationReason.CALLER_REQUESTED: "CALLBACK REQUESTED - caller asked to speak with a person.",
        EscalationReason.REPEATED_MISUNDERSTANDING: (
            "CALLBACK REQUESTED - automated intake could not understand the caller after 3 attempts."
        ),
        EscalationReason.SYSTEM_ERROR: (
            "CALLBACK REQUESTED - automated intake hit a system error before completing."
        ),
    }

    if session.ticket_id is not None:
        ticket = await db.get(Ticket, session.ticket_id)
    else:
        ticket = await _create_ticket(db, session, escalation_note=notes[reason])
        session.ticket_id = ticket.id

    phone = session.collected.get("phone_number") or (
        session.from_number if caller_id_is_usable(session.from_number) else None
    )

    if phone:
        opener = (
            scripts.ESCALATION_CALLER_REQUESTED
            if reason == EscalationReason.CALLER_REQUESTED
            else scripts.ESCALATION_MISUNDERSTOOD
        ).format(phone=scripts.spoken_phone_number(phone))
    else:
        opener = scripts.ESCALATION_NO_CALLBACK_NUMBER

    read_back = scripts.ESCALATION_READ_BACK.format(
        ticket_number=scripts.spoken_ticket_number(ticket.ticket_number)
    )
    return TurnOutcome(
        _speak(session, twiml.say_and_hangup(opener, read_back, scripts.GOODBYE)), ticket.id
    )


def _wants_human(utterance: str, result: nlu.TurnResult) -> bool:
    """Decide whether the caller is asking for a person.

    The model's semantic judgement is authoritative -- keyword matching alone
    would misfire on descriptions like "the person at the front desk can't
    print". The keyword list is only a fallback for when the NLU call itself
    failed, so an explicit request still works while the model is unreachable.
    """
    if result.escalation_requested:
        return True
    return result.failed and nlu.mentions_escalation(utterance)


def _speak(session: VoiceCallSession, twiml_body: str) -> str:
    """Record what the agent said, for the transcript on the ticket."""
    record_turn(session, role="agent", text=_extract_spoken_text(twiml_body))
    return twiml_body


def _extract_spoken_text(twiml_body: str) -> str:
    return " ".join(twiml.spoken_lines(twiml_body)).strip()

"""Call-ending logic shared by every voice transport (Twilio webhooks, the SIP gateway).

Split out of app/voice/routes.py so a new transport doesn't have to duplicate
the ticket-dispatch and abandoned-call-salvage rules -- just call these with
its own session/background_tasks.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.summarizer import generate_summary_for_ticket
from app.db.models import VoiceCallSession, VoiceCallState
from app.notifications.email import send_ticket_notification
from app.services import ticket_service
from app.voice import orchestrator

logger = logging.getLogger("hfmg.voice.call_lifecycle")

_TERMINAL_STATES = (VoiceCallState.COMPLETED, VoiceCallState.ESCALATED, VoiceCallState.ABANDONED)


async def dispatch_ticket_tasks(db: AsyncSession, background_tasks: BackgroundTasks, ticket_id: UUID) -> None:
    """Fire the same background work the web form fires.

    The AI summary is deliberately not awaited -- the caller hears their ticket
    number immediately (or the SIP gateway moves on to its next prompt) and the
    summary lands seconds later.
    """
    ticket = await ticket_service.get_ticket(db, ticket_id)
    background_tasks.add_task(send_ticket_notification, ticket, "created")
    background_tasks.add_task(generate_summary_for_ticket, ticket.id)


async def finalize_call(
    db: AsyncSession,
    session: VoiceCallSession,
    background_tasks: BackgroundTasks,
    *,
    ended_reason: str,
) -> None:
    """Close out the session when a call ends, salvaging useful abandonments.

    `ended_reason` is free text for the log line only (Twilio's CallStatus,
    or whatever the SIP gateway reports for why the call ended).
    """
    session.ended_at = datetime.now(timezone.utc)

    if session.state in _TERMINAL_STATES or session.ticket_id is not None:
        await db.commit()
        return

    session.state = VoiceCallState.ABANDONED

    # Safety net: the caller had a real problem and a reachable number, so
    # don't lose it just because the call dropped.
    has_description = bool(session.collected.get("description"))
    has_phone = bool(session.collected.get("phone_number") or session.from_number)
    if has_description and has_phone:
        ticket = await orchestrator.salvage_abandoned_call(db, session)
        await db.commit()
        await dispatch_ticket_tasks(db, background_tasks, ticket.id)
        logger.info(
            "Salvaged abandoned call %s into ticket %s", session.twilio_call_sid, ticket.ticket_number
        )
    else:
        await db.commit()
        logger.info("Call %s abandoned (%s) with nothing to salvage", session.twilio_call_sid, ended_reason)

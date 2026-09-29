"""Twilio voice webhooks.

Thin by design: validate the signature, delegate to the orchestrator, return
TwiML. All conversation logic lives in orchestrator.py.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import EscalationReason
from app.voice import call_lifecycle, orchestrator, scripts, twiml
from app.voice.security import verify_twilio_signature
from app.voice.session import get_or_create_session, get_session

logger = logging.getLogger("hfmg.voice.routes")

router = APIRouter(prefix="/webhooks/twilio", tags=["twilio-voice"])

TWIML_MEDIA_TYPE = "application/xml"


def _twiml(body: str) -> Response:
    return Response(content=body, media_type=TWIML_MEDIA_TYPE)


@router.post("/voice", dependencies=[Depends(verify_twilio_signature)])
async def incoming_call(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    session = await get_or_create_session(
        db,
        call_sid=call_sid,
        from_number=str(form.get("From", "")),
        to_number=str(form.get("To", "")),
    )

    try:
        outcome = await orchestrator.start_call(session)
        await db.commit()
    except Exception:
        logger.exception("Failed to start call %s", call_sid)
        return _twiml(twiml.say_and_hangup(scripts.SYSTEM_ERROR))

    return _twiml(outcome.twiml)


@router.post("/voice/gather", dependencies=[Depends(verify_twilio_signature)])
async def gather(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    speech = str(form.get("SpeechResult", ""))
    raw_confidence = form.get("Confidence")
    confidence = float(raw_confidence) if raw_confidence not in (None, "") else None

    session = await get_session(db, call_sid)
    if session is None:
        logger.warning("Gather for unknown call %s", call_sid)
        return _twiml(twiml.say_and_hangup(scripts.SYSTEM_ERROR))

    try:
        outcome = await orchestrator.handle_turn(
            db, session, utterance=speech, confidence=confidence
        )
        await db.commit()
    except Exception:
        logger.exception("Turn failed for call %s; escalating", call_sid)
        try:
            outcome = await orchestrator.escalate(db, session, EscalationReason.SYSTEM_ERROR)
            await db.commit()
        except Exception:
            logger.exception("Escalation also failed for call %s", call_sid)
            return _twiml(twiml.say_and_hangup(scripts.SYSTEM_ERROR))

    if outcome.ticket_id is not None:
        await call_lifecycle.dispatch_ticket_tasks(db, background_tasks, outcome.ticket_id)

    logger.info(
        "call=%s state=%s misunderstandings=%s stt_confidence=%s",
        call_sid,
        session.state.value,
        session.misunderstanding_count,
        confidence,
    )
    return _twiml(outcome.twiml)


@router.post("/voice/status", dependencies=[Depends(verify_twilio_signature)])
async def call_status(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Finalize the session when the call ends, salvaging useful abandonments."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    twilio_status = str(form.get("CallStatus", ""))

    session = await get_session(db, call_sid)
    if session is None:
        return Response(status_code=204)

    await call_lifecycle.finalize_call(db, session, background_tasks, ended_reason=twilio_status)
    return Response(status_code=204)


@router.post("/voice/fallback", dependencies=[Depends(verify_twilio_signature)])
async def fallback(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Twilio calls this when the primary webhook errors or times out."""
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    logger.error("Twilio fallback invoked for call %s", call_sid)

    session = await get_session(db, call_sid)
    if session is not None and session.ticket_id is None:
        try:
            outcome = await orchestrator.escalate(db, session, EscalationReason.SYSTEM_ERROR)
            await db.commit()
            return _twiml(outcome.twiml)
        except Exception:
            logger.exception("Fallback escalation failed for call %s", call_sid)

    return _twiml(twiml.say_and_hangup(scripts.SYSTEM_ERROR))

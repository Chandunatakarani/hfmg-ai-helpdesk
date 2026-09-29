"""SIP gateway endpoints: a second front end for the same voice orchestrator.

The gateway (a SIPSorcery service on another server, registered as the
Nextiva "no answer" overflow extension) handles SIP signaling, RTP audio,
and speech-to-text/text-to-speech itself. It calls in here with plain caller
utterances and gets back plain reply text -- everything about the
conversation (state machine, NLU, ticket creation) is the same code path
Twilio's webhooks use; see orchestrator.py and CALL_FLOW.md.

Unlike the Twilio routes, there's no per-request signature to validate here
(no equivalent of Twilio's request-signing scheme), so every endpoint is
gated by a shared bearer token instead -- see verify_sip_gateway_token.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import EscalationReason, VoiceCallChannel
from app.voice import call_lifecycle, orchestrator, scripts, twiml
from app.voice.security import verify_sip_gateway_token
from app.voice.session import get_or_create_session, get_session

logger = logging.getLogger("hfmg.voice.sip_routes")

router = APIRouter(
    prefix="/voice/sip", tags=["sip-voice"], dependencies=[Depends(verify_sip_gateway_token)]
)


class StartCallRequest(BaseModel):
    call_id: str
    from_number: str = ""
    to_number: str = ""


class TurnRequest(BaseModel):
    call_id: str
    utterance: str = ""
    confidence: float | None = None


class StatusRequest(BaseModel):
    call_id: str
    reason: str = ""


class TurnResponse(BaseModel):
    """What the gateway should say next, and whether to keep listening."""

    lines: list[str]
    expect_reply: bool
    ticket_id: str | None = None


def _to_response(outcome: orchestrator.TurnOutcome) -> TurnResponse:
    return TurnResponse(
        lines=twiml.spoken_lines(outcome.twiml),
        expect_reply=twiml.expects_reply(outcome.twiml),
        ticket_id=str(outcome.ticket_id) if outcome.ticket_id else None,
    )


_SYSTEM_ERROR_RESPONSE = TurnResponse(lines=[scripts.SYSTEM_ERROR], expect_reply=False, ticket_id=None)


@router.post("/start", response_model=TurnResponse)
async def start_call(payload: StartCallRequest, db: AsyncSession = Depends(get_db)) -> TurnResponse:
    session = await get_or_create_session(
        db,
        call_sid=payload.call_id,
        from_number=payload.from_number,
        to_number=payload.to_number,
        channel=VoiceCallChannel.SIP,
    )

    try:
        outcome = await orchestrator.start_call(session)
        await db.commit()
    except Exception:
        logger.exception("Failed to start SIP call %s", payload.call_id)
        return _SYSTEM_ERROR_RESPONSE

    return _to_response(outcome)


@router.post("/turn", response_model=TurnResponse)
async def turn(
    payload: TurnRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> TurnResponse:
    session = await get_session(db, payload.call_id)
    if session is None:
        logger.warning("Turn for unknown SIP call %s", payload.call_id)
        return _SYSTEM_ERROR_RESPONSE

    try:
        outcome = await orchestrator.handle_turn(
            db, session, utterance=payload.utterance, confidence=payload.confidence
        )
        await db.commit()
    except Exception:
        logger.exception("Turn failed for SIP call %s; escalating", payload.call_id)
        try:
            outcome = await orchestrator.escalate(db, session, EscalationReason.SYSTEM_ERROR)
            await db.commit()
        except Exception:
            logger.exception("Escalation also failed for SIP call %s", payload.call_id)
            return _SYSTEM_ERROR_RESPONSE

    if outcome.ticket_id is not None:
        await call_lifecycle.dispatch_ticket_tasks(db, background_tasks, outcome.ticket_id)

    logger.info(
        "sip call=%s state=%s misunderstandings=%s stt_confidence=%s",
        payload.call_id,
        session.state.value,
        session.misunderstanding_count,
        payload.confidence,
    )
    return _to_response(outcome)


@router.post("/status", status_code=204)
async def call_status(
    payload: StatusRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> None:
    """The gateway calls this once the SIP call ends (BYE either direction)."""
    session = await get_session(db, payload.call_id)
    if session is None:
        return

    await call_lifecycle.finalize_call(db, session, background_tasks, ended_reason=payload.reason)

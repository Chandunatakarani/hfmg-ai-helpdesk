"""Load, create, and persist per-call conversation state."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VoiceCallChannel, VoiceCallSession, VoiceCallState

# Caller ID values Twilio sends when the number is withheld or unavailable.
_UNUSABLE_CALLER_IDS = {"", "anonymous", "unknown", "restricted", "private", "+266696687"}


def caller_id_is_usable(from_number: str) -> bool:
    normalized = (from_number or "").strip().lower()
    if normalized in _UNUSABLE_CALLER_IDS:
        return False
    return sum(c.isdigit() for c in normalized) >= 10


async def get_session(db: AsyncSession, call_sid: str) -> VoiceCallSession | None:
    stmt = select(VoiceCallSession).where(VoiceCallSession.twilio_call_sid == call_sid)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_or_create_session(
    db: AsyncSession,
    *,
    call_sid: str,
    from_number: str,
    to_number: str,
    channel: VoiceCallChannel = VoiceCallChannel.TWILIO,
) -> VoiceCallSession:
    """Idempotent by call id, so a retried turn 0 (Twilio, or the SIP gateway) resumes the same call."""
    existing = await get_session(db, call_sid)
    if existing is not None:
        return existing

    session = VoiceCallSession(
        twilio_call_sid=call_sid,
        channel=channel,
        from_number=from_number or "unknown",
        to_number=to_number or "unknown",
        state=VoiceCallState.GREETING,
        collected={"phone_number": from_number} if caller_id_is_usable(from_number) else {},
        turns=[],
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


def record_turn(session: VoiceCallSession, *, role: str, text: str, confidence: float | None = None) -> None:
    """Append to the transcript.

    Reassigns the list because SQLAlchemy does not track in-place mutation of
    a JSONB column.
    """
    session.turns = [
        *session.turns,
        {
            "role": role,
            "text": text,
            "confidence": confidence,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    ]


def update_collected(session: VoiceCallSession, **values) -> None:
    """Merge slot values into the collected payload (same JSONB caveat)."""
    session.collected = {**session.collected, **values}


def transcript_text(session: VoiceCallSession) -> str:
    lines = []
    for turn in session.turns:
        speaker = "Agent" if turn.get("role") == "agent" else "Caller"
        lines.append(f"{speaker}: {turn.get('text', '')}")
    return "\n".join(lines)


async def save(db: AsyncSession, session: VoiceCallSession) -> None:
    await db.commit()

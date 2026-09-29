"""SIP gateway endpoint tests: bearer-token auth and the HTTP layer.

Conversation-logic coverage (NLU, ticket creation, escalation) lives in
test_voice_agent.py against orchestrator.py directly -- these tests only
check that the SIP transport wires into that same state machine correctly
and renders plain text/JSON instead of TwiML.
"""

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Category, Priority, Ticket, VoiceCallChannel, VoiceCallSession, VoiceCallState
from app.voice import nlu

pytestmark = pytest.mark.asyncio(loop_scope="session")

settings = get_settings()

START_URL = "/api/v1/voice/sip/start"
TURN_URL = "/api/v1/voice/sip/turn"
STATUS_URL = "/api/v1/voice/sip/status"

AUTH = {"Authorization": "Bearer test-sip-token"}


@pytest.fixture(autouse=True)
def _configure_token(monkeypatch):
    monkeypatch.setattr(settings, "voice_sip_gateway_token", "test-sip-token")


def _async(value):
    async def _coro():
        return value

    return _coro()


async def test_start_call_greets_and_creates_sip_session(client, db_session):
    response = await client.post(
        START_URL,
        json={"call_id": "sip-call-1", "from_number": "+18455550142", "to_number": "3000"},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["expect_reply"] is True
    assert body["ticket_id"] is None
    assert any("Thank you for calling" in line for line in body["lines"])

    session = (
        await db_session.execute(
            select(VoiceCallSession).where(VoiceCallSession.twilio_call_sid == "sip-call-1")
        )
    ).scalar_one()
    assert session.channel == VoiceCallChannel.SIP
    assert session.state == VoiceCallState.COLLECT_DESCRIPTION
    assert session.collected["phone_number"] == "+18455550142"


async def test_start_call_is_idempotent(client, db_session):
    payload = {"call_id": "sip-call-dup", "from_number": "+18455550142", "to_number": "3000"}
    await client.post(START_URL, json=payload, headers=AUTH)
    await client.post(START_URL, json=payload, headers=AUTH)

    sessions = (
        await db_session.execute(
            select(VoiceCallSession).where(VoiceCallSession.twilio_call_sid == "sip-call-dup")
        )
    ).scalars().all()
    assert len(sessions) == 1


async def test_turn_for_unknown_call_returns_system_error(client):
    response = await client.post(
        TURN_URL, json={"call_id": "sip-nonexistent", "utterance": "hi"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["expect_reply"] is False


async def test_missing_token_is_rejected(client):
    response = await client.post(START_URL, json={"call_id": "sip-noauth"})
    assert response.status_code == 401


async def test_wrong_token_is_rejected(client):
    response = await client.post(
        START_URL, json={"call_id": "sip-badauth"}, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


async def test_full_sip_call_creates_phone_ticket(client, db_session, monkeypatch):
    db_session.add(Category(name="Other", default_priority=Priority.MEDIUM))
    db_session.add(Category(name="Network", default_priority=Priority.HIGH))
    await db_session.commit()

    monkeypatch.setattr(
        nlu,
        "interpret_description",
        lambda u: _async(
            nlu.TurnResult(
                value="No internet in the back office.",
                confidence="high",
                unable_to_determine=False,
                category="Network",
                priority=Priority.HIGH,
                impact="the back office",
                extras={"short_issue": "the network"},
            )
        ),
    )
    monkeypatch.setattr(
        nlu,
        "interpret_name",
        lambda u: _async(nlu.TurnResult(value="John Smith", unable_to_determine=False)),
    )
    monkeypatch.setattr(
        nlu,
        "interpret_email",
        lambda u: _async(nlu.TurnResult(unable_to_determine=False, extras={"declined": True})),
    )

    await client.post(
        START_URL,
        json={"call_id": "sip-full", "from_number": "+18455550142", "to_number": "3000"},
        headers=AUTH,
    )
    await client.post(
        TURN_URL,
        json={"call_id": "sip-full", "utterance": "no internet back office", "confidence": 0.91},
        headers=AUTH,
    )
    await client.post(TURN_URL, json={"call_id": "sip-full", "utterance": "John Smith"}, headers=AUTH)
    final = await client.post(TURN_URL, json={"call_id": "sip-full", "utterance": "skip"}, headers=AUTH)

    body = final.json()
    assert body["ticket_id"] is not None
    assert any("Your ticket number is" in line for line in body["lines"])

    ticket = (await db_session.execute(select(Ticket))).scalars().one()
    assert ticket.caller_name == "John Smith"
    assert ticket.phone_number == "+18455550142"
    assert ticket.priority == Priority.HIGH


async def test_status_salvages_abandoned_sip_call(client, db_session, monkeypatch):
    db_session.add(Category(name="Other", default_priority=Priority.MEDIUM))
    await db_session.commit()

    monkeypatch.setattr(
        nlu,
        "interpret_description",
        lambda u: _async(
            nlu.TurnResult(
                value="Printer is jammed.",
                confidence="high",
                unable_to_determine=False,
                category="Other",
                priority=Priority.LOW,
                extras={"short_issue": "the printer"},
            )
        ),
    )

    await client.post(
        START_URL,
        json={"call_id": "sip-drop", "from_number": "+18455550142", "to_number": "3000"},
        headers=AUTH,
    )
    await client.post(TURN_URL, json={"call_id": "sip-drop", "utterance": "printer jammed"}, headers=AUTH)
    response = await client.post(
        STATUS_URL, json={"call_id": "sip-drop", "reason": "bye"}, headers=AUTH
    )

    assert response.status_code == 204
    ticket = (await db_session.execute(select(Ticket))).scalars().one()
    assert "INCOMPLETE VOICE INTAKE" in ticket.description
    assert ticket.caller_name == "Unknown caller (voice)"

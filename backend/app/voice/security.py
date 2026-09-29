"""Voice-transport authentication: Twilio webhook signatures and the SIP gateway token.

These endpoints are publicly reachable and create records, so this is the
only thing standing between an outside caller and forged tickets.
"""

import hmac
import logging

from fastapi import HTTPException, Request, status
from twilio.request_validator import RequestValidator

from app.core.config import get_settings

logger = logging.getLogger("hfmg.voice.security")

settings = get_settings()


def _expected_url(request: Request) -> str:
    """The URL Twilio signed, which may differ from the URL we received.

    Behind a tunnel or proxy the app sees an internal host; Twilio signed the
    public one, so the signature only matches if we reconstruct it.
    """
    if settings.twilio_public_base_url:
        base = settings.twilio_public_base_url.rstrip("/")
        return f"{base}{request.url.path}"
    return str(request.url)


async def verify_twilio_signature(request: Request) -> None:
    if not settings.twilio_validate_signature:
        logger.warning("Twilio signature validation is DISABLED -- local testing only")
        return

    if not settings.twilio_auth_token:
        logger.error("TWILIO_AUTH_TOKEN is not set; rejecting webhook")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook not configured")

    signature = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}

    validator = RequestValidator(settings.twilio_auth_token)
    if not validator.validate(_expected_url(request), params, signature):
        logger.warning("Rejected Twilio webhook with invalid signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")


async def verify_sip_gateway_token(request: Request) -> None:
    if not settings.voice_sip_gateway_token:
        logger.error("VOICE_SIP_GATEWAY_TOKEN is not set; rejecting SIP gateway request")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="SIP gateway not configured")

    auth_header = request.headers.get("Authorization", "")
    scheme, _, presented = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    # Constant-time compare -- this token is a long-lived shared secret, not a
    # single-use signature, so a timing side-channel is worth closing.
    if not hmac.compare_digest(presented, settings.voice_sip_gateway_token):
        logger.warning("Rejected SIP gateway request with invalid token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

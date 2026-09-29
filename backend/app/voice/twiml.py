"""TwiML response builders.

Uses the Twilio SDK rather than hand-built XML so caller-supplied values
(names, emails) are escaped correctly.
"""

import re

from twilio.twiml.voice_response import Gather, VoiceResponse

from app.core.config import get_settings

settings = get_settings()

GATHER_ACTION = "/api/v1/webhooks/twilio/voice/gather"


def ask(prompt: str) -> str:
    """Speak a prompt and wait for the caller's reply."""
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=GATHER_ACTION,
        method="POST",
        speech_timeout="auto",
        speech_model=settings.voice_speech_model,
        language=settings.voice_language,
        barge_in=True,
        timeout=settings.voice_gather_timeout,
    )
    gather.say(prompt, voice=settings.voice_tts_voice)
    response.append(gather)
    # Reached only when the caller says nothing at all -- re-enters the state
    # machine so silence counts as a failure rather than dropping the call.
    response.redirect(f"{GATHER_ACTION}?silent=1", method="POST")
    return str(response)


def say_and_hangup(*lines: str) -> str:
    """Speak one or more final lines, then end the call."""
    response = VoiceResponse()
    for line in lines:
        if line:
            response.say(line, voice=settings.voice_tts_voice)
    response.hangup()
    return str(response)


def say_then_ask(statement: str, prompt: str) -> str:
    """Speak a statement, then ask a question in the same turn.

    Used to acknowledge what the caller said before asking the next question,
    which keeps the exchange from feeling like an interrogation.
    """
    response = VoiceResponse()
    response.say(statement, voice=settings.voice_tts_voice)
    gather = Gather(
        input="speech",
        action=GATHER_ACTION,
        method="POST",
        speech_timeout="auto",
        speech_model=settings.voice_speech_model,
        language=settings.voice_language,
        barge_in=True,
        timeout=settings.voice_gather_timeout,
    )
    gather.say(prompt, voice=settings.voice_tts_voice)
    response.append(gather)
    response.redirect(f"{GATHER_ACTION}?silent=1", method="POST")
    return str(response)


def spoken_lines(twiml_body: str) -> list[str]:
    """Recover the plain text an `ask`/`say_and_hangup`/`say_then_ask` result speaks.

    Lets a non-Twilio channel (the SIP gateway) reuse the orchestrator's TwiML
    output as its source of truth, instead of duplicating the state machine's
    scripted responses in a second format.
    """
    return re.findall(r"<Say[^>]*>(.*?)</Say>", twiml_body, re.DOTALL)


def expects_reply(twiml_body: str) -> bool:
    """Whether the caller should be prompted for another turn, or the call is ending."""
    return "<Gather" in twiml_body

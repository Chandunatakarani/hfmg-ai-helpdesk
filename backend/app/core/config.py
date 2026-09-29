from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hfmg:hfmg_dev_local@localhost:5432/hfmg_helpdesk"

    cors_origins: str = "http://localhost:5173"

    # Surfaced read-only by GET /settings/status (DESIGN.md §12) so the
    # dashboard never has to guess which deployment it's looking at.
    environment: str = "development"

    enable_email_notifications: bool = True

    # --- Email provider (Twilio SendGrid, via its API -- not SMTP) ---
    email_provider: str = "sendgrid"
    sendgrid_api_key: str = ""
    sendgrid_timeout_seconds: float = 10.0
    sendgrid_max_retries: int = 2
    # Fixed sender/recipient by design: every ticket notification goes from
    # EMAIL_FROM to HELPDESK_EMAIL, not a per-call choice.
    email_from: str = "reminder@hfmg.net"
    helpdesk_email: str = "helpdesk@hfmg.net"

    # --- Outlook provider (internal Exchange/Graph mail service, another server) ---
    # No trailing slash, e.g. "http://172.22.6.98:4200". Endpoint path is fixed:
    # POST {outlook_api_base_url}/api/Values/mail/send.
    outlook_api_base_url: str = "http://172.22.6.98:4200"
    outlook_send_timeout_seconds: float = 10.0
    outlook_send_max_retries: int = 2

    enable_ai_summary: bool = False

    # --- LLM provider ---
    llm_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-5-nano"
    # Point at a compatible gateway or proxy without code changes.
    openai_base_url: str = ""
    openai_timeout_seconds: float = 20.0
    openai_max_retries: int = 2
    openai_max_output_tokens: int = 2000
    # Both are omitted from requests unless set. Reasoning models (the gpt-5
    # family) reject `temperature` outright, so sending it by default would
    # break every call; leaving these unset keeps new models working as they
    # ship, while older models can still be tuned via env vars.
    openai_temperature: float | None = None
    openai_reasoning_effort: str = ""

    # --- Twilio voice agent (Phase 2) ---
    twilio_auth_token: str = ""
    # Signature validation is mandatory in production; disabling it is only for
    # local testing without a real Twilio account.
    twilio_validate_signature: bool = True
    # Set when running behind a tunnel/proxy so the signature is checked against
    # the URL Twilio actually called, not the internal one.
    twilio_public_base_url: str = ""
    voice_tts_voice: str = "Polly.Joanna-Neural"
    voice_language: str = "en-US"
    voice_speech_model: str = "experimental_conversations"
    voice_gather_timeout: int = 6
    voice_nlu_timeout_seconds: float = 4.0
    # Retries share the timeout above, since a caller is waiting on the line.
    # Keep low: Twilio abandons the webhook at roughly 15 seconds.
    voice_nlu_max_retries: int = 1
    voice_max_misunderstandings: int = 3
    voice_max_email_attempts: int = 2

    # --- SIP gateway (Nextiva extension, via a SIPSorcery service on another
    # server) -- a second front end for the same voice orchestrator above.
    # Shared secret the gateway presents as `Authorization: Bearer <token>`;
    # there's no per-request signature scheme here the way Twilio has one.
    voice_sip_gateway_token: str = ""

    @field_validator("openai_temperature", mode="before")
    @classmethod
    def _blank_temperature_is_unset(cls, value: Any) -> Any:
        """Treat `OPENAI_TEMPERATURE=` in a .env file as "don't send it".

        Without this, shipping the variable blank (which is the default, since
        reasoning models reject temperature) fails validation on startup.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

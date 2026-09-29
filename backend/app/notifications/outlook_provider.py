"""Outlook provider: calls the internal Exchange/Graph mail service running on
another server (SummarizeMail.Service.ExchangeMailService.SendMailAsServiceAccount,
exposed via POST /api/Values/mail/send) instead of talking to Microsoft Graph
directly. That server has no auth in front of it (internal network only), so
this client sends no credentials.

The endpoint is `[Consumes("multipart/form-data")]` and binds `List<string>`
form fields (To/Cc) by repeated same-name parts, not JSON arrays -- so this
uses httpx's `files=` form with `(None, value)` tuples to force multipart
encoding for plain fields, per field, rather than `data=`.
"""

import asyncio
import logging
import random

import httpx

from app.core.config import get_settings

logger = logging.getLogger("hfmg.notifications.outlook")

settings = get_settings()

SEND_MAIL_PATH = "/api/Values/mail/send"

# Same rationale as SendGridProvider: only transient failures are worth
# retrying. A 400 (e.g. missing recipient) fails identically every time.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OutlookProvider:
    name = "outlook"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(settings.outlook_api_base_url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def send(
        self,
        *,
        to: str,
        from_email: str,
        subject: str,
        body: str,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> bool:
        if not self.is_configured:
            logger.warning("OUTLOOK_API_BASE_URL is not set; cannot send email")
            return False

        request_timeout = timeout if timeout is not None else settings.outlook_send_timeout_seconds
        attempts = (max_retries if max_retries is not None else settings.outlook_send_max_retries) + 1

        url = settings.outlook_api_base_url.rstrip("/") + SEND_MAIL_PATH
        body_html = body.replace("\n", "<br>")

        # (None, value) forces multipart/form-data for a plain field (no
        # filename), matching [Consumes("multipart/form-data")]. Repeating
        # "To" binds ASP.NET's List<string> model binder.
        fields = [
            ("To", (None, to)),
            ("FromEmail", (None, from_email)),
            ("Subject", (None, subject)),
            ("BodyHtml", (None, body_html)),
        ]

        client = self._get_client()

        for attempt in range(1, attempts + 1):
            try:
                response = await client.post(url, files=fields, timeout=request_timeout)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt >= attempts:
                    logger.warning(
                        "Outlook mail service request failed after %s attempt(s): %s",
                        attempt,
                        type(exc).__name__,
                    )
                    return False
                await self._backoff(attempt)
                continue
            except Exception:
                logger.exception("Outlook mail service request failed with an unexpected error")
                return False

            if response.status_code == 200:
                logger.info("Outlook mail service accepted message")
                return True

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                logger.info(
                    "Outlook mail service returned %s on attempt %s/%s; retrying",
                    response.status_code,
                    attempt,
                    attempts,
                )
                await self._backoff(attempt)
                continue

            # Non-retryable, or retries exhausted. Body not logged -- it can
            # echo back request content (recipient, subject).
            logger.warning(
                "Outlook mail service send failed with status %s after %s attempt(s)",
                response.status_code,
                attempt,
            )
            return False

        return False

    @staticmethod
    async def _backoff(attempt: int) -> None:
        delay = min(0.25 * (2 ** (attempt - 1)), 2.0) * (0.5 + random.random() / 2)
        await asyncio.sleep(delay)

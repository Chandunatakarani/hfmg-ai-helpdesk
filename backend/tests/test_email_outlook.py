"""Outlook provider tests.

The httpx client is mocked throughout, so these run offline with no live
server -- they assert our integration contract with the internal Exchange
mail service: request shape (multipart fields), retry policy, and that
failures never leak recipient/subject/body into logs.
"""

import logging

import httpx
import pytest

from app.core.config import get_settings
from app.notifications.outlook_provider import OutlookProvider

pytestmark = pytest.mark.asyncio(loop_scope="session")

settings = get_settings()


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient, recording calls and replaying outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def post(self, url, *, files, timeout):
        self.calls.append({"url": url, "files": files, "timeout": timeout})
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(status_code):
    return httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "http://172.22.6.98:4200/api/Values/mail/send"),
    )


def make_provider(monkeypatch, outcomes, *, base_url="http://172.22.6.98:4200"):
    monkeypatch.setattr(settings, "outlook_api_base_url", base_url)
    provider = OutlookProvider()
    client = FakeAsyncClient(outcomes)
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider, client


# --- request shape --------------------------------------------------------


async def test_send_success_posts_expected_fields(monkeypatch):
    provider, client = make_provider(monkeypatch, [_response(200)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="Subj", body="Line one\nLine two"
    )

    assert result is True
    call = client.calls[0]
    assert call["url"] == "http://172.22.6.98:4200/api/Values/mail/send"
    fields = dict(call["files"])
    assert fields["To"] == (None, "helpdesk@hfmg.net")
    assert fields["FromEmail"] == (None, "reminder@hfmg.net")
    assert fields["Subject"] == (None, "Subj")
    assert fields["BodyHtml"] == (None, "Line one<br>Line two")


async def test_unconfigured_provider_returns_false(monkeypatch):
    monkeypatch.setattr(settings, "outlook_api_base_url", "")
    provider = OutlookProvider()
    assert provider.is_configured is False
    assert await provider.send(to="a@b.com", from_email="c@d.com", subject="s", body="b") is False


# --- retry policy -----------------------------------------------------


async def test_retries_on_429_then_succeeds(monkeypatch):
    provider, client = make_provider(monkeypatch, [_response(429), _response(200)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=1
    )

    assert result is True
    assert len(client.calls) == 2


async def test_retries_on_5xx(monkeypatch):
    provider, client = make_provider(monkeypatch, [_response(503), _response(200)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=1
    )

    assert result is True
    assert len(client.calls) == 2


async def test_does_not_retry_on_400(monkeypatch):
    """A missing recipient fails identically every time; retrying wastes nothing but time."""
    provider, client = make_provider(monkeypatch, [_response(400), _response(200)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=3
    )

    assert result is False
    assert len(client.calls) == 1


async def test_gives_up_after_max_retries(monkeypatch):
    provider, client = make_provider(monkeypatch, [_response(503), _response(503), _response(503)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=2
    )

    assert result is False
    assert len(client.calls) == 3  # initial + 2 retries


async def test_retries_connection_error(monkeypatch):
    provider, client = make_provider(monkeypatch, [httpx.ConnectError("boom"), _response(200)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=1
    )

    assert result is True
    assert len(client.calls) == 2


async def test_no_retries_means_single_attempt(monkeypatch):
    provider, client = make_provider(monkeypatch, [_response(503)])

    result = await provider.send(
        to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body="b", max_retries=0
    )

    assert result is False
    assert len(client.calls) == 1


# --- no PHI in logs on failure ---------------------------------------------


async def test_failure_does_not_log_body_or_recipient(monkeypatch, caplog):
    """Core requirement: a failed send must never leak ticket content to logs."""
    provider, _ = make_provider(monkeypatch, [_response(400)])

    secret_body = "Caller: Maria Lopez\nDescription: patient scheduling issue for John Doe"
    with caplog.at_level(logging.WARNING, logger="hfmg.notifications.outlook"):
        result = await provider.send(
            to="helpdesk@hfmg.net",
            from_email="reminder@hfmg.net",
            subject="New ticket HFMG-2026-000123",
            body=secret_body,
        )

    assert result is False
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Maria Lopez" not in log_text
    assert "John Doe" not in log_text
    assert "patient" not in log_text
    assert "helpdesk@hfmg.net" not in log_text
    assert "HFMG-2026-000123" not in log_text  # subject also excluded


async def test_connection_error_failure_does_not_log_body(monkeypatch, caplog):
    provider, _ = make_provider(monkeypatch, [httpx.ConnectError("boom")])

    secret_body = "Description: sensitive patient detail"
    with caplog.at_level(logging.WARNING, logger="hfmg.notifications.outlook"):
        result = await provider.send(
            to="helpdesk@hfmg.net", from_email="reminder@hfmg.net", subject="s", body=secret_body,
            max_retries=0,
        )

    assert result is False
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sensitive patient detail" not in log_text

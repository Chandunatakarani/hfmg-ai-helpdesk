"""Cached reachability checks for OpenAI, Twilio, Email (SendGrid), and the
database -- backs `GET /health/dependencies` and `GET /settings/status`.

DESIGN.md §6.1 calls for "a lightweight reachability check per dependency,
cached for a short interval so the dashboard doesn't trigger a live
OpenAI/Twilio call on every page load." This module is that cache, plus the
checks themselves.

Each `check_*` function is a standalone, monkeypatchable unit (same pattern
as `app.ai.summarizer.get_provider`) so tests never need real network access
or real credentials -- see tests/test_health_dependencies.py.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

settings = get_settings()

DependencyStatusValue = Literal["operational", "degraded", "down", "unknown"]

# Matches the frontend's StatusBar poll interval (30s) -- within one poll
# window, a repeat check is served from cache instead of re-pinging a
# billed/rate-limited third party.
_CACHE_TTL_SECONDS = 30.0
_CHECK_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class DependencyCheck:
    status: DependencyStatusValue
    checked_at: datetime


_cache: dict[str, tuple[float, DependencyCheck]] = {}


def reset_cache() -> None:
    """Test-only: clears the in-memory cache so tests don't leak state into each other."""
    _cache.clear()


async def _cached(key: str, check_fn) -> DependencyCheck:
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]
    result = await check_fn()
    _cache[key] = (now, result)
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def check_database(db: AsyncSession) -> DependencyCheck:
    try:
        await db.execute(text("SELECT 1"))
        return DependencyCheck(status="operational", checked_at=_now())
    except Exception:
        return DependencyCheck(status="down", checked_at=_now())


async def check_openai() -> DependencyCheck:
    if not settings.openai_api_key:
        return DependencyCheck(status="down", checked_at=_now())

    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
        return DependencyCheck(status="operational" if response.status_code < 400 else "down", checked_at=_now())
    except Exception:
        return DependencyCheck(status="down", checked_at=_now())


async def check_twilio() -> DependencyCheck:
    # A real reachability call would need TWILIO_ACCOUNT_SID (Twilio's REST
    # API authenticates with account_sid + auth_token via Basic Auth) --
    # that setting doesn't exist in app/core/config.py today, and adding a
    # new required secret just to back a status indicator is out of scope
    # for this change (see BACKEND_GAP_ANALYSIS.md). This stands in with a
    # configuration check: "is the credential we do have set at all."
    status: DependencyStatusValue = "operational" if settings.twilio_auth_token else "down"
    return DependencyCheck(status=status, checked_at=_now())


async def check_email() -> DependencyCheck:
    if not settings.outlook_api_base_url:
        return DependencyCheck(
            status="down",
            checked_at=_now()
        )

    try:
        async with httpx.AsyncClient(
            timeout=_CHECK_TIMEOUT_SECONDS
        ) as client:

            response = await client.get(
                settings.outlook_api_base_url
            )

        return DependencyCheck(
            status="operational" if response.status_code < 500 else "down",
            checked_at=_now()
        )

    except Exception:
        return DependencyCheck(
            status="down",
            checked_at=_now()
        )


async def get_dependency_health(db: AsyncSession) -> dict[str, DependencyCheck]:
    return {
        "openai": await _cached("openai", check_openai),
        "twilio": await _cached("twilio", check_twilio),
        "database": await _cached("database", lambda: check_database(db)),
        "email": await _cached("email", check_email),
    }

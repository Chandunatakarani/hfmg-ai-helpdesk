"""Email provider selection.

Adding a provider means writing one module implementing EmailProvider and
registering it here; nothing above this layer changes.
"""

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.notifications.base import EmailProvider
from app.notifications.outlook_provider import OutlookProvider
from app.notifications.sendgrid_provider import SendGridProvider

logger = logging.getLogger("hfmg.notifications.factory")

settings = get_settings()

_PROVIDERS = {
    "sendgrid": SendGridProvider,
    "outlook": OutlookProvider,
}


@lru_cache
def get_email_provider() -> EmailProvider:
    name = (settings.email_provider or "sendgrid").lower()
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        logger.error("Unknown EMAIL_PROVIDER %r; falling back to sendgrid", name)
        provider_cls = SendGridProvider
    return provider_cls()

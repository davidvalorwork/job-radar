"""Contact guard for future delivery adapters. No sending functionality."""

from datetime import datetime, timedelta


def may_contact(
    *,
    now: datetime,
    last_recipient_contact: datetime | None,
    last_company_contact: datetime | None,
    suppressed: bool,
    unresolved_send: bool,
    official_contact_verified: bool,
    cooldown_hours: int = 48,
) -> bool:
    if now.utcoffset() is None or cooldown_hours < 1:
        raise ValueError("An aware clock and positive cooldown are required")
    if suppressed or unresolved_send or not official_contact_verified:
        return False
    for previous in (last_recipient_contact, last_company_contact):
        if previous is not None:
            if previous.utcoffset() is None:
                raise ValueError("Contact timestamps must be timezone-aware")
            if now - previous < timedelta(hours=cooldown_hours):
                return False
    return True

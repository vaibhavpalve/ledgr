"""The rules of receipt chasing, as pure functions (ADR-114). No I/O here.

    cadence        weekly (default) or fortnightly, per client, opt-in
    window         the calendar window a cadence allows ONE scheduled send in: the ISO week
                   ('2026-W41') or a pair of ISO weeks ('2026-F21'), in UTC
    send hours     scheduled sends go out on weekdays between 08:00 and 16:00 UTC (09:00-17:00 or
                   10:00-18:00 in the Netherlands), so a new week starts with a Monday-morning
                   mail rather than one at midnight; manual requests go whenever the sweep runs
    manual gap     a manual request is refused within 24 hours of the client's last chase or last
                   request (decision 5's "24h minimum gap")

UTC rather than Europe/Amsterdam: the API has no tz database dependency (Windows hosts ship none),
and a window that turns over at 01:00 or 02:00 Dutch time instead of midnight changes nothing a
person notices.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

MANUAL_GAP = timedelta(hours=24)
#: A request the sweep could not deliver within this long lapses (the accountant asks again).
REQUEST_LIFETIME = timedelta(hours=24)
SEND_HOURS_UTC = range(8, 16)


class Cadence(enum.StrEnum):
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"


class BlockedReason(enum.StrEnum):
    NOTHING_MISSING = "nothing_missing"
    CHASED_RECENTLY = "chased_recently"
    NO_RECIPIENT = "no_recipient"


class SendKind(enum.StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("chasing works on aware datetimes only")
    return at.astimezone(UTC)


def window_key(cadence: Cadence, at: datetime) -> str:
    """The cadence window `at` falls in. Weekly: the ISO week. Fortnightly: ISO weeks 1-2 are
    F01, 3-4 F02, ... - a 53-week year's last fortnight is one week long, which at worst sends
    two chases a week apart once every few years."""
    year, week, _ = _utc(at).isocalendar()
    if cadence is Cadence.WEEKLY:
        return f"{year}-W{week:02d}"
    return f"{year}-F{(week + 1) // 2:02d}"


def in_send_hours(at: datetime) -> bool:
    moment = _utc(at)
    return moment.weekday() < 5 and moment.hour in SEND_HOURS_UTC


def blocked_reason(
    *,
    missing_count: int,
    last_chased_at: datetime | None,
    last_requested_at: datetime | None,
    recipient_count: int | None,
    now: datetime,
) -> BlockedReason | None:
    """Why a manual chase of this client cannot go now, or None when it can.

    `recipient_count` None means "not counted yet" (the sweep has not visited this client): that
    is not a block - the sweep resolves the recipients when it delivers, and skips if there are
    none. Checked in the order a person would want to hear it: nothing to ask for, asked too
    recently, nobody to ask.
    """
    if missing_count <= 0:
        return BlockedReason.NOTHING_MISSING
    latest = max((t for t in (last_chased_at, last_requested_at) if t is not None), default=None)
    if latest is not None and _utc(now) - _utc(latest) < MANUAL_GAP:
        return BlockedReason.CHASED_RECENTLY
    if recipient_count is not None and recipient_count <= 0:
        return BlockedReason.NO_RECIPIENT
    return None

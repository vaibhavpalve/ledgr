"""ADR-114's chasing rules, without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api.chasing.model import BlockedReason, Cadence, blocked_reason, in_send_hours, window_key

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)  # a Wednesday, ISO week 41


def test_weekly_windows_are_iso_weeks() -> None:
    assert window_key(Cadence.WEEKLY, NOW) == "2026-W41"
    # Monday 00:00 starts a new window; Sunday 23:59 is still the old one.
    assert window_key(Cadence.WEEKLY, datetime(2026, 10, 12, 0, 0, tzinfo=UTC)) == "2026-W42"
    assert window_key(Cadence.WEEKLY, datetime(2026, 10, 11, 23, 59, tzinfo=UTC)) == "2026-W41"


def test_fortnightly_windows_pair_iso_weeks() -> None:
    w41 = window_key(Cadence.FORTNIGHTLY, NOW)
    w42 = window_key(Cadence.FORTNIGHTLY, NOW + timedelta(days=7))
    w43 = window_key(Cadence.FORTNIGHTLY, NOW + timedelta(days=14))
    assert w41 == w42 == "2026-F21"
    assert w43 == "2026-F22"
    assert window_key(Cadence.FORTNIGHTLY, datetime(2027, 1, 4, tzinfo=UTC)) == "2027-F01"


def test_window_keys_need_an_aware_time() -> None:
    with pytest.raises(ValueError):
        window_key(Cadence.WEEKLY, datetime(2026, 10, 7, 10, 0))


def test_scheduled_sends_wait_for_weekday_office_hours() -> None:
    assert in_send_hours(NOW)
    assert not in_send_hours(NOW.replace(hour=7, minute=59))
    assert not in_send_hours(NOW.replace(hour=16))
    assert not in_send_hours(datetime(2026, 10, 10, 10, 0, tzinfo=UTC))  # Saturday


def _blocked(**overrides: object) -> BlockedReason | None:
    facts: dict[str, object] = {
        "missing_count": 3,
        "last_chased_at": None,
        "last_requested_at": None,
        "recipient_count": 1,
        "now": NOW,
    }
    facts.update(overrides)
    return blocked_reason(**facts)  # type: ignore[arg-type]


def test_nothing_blocks_a_first_chase() -> None:
    assert _blocked() is None
    # Not counted yet is not "nobody": the sweep decides when it delivers.
    assert _blocked(recipient_count=None) is None


def test_nothing_missing_blocks_first() -> None:
    assert _blocked(missing_count=0, recipient_count=0) is BlockedReason.NOTHING_MISSING


def test_the_24_hour_gap_counts_sends_and_requests() -> None:
    assert _blocked(last_chased_at=NOW - timedelta(hours=23)) is BlockedReason.CHASED_RECENTLY
    assert _blocked(last_requested_at=NOW - timedelta(hours=2)) is BlockedReason.CHASED_RECENTLY
    assert _blocked(last_chased_at=NOW - timedelta(hours=24, minutes=1)) is None


def test_nobody_to_mail_blocks() -> None:
    assert _blocked(recipient_count=0) is BlockedReason.NO_RECIPIENT

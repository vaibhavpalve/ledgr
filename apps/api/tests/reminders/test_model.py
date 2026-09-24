"""ADR-090: which reminders are due on a given day."""

from __future__ import annotations

import uuid
from datetime import date

from api.reminders.model import (
    OpenPeriod,
    ReminderKind,
    due_date,
    receipts_reminder,
    vat_reminders,
)

Q3 = OpenPeriod(uuid.uuid4(), date(2026, 7, 1), date(2026, 9, 30), has_vat_postings=True)


def test_the_due_date_is_the_last_day_of_the_next_month() -> None:
    assert due_date(date(2026, 9, 30)) == date(2026, 10, 31)
    assert due_date(date(2026, 12, 31)) == date(2027, 1, 31)
    assert due_date(date(2027, 1, 31)) == date(2027, 2, 28)


def test_nothing_is_due_while_the_period_runs_or_more_than_a_week_ahead() -> None:
    assert vat_reminders([Q3], today=date(2026, 9, 30)) == []
    assert vat_reminders([Q3], today=date(2026, 10, 23)) == []


def test_the_week_before_the_deadline_is_a_due_reminder() -> None:
    [reminder] = vat_reminders([Q3], today=date(2026, 10, 24))
    assert reminder.kind is ReminderKind.VAT_DUE
    assert reminder.due == date(2026, 10, 31)
    # Every day in the window gives the same key: the log turns a second run into nothing.
    assert vat_reminders([Q3], today=date(2026, 10, 31))[0].key == reminder.key


def test_after_the_deadline_it_is_overdue() -> None:
    [reminder] = vat_reminders([Q3], today=date(2026, 11, 1))
    assert reminder.kind is ReminderKind.VAT_OVERDUE


def test_a_period_with_no_btw_postings_is_not_chased() -> None:
    quiet = OpenPeriod(uuid.uuid4(), date(2026, 1, 1), date(2026, 3, 31), has_vat_postings=False)
    assert vat_reminders([quiet], today=date(2026, 4, 30)) == []


def test_receipts_are_a_weekly_reminder_once_they_have_waited() -> None:
    assert receipts_reminder(waiting=0, oldest=date(2026, 9, 1), today=date(2026, 9, 24)) is None
    assert receipts_reminder(waiting=2, oldest=date(2026, 9, 23), today=date(2026, 9, 24)) is None
    monday = receipts_reminder(waiting=2, oldest=date(2026, 9, 1), today=date(2026, 9, 21))
    friday = receipts_reminder(waiting=5, oldest=date(2026, 9, 1), today=date(2026, 9, 25))
    assert monday is not None and friday is not None
    assert monday.key == friday.key == "receipts:2026-W39"

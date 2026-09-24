"""Which reminders are due today (ADR-090). Pure: dates in, reminders out.

--- BTW deadlines ---

A return is due by the last day of the month after its period (`api.vat.deadlines`). For a period
that has ENDED, is NOT filed, and has BTW postings (a period with nothing in it is not chased -
the same "no BTW postings" the /vat screen shows it as), a person is reminded:

    due     7 days before the due date, once        key vat_due:<period>
    overdue the day after the due date, once        key vat_overdue:<period>

"Once" is the reminder log's unique key, not this module: here a reminder is due on every day
inside its window, so a job that missed a day (or ran late) still sends it, and the log turns the
second run into nothing.

--- Receipts waiting ---

Purchases captured but not booked are costs and input BTW that are not in the books yet. When some
have waited at least `RECEIPTS_WAIT_DAYS`, one reminder per ISO week (key receipts:<year>-W<week>),
so a person with a pile of receipts hears about it weekly, not daily.
"""

from __future__ import annotations

import calendar
import enum
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

DUE_SOON_DAYS = 7
RECEIPTS_WAIT_DAYS = 3


class ReminderKind(enum.Enum):
    VAT_DUE = "vat_due"
    VAT_OVERDUE = "vat_overdue"
    RECEIPTS_WAITING = "receipts_waiting"


@dataclass(frozen=True, slots=True)
class OpenPeriod:
    period_id: uuid.UUID
    start: date
    end: date
    has_vat_postings: bool


@dataclass(frozen=True, slots=True)
class Reminder:
    kind: ReminderKind
    key: str
    #: What the mail says: the period's dates and due date, or the receipt count.
    period_start: date | None = None
    period_end: date | None = None
    due: date | None = None
    count: int | None = None


def due_date(period_end: date) -> date:
    year, month = (
        (period_end.year + 1, 1)
        if period_end.month == 12
        else (period_end.year, period_end.month + 1)
    )
    return date(year, month, calendar.monthrange(year, month)[1])


def vat_reminders(periods: list[OpenPeriod], *, today: date) -> list[Reminder]:
    reminders: list[Reminder] = []
    for period in periods:
        if period.end >= today or not period.has_vat_postings:
            continue
        due = due_date(period.end)
        kind: ReminderKind | None = None
        if due < today:
            kind = ReminderKind.VAT_OVERDUE
        elif due - timedelta(days=DUE_SOON_DAYS) <= today:
            kind = ReminderKind.VAT_DUE
        if kind is None:
            continue
        reminders.append(
            Reminder(
                kind=kind,
                key=f"{kind.value}:{period.period_id}",
                period_start=period.start,
                period_end=period.end,
                due=due,
            )
        )
    return reminders


def receipts_reminder(*, waiting: int, oldest: date | None, today: date) -> Reminder | None:
    if waiting <= 0 or oldest is None or (today - oldest).days < RECEIPTS_WAIT_DAYS:
        return None
    year, week, _ = today.isocalendar()
    return Reminder(
        kind=ReminderKind.RECEIPTS_WAITING, key=f"receipts:{year}-W{week:02d}", count=waiting
    )

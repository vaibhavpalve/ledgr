"""SI-07: recurring invoicing's rules - FR-AR-008, with no database and no invoice.

    FR-AR-008  Recurring/subscription invoicing with schedules, indexation and end
               dates.

Everything that DECIDES is here and pure: on which dates a schedule falls, when it
is finished, what a price is after indexation, and whether a definition is valid at
all. The service supplies the facts (how many runs there have been, today's date) and
turns a run into an invoice through `InvoicingService`; this module never touches one.

--- Dates are derived from the ANCHOR, never chained ---

Occurrence `n` is `start_date + n * interval` months, computed from the start date
each time. Chaining ("the previous date plus a month") drifts: a schedule anchored on
31 January would go 28 Feb, 28 Mar, 28 Apr, and never recover. Anchored, it goes
28 Feb, 31 Mar, 30 Apr - each month's own last day when the anchor day does not
exist, and the anchor day whenever it does.

--- A finished schedule stops at the FIRST of its two limits ---

`end_date` (the last date a run may fall on, inclusive) and `max_runs` (a count) may
both be set; whichever is reached first ends the schedule. Neither set means it runs
until somebody ends it.

--- Indexation compounds, once per COMPLETED year ---

`indexation_percent` raises a price by that percentage on each anniversary of the
start date, compounding. A run in the first year pays the base price; the first run
on or after the first anniversary pays base x (1 + p/100); after two anniversaries,
base x (1 + p/100)^2. The base price is stored unindexed, so a schedule always shows
what was agreed and each invoice what was charged. The result is rounded ONCE, to the
four decimals `sales_invoice_line.unit_price` holds (0037), so the price is never
rounded in the middle of the compounding.

All money is `Decimal` (NFR-031).
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "INTERVALS",
    "MAX_LINES",
    "MAX_RUNS_PER_CALL",
    "DefinitionInvalid",
    "RecurringLine",
    "Schedule",
    "add_months",
    "due_runs",
    "indexed_price",
    "next_run",
    "occurrence",
    "validate_definition",
    "years_elapsed",
]

#: The rhythms a business actually bills on, in months: monthly, every two months,
#: quarterly, every four months, half-yearly, yearly. Mirrors the CHECK in 0056.
INTERVALS: tuple[int, ...] = (1, 2, 3, 4, 6, 12)

MAX_LINES = 100

#: The most invoices ONE schedule generates in ONE call. A schedule left unrun for
#: years would otherwise generate them all at once - dozens of backdated invoices -
#: in one request. The rest wait for the next call.
MAX_RUNS_PER_CALL = 12

_UNIT_PRICE_PLACES = Decimal("0.0001")


class DefinitionInvalid(ValueError):
    """A schedule that could not run, or would bill something nonsensical. Refused
    when it is saved, not discovered when a customer is invoiced."""


@dataclass(frozen=True, slots=True)
class Schedule:
    """The part of a schedule the date rules need."""

    start_date: date
    interval_months: int
    end_date: date | None = None
    max_runs: int | None = None
    indexation_percent: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class RecurringLine:
    description: str
    quantity: Decimal
    #: The BASE price, before indexation.
    unit_price: Decimal
    vat_treatment: str
    discount_percent: Decimal = Decimal(0)


def add_months(anchor: date, months: int) -> date:
    """`anchor` plus `months`, clamped to the last day of a shorter month.

    31 January + 1 month is 28 (or 29) February, not an error and not 3 March.
    """
    index = anchor.year * 12 + (anchor.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def occurrence(schedule: Schedule, index: int) -> date:
    """The date of run number `index` (0 is the start date), from the anchor."""
    if index < 0:
        raise ValueError("a run index is not negative")
    return add_months(schedule.start_date, index * schedule.interval_months)


def _allowed(schedule: Schedule, index: int) -> bool:
    if schedule.max_runs is not None and index >= schedule.max_runs:
        return False
    return schedule.end_date is None or occurrence(schedule, index) <= schedule.end_date


def next_run(schedule: Schedule, runs_generated: int) -> date | None:
    """The date of the next run, or None once the schedule is finished."""
    return occurrence(schedule, runs_generated) if _allowed(schedule, runs_generated) else None


def due_runs(
    schedule: Schedule,
    runs_generated: int,
    today: date,
    *,
    cap: int = MAX_RUNS_PER_CALL,
) -> list[tuple[int, date]]:
    """Every run that should exist by `today` and does not yet: `(index, date)`,
    oldest first, at most `cap`.

    A schedule that was not run for three months returns three entries, each dated
    in its own month - catching up produces the invoices that should have been
    raised, dated when they should have been, not one dated today.
    """
    due: list[tuple[int, date]] = []
    index = runs_generated
    while len(due) < cap and _allowed(schedule, index):
        on = occurrence(schedule, index)
        if on > today:
            break
        due.append((index, on))
        index += 1
    return due


def years_elapsed(start: date, on: date) -> int:
    """Complete years from `start` to `on`: 0 before the first anniversary, 1 from
    it, and so on. Never negative."""
    if on <= start:
        return 0
    years = on.year - start.year
    if (on.month, on.day) < (start.month, start.day):
        years -= 1
    return max(years, 0)


def indexed_price(base: Decimal, percent: Decimal, start: date, on: date) -> Decimal:
    """`base` raised by `percent` for every completed year between `start` and `on`,
    compounding, rounded once to four decimals (half up)."""
    if isinstance(base, float) or isinstance(percent, float):
        raise TypeError("prices and percentages must be Decimal, not float (NFR-031)")
    years = years_elapsed(start, on)
    if years == 0 or percent == 0:
        return base.quantize(_UNIT_PRICE_PLACES, rounding=ROUND_HALF_UP)
    factor = (Decimal(1) + percent / Decimal(100)) ** years
    return (base * factor).quantize(_UNIT_PRICE_PLACES, rounding=ROUND_HALF_UP)


def validate_definition(
    *,
    schedule: Schedule,
    due_days: int | None,
    lines: Sequence[RecurringLine],
) -> None:
    """Raise `DefinitionInvalid` for anything the schedule could not honestly run.

    Checked here so that a bad schedule is refused when somebody saves it, with the
    problem stated, rather than failing silently on the first of the month.
    """
    if schedule.interval_months not in INTERVALS:
        raise DefinitionInvalid(f"the interval is one of {INTERVALS} months")
    if schedule.end_date is not None and schedule.end_date < schedule.start_date:
        raise DefinitionInvalid("the end date is before the start date")
    if schedule.max_runs is not None and schedule.max_runs < 1:
        raise DefinitionInvalid("a schedule runs at least once")
    if not Decimal(0) <= schedule.indexation_percent <= Decimal(100):
        raise DefinitionInvalid("indexation is between 0 and 100 percent")
    if due_days is not None and not 0 <= due_days <= 365:
        raise DefinitionInvalid("the payment term is between 0 and 365 days")

    if not lines:
        raise DefinitionInvalid("a schedule bills at least one line")
    if len(lines) > MAX_LINES:
        raise DefinitionInvalid(f"a schedule has at most {MAX_LINES} lines")
    for line in lines:
        if not line.description.strip():
            raise DefinitionInvalid("every line has a description")
        if line.quantity == 0:
            raise DefinitionInvalid("a line's quantity is not zero")
        if not Decimal(0) <= line.discount_percent <= Decimal(100):
            raise DefinitionInvalid("a discount is between 0 and 100 percent")
        if isinstance(line.quantity, float) or isinstance(line.unit_price, float):
            raise DefinitionInvalid("amounts are decimals, never floats (NFR-031)")
        if not line.vat_treatment.strip():
            raise DefinitionInvalid("every line has a VAT treatment")

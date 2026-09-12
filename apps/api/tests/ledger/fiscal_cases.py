"""Fiscal years and the periods they derive to, as data (FR-ONB-006).

    FR-ONB-006  Fiscal year definition, including non-calendar and short first
                years.

Each case is a span and a scheme, plus the periods it must produce. Written as
a table because it is executed twice: against `derive_periods` in
api/ledger/fiscal.py, and against `ledger.derive_fiscal_periods` in migration
0029. Two implementations exist because the database has to derive periods
inside the transaction that creates the year, and an onboarding screen has to
show them before anything is written - neither can borrow the other's answer,
so the answers are compared instead.

--- What the interesting cases are ---

Not the calendar year. That one works in any implementation, including a wrong
one that assumes January. The cases that separate a correct derivation from a
plausible one are all at the edges:

    a year that does not start in January          (non-calendar)
    a year that does not start on the 1st          (short first year)
    a year that does not end on a month end        (a stub at both ends)
    a year starting on the LAST day of a month     (a one-day period)
    a quarterly year straddling calendar quarters  (five periods, not four)
    February in a leap year                        (29 days, not 28)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from api.ledger.fiscal import PeriodScheme


@dataclass(frozen=True, slots=True)
class FiscalYearCase:
    name: str
    start_date: date
    end_date: date
    scheme: PeriodScheme
    #: (period_number, start, end) for every period, in order. Spelled out
    #: rather than computed - a case table that derives its own expectation
    #: tests nothing.
    expected: tuple[tuple[int, date, date], ...]


def _months(year: int) -> tuple[tuple[int, date, date], ...]:
    """The twelve months of a calendar year, for the one case where writing
    them out adds nothing.
    """
    ends = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return tuple((n, date(year, n, 1), date(year, n, ends[n - 1])) for n in range(1, 13))


FISCAL_YEAR_CASES: tuple[FiscalYearCase, ...] = (
    FiscalYearCase(
        name="a calendar year, monthly",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        scheme=PeriodScheme.MONTHLY,
        expected=_months(2026),
    ),
    FiscalYearCase(
        name="a leap year has a 29-day February",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        scheme=PeriodScheme.MONTHLY,
        expected=_months(2024),
    ),
    FiscalYearCase(
        # FR-ONB-006's "non-calendar": the periods follow the year, not the
        # calendar's idea of where a year starts.
        name="a July-June year, monthly",
        start_date=date(2026, 7, 1),
        end_date=date(2027, 6, 30),
        scheme=PeriodScheme.MONTHLY,
        expected=(
            (1, date(2026, 7, 1), date(2026, 7, 31)),
            (2, date(2026, 8, 1), date(2026, 8, 31)),
            (3, date(2026, 9, 1), date(2026, 9, 30)),
            (4, date(2026, 10, 1), date(2026, 10, 31)),
            (5, date(2026, 11, 1), date(2026, 11, 30)),
            (6, date(2026, 12, 1), date(2026, 12, 31)),
            (7, date(2027, 1, 1), date(2027, 1, 31)),
            (8, date(2027, 2, 1), date(2027, 2, 28)),
            (9, date(2027, 3, 1), date(2027, 3, 31)),
            (10, date(2027, 4, 1), date(2027, 4, 30)),
            (11, date(2027, 5, 1), date(2027, 5, 31)),
            (12, date(2027, 6, 1), date(2027, 6, 30)),
        ),
    ),
    FiscalYearCase(
        # FR-ONB-006's "short first year": a company incorporated on 15 March
        # whose first book year ends with the calendar year. Ten periods, the
        # first of them seventeen days.
        name="a short first year, incorporated mid-March",
        start_date=date(2026, 3, 15),
        end_date=date(2026, 12, 31),
        scheme=PeriodScheme.MONTHLY,
        expected=(
            (1, date(2026, 3, 15), date(2026, 3, 31)),
            (2, date(2026, 4, 1), date(2026, 4, 30)),
            (3, date(2026, 5, 1), date(2026, 5, 31)),
            (4, date(2026, 6, 1), date(2026, 6, 30)),
            (5, date(2026, 7, 1), date(2026, 7, 31)),
            (6, date(2026, 8, 1), date(2026, 8, 31)),
            (7, date(2026, 9, 1), date(2026, 9, 30)),
            (8, date(2026, 10, 1), date(2026, 10, 31)),
            (9, date(2026, 11, 1), date(2026, 11, 30)),
            (10, date(2026, 12, 1), date(2026, 12, 31)),
        ),
    ),
    FiscalYearCase(
        # A stub at BOTH ends, which is where an implementation that only
        # truncates the first period gets it wrong.
        name="a short year ending mid-month",
        start_date=date(2026, 3, 15),
        end_date=date(2026, 6, 15),
        scheme=PeriodScheme.MONTHLY,
        expected=(
            (1, date(2026, 3, 15), date(2026, 3, 31)),
            (2, date(2026, 4, 1), date(2026, 4, 30)),
            (3, date(2026, 5, 1), date(2026, 5, 31)),
            (4, date(2026, 6, 1), date(2026, 6, 15)),
        ),
    ),
    FiscalYearCase(
        # The case that needed migration 0029 to relax period's date check:
        # the first period is a single day.
        name="a year starting on the last day of a month",
        start_date=date(2026, 3, 31),
        end_date=date(2026, 5, 31),
        scheme=PeriodScheme.MONTHLY,
        expected=(
            (1, date(2026, 3, 31), date(2026, 3, 31)),
            (2, date(2026, 4, 1), date(2026, 4, 30)),
            (3, date(2026, 5, 1), date(2026, 5, 31)),
        ),
    ),
    FiscalYearCase(
        name="a year shorter than one month",
        start_date=date(2026, 3, 10),
        end_date=date(2026, 3, 20),
        scheme=PeriodScheme.MONTHLY,
        expected=((1, date(2026, 3, 10), date(2026, 3, 20)),),
    ),
    FiscalYearCase(
        name="a calendar year, quarterly",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        scheme=PeriodScheme.QUARTERLY,
        expected=(
            (1, date(2026, 1, 1), date(2026, 3, 31)),
            (2, date(2026, 4, 1), date(2026, 6, 30)),
            (3, date(2026, 7, 1), date(2026, 9, 30)),
            (4, date(2026, 10, 1), date(2026, 12, 31)),
        ),
    ),
    FiscalYearCase(
        # FIVE periods, not four, and that is the point. Quarters are CALENDAR
        # quarters because a period is also the unit a VAT return is filed for
        # (0021, CMP-014), and Dutch returns are filed on calendar quarters
        # whatever the fiscal year does. Fiscal quarters counted from May would
        # give four tidy periods that could not be filed as anything.
        name="a quarterly year starting in May straddles calendar quarters",
        start_date=date(2026, 5, 1),
        end_date=date(2027, 4, 30),
        scheme=PeriodScheme.QUARTERLY,
        expected=(
            (1, date(2026, 5, 1), date(2026, 6, 30)),
            (2, date(2026, 7, 1), date(2026, 9, 30)),
            (3, date(2026, 10, 1), date(2026, 12, 31)),
            (4, date(2027, 1, 1), date(2027, 3, 31)),
            (5, date(2027, 4, 1), date(2027, 4, 30)),
        ),
    ),
    FiscalYearCase(
        # A quarterly year aligned to the calendar quarters but not to January.
        name="a quarterly year starting in July aligns exactly",
        start_date=date(2026, 7, 1),
        end_date=date(2027, 6, 30),
        scheme=PeriodScheme.QUARTERLY,
        expected=(
            (1, date(2026, 7, 1), date(2026, 9, 30)),
            (2, date(2026, 10, 1), date(2026, 12, 31)),
            (3, date(2027, 1, 1), date(2027, 3, 31)),
            (4, date(2027, 4, 1), date(2027, 6, 30)),
        ),
    ),
    FiscalYearCase(
        # NL permits a long first book year of up to about two years. Same
        # walk, more periods.
        name="a long first book year",
        start_date=date(2026, 11, 1),
        end_date=date(2027, 12, 31),
        scheme=PeriodScheme.QUARTERLY,
        expected=(
            (1, date(2026, 11, 1), date(2026, 12, 31)),
            (2, date(2027, 1, 1), date(2027, 3, 31)),
            (3, date(2027, 4, 1), date(2027, 6, 30)),
            (4, date(2027, 7, 1), date(2027, 9, 30)),
            (5, date(2027, 10, 1), date(2027, 12, 31)),
        ),
    ),
)


#: Spans a fiscal year may not have. The message substring has to appear in
#: both the Python refusal and the SQL one, so a case cannot pass against one
#: implementation for a reason the other does not share.
INVALID_YEARS: tuple[tuple[str, date, date, str], ...] = (
    (
        "a year that ends before it starts",
        date(2026, 12, 31),
        date(2026, 1, 1),
        "ends after it starts",
    ),
    (
        "a year that starts and ends on the same day",
        date(2026, 1, 1),
        date(2026, 1, 1),
        "ends after it starts",
    ),
    (
        "a year longer than the law allows",
        date(2026, 1, 1),
        date(2029, 1, 1),
        "longer than 24 months",
    ),
)

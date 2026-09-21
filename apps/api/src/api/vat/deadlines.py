"""When the next BTW return is due (CMP-014).

Pure date rules with no database, so the rule is written down in one place and
tested as a table.

--- The rule, and what it assumes ---

A Dutch BTW return (aangifte omzetbelasting) for a period is due by the END OF
THE MONTH AFTER the period ends: the third quarter (1 July to 30 September) is
due on 31 October; a monthly period ending 31 August is due on 30 September.
No adjustment is made for a weekend or a public holiday: the date returned is
the one a reader would work out from the rule, and the UI shows it as a date,
not as a promise of what the Belastingdienst will accept.

Two assumptions are recorded here rather than buried, because both were
decided, not discovered:

  1. The period is the fiscal year's own period scheme (monthly or quarterly).
     The product has no separate "how often this business files" setting yet.
     A business that books monthly but files quarterly would be shown the wrong
     period; that setting, when it exists, replaces `scheme` at the call site
     and nothing in this module changes.
  2. The rule above is the one the owner confirmed for the businesses the
     product serves. It is not a general statement of Dutch tax law: an annual
     filer, a business with an extension, or a return with a different due
     date is outside it.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Literal

PeriodScheme = Literal["monthly", "quarterly"]


@dataclass(frozen=True, slots=True)
class ReturnPeriod:
    start: date
    end: date
    due: date


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def return_period(*, on: date, scheme: PeriodScheme) -> ReturnPeriod:
    """The filing period that contains `on`, and when its return is due.

    "Next return" is the period still open on `on`: it is the one whose return
    will be due next, because the previous period's return has either been
    filed or is already late and is not this function's business.
    """
    if scheme == "monthly":
        first_month = on.month
        last_month = on.month
    elif scheme == "quarterly":
        first_month = 3 * ((on.month - 1) // 3) + 1
        last_month = first_month + 2
    else:  # pragma: no cover - the type says so; a bad value must not pass silently
        raise ValueError(f"unknown period scheme: {scheme!r}")

    due_year, due_month = _add_months(on.year, last_month, 1)
    return ReturnPeriod(
        start=date(on.year, first_month, 1),
        end=_last_day(on.year, last_month),
        due=_last_day(due_year, due_month),
    )

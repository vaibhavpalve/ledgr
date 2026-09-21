"""The BTW due-date rule (CMP-014), as a table of worked examples."""

from __future__ import annotations

from datetime import date

import pytest

from api.vat.deadlines import return_period


@pytest.mark.parametrize(
    ("on", "scheme", "start", "end", "due"),
    [
        # Quarterly: due the last day of the month after the quarter.
        (date(2026, 9, 21), "quarterly", date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 31)),
        (date(2026, 7, 1), "quarterly", date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 31)),
        (date(2026, 9, 30), "quarterly", date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 31)),
        (date(2026, 2, 10), "quarterly", date(2026, 1, 1), date(2026, 3, 31), date(2026, 4, 30)),
        # The last quarter is due in January of the next year.
        (date(2026, 11, 5), "quarterly", date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 31)),
        # Monthly.
        (date(2026, 8, 15), "monthly", date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 30)),
        (date(2026, 12, 3), "monthly", date(2026, 12, 1), date(2026, 12, 31), date(2027, 1, 31)),
        # Month lengths, including a leap year.
        (date(2028, 1, 9), "monthly", date(2028, 1, 1), date(2028, 1, 31), date(2028, 2, 29)),
        (date(2027, 1, 9), "monthly", date(2027, 1, 1), date(2027, 1, 31), date(2027, 2, 28)),
    ],
)
def test_the_return_is_due_at_the_end_of_the_month_after_the_period(
    on: date, scheme: str, start: date, end: date, due: date
) -> None:
    period = return_period(on=on, scheme=scheme)  # type: ignore[arg-type]

    assert (period.start, period.end, period.due) == (start, end, due)


def test_a_weekend_due_date_is_not_moved() -> None:
    # 31 Oct 2026 is a Saturday. The rule states the date; it does not adjust it.
    assert return_period(on=date(2026, 9, 21), scheme="quarterly").due == date(2026, 10, 31)

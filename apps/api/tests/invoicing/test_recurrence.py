"""SI-07's rules - api.invoicing.recurrence (ADR-074).

The dates and prices are worked out by hand in the comments. The properties that
matter most: a month-end anchor never drifts, catching up dates each invoice in its
own month, and indexation compounds once per COMPLETED year.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.invoicing.recurrence import (
    INTERVALS,
    MAX_LINES,
    MAX_RUNS_PER_CALL,
    DefinitionInvalid,
    RecurringLine,
    Schedule,
    add_months,
    due_runs,
    indexed_price,
    next_run,
    occurrence,
    validate_definition,
    years_elapsed,
)

D = Decimal


def sched(start: date = date(2026, 1, 15), interval: int = 1, **kw: object) -> Schedule:
    return Schedule(start_date=start, interval_months=interval, **kw)  # type: ignore[arg-type]


def line(**kw: object) -> RecurringLine:
    defaults: dict[str, object] = dict(
        description="Onderhoudscontract",
        quantity=D("1"),
        unit_price=D("100"),
        vat_treatment="btw_21",
    )
    defaults.update(kw)
    return RecurringLine(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# add_months - the clamp
# ===========================================================================


@pytest.mark.parametrize(
    ("anchor", "months", "expected"),
    [
        (date(2026, 1, 15), 1, date(2026, 2, 15)),
        (date(2026, 1, 31), 1, date(2026, 2, 28)),  # no 31 February: clamp, not error
        (date(2024, 1, 31), 1, date(2024, 2, 29)),  # leap year
        (date(2026, 1, 31), 2, date(2026, 3, 31)),  # the day is back when it exists
        (date(2026, 1, 31), 3, date(2026, 4, 30)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),  # crosses a year
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
        (date(2026, 3, 31), -1, date(2026, 2, 28)),
        (date(2024, 2, 29), 12, date(2025, 2, 28)),  # a leap-day anchor, a common year
        (date(2024, 2, 29), 48, date(2028, 2, 29)),
    ],
)
def test_add_months(anchor: date, months: int, expected: date) -> None:
    assert add_months(anchor, months) == expected


# ===========================================================================
# occurrence - anchored, never chained
# ===========================================================================


def test_a_month_end_anchor_does_not_drift() -> None:
    """The reason dates are derived from the anchor: chained from the previous
    date, 31 Jan would go 28 Feb, 28 Mar, 28 Apr forever."""
    schedule = sched(start=date(2026, 1, 31))
    assert [occurrence(schedule, n) for n in range(5)] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),  # back on the 31st
        date(2026, 4, 30),
        date(2026, 5, 31),
    ]


@pytest.mark.parametrize(
    ("interval", "third"),
    [
        (1, date(2026, 3, 15)),
        (2, date(2026, 5, 15)),
        (3, date(2026, 7, 15)),
        (6, date(2027, 1, 15)),
        (12, date(2028, 1, 15)),
    ],
)
def test_the_interval_sets_the_spacing(interval: int, third: date) -> None:
    assert occurrence(sched(interval=interval), 2) == third


def test_the_first_run_is_the_start_date() -> None:
    assert occurrence(sched(start=date(2026, 3, 9)), 0) == date(2026, 3, 9)


def test_a_negative_index_is_refused() -> None:
    with pytest.raises(ValueError):
        occurrence(sched(), -1)


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2060, 12, 31)),
    interval=st.sampled_from(INTERVALS),
    index=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=200)
def test_occurrences_strictly_increase_and_keep_the_month_arithmetic(
    start: date, interval: int, index: int
) -> None:
    schedule = sched(start=start, interval=interval)
    now, later = occurrence(schedule, index), occurrence(schedule, index + 1)

    assert later > now
    months = (later.year * 12 + later.month) - (now.year * 12 + now.month)
    assert months == interval
    # The day is the anchor's day, or the month's last day when that does not exist.
    assert later.day == min(start.day, calendar.monthrange(later.year, later.month)[1])


# ===========================================================================
# end date and max runs
# ===========================================================================


def test_an_open_ended_schedule_always_has_a_next_run() -> None:
    assert next_run(sched(), 500) is not None


def test_the_end_date_is_inclusive() -> None:
    schedule = sched(start=date(2026, 1, 15), end_date=date(2026, 3, 15))
    assert next_run(schedule, 2) == date(2026, 3, 15)  # exactly on the end date: runs
    assert next_run(schedule, 3) is None  # 15 April is past it


def test_an_end_date_between_two_runs_stops_at_the_earlier() -> None:
    schedule = sched(start=date(2026, 1, 15), end_date=date(2026, 3, 14))
    assert next_run(schedule, 2) is None  # 15 March would be a day past the end


def test_max_runs_ends_the_schedule() -> None:
    schedule = sched(max_runs=3)
    assert [next_run(schedule, n) is not None for n in range(5)] == [True, True, True, False, False]


def test_whichever_limit_comes_first_wins() -> None:
    by_count = sched(start=date(2026, 1, 15), max_runs=2, end_date=date(2030, 1, 1))
    by_date = sched(start=date(2026, 1, 15), max_runs=99, end_date=date(2026, 2, 20))

    assert next_run(by_count, 2) is None
    assert next_run(by_date, 1) == date(2026, 2, 15)
    assert next_run(by_date, 2) is None


# ===========================================================================
# due_runs - catching up
# ===========================================================================


def test_nothing_is_due_before_the_start_date() -> None:
    assert due_runs(sched(start=date(2026, 6, 1)), 0, date(2026, 5, 31)) == []


def test_the_run_on_its_own_date_is_due() -> None:
    assert due_runs(sched(start=date(2026, 6, 1)), 0, date(2026, 6, 1)) == [(0, date(2026, 6, 1))]


def test_catching_up_dates_each_invoice_in_its_own_month() -> None:
    """Three months unrun: three invoices, dated 15 Jan, 15 Feb, 15 Mar - not one
    dated today."""
    due = due_runs(sched(start=date(2026, 1, 15)), 0, date(2026, 3, 20))
    assert due == [(0, date(2026, 1, 15)), (1, date(2026, 2, 15)), (2, date(2026, 3, 15))]


def test_only_runs_not_yet_generated_are_due() -> None:
    due = due_runs(sched(start=date(2026, 1, 15)), 2, date(2026, 5, 1))
    assert due == [(2, date(2026, 3, 15)), (3, date(2026, 4, 15))]


def test_catching_up_is_capped_and_the_rest_wait() -> None:
    long_unrun = due_runs(sched(start=date(2020, 1, 1)), 0, date(2026, 1, 1))
    assert len(long_unrun) == MAX_RUNS_PER_CALL
    assert long_unrun[0][0] == 0 and long_unrun[-1][0] == MAX_RUNS_PER_CALL - 1


def test_a_finished_schedule_has_nothing_due() -> None:
    assert due_runs(sched(max_runs=2), 2, date(2030, 1, 1)) == []


def test_due_runs_stop_at_the_end_date() -> None:
    schedule = sched(start=date(2026, 1, 15), end_date=date(2026, 2, 28))
    assert [d for _, d in due_runs(schedule, 0, date(2026, 12, 31))] == [
        date(2026, 1, 15),
        date(2026, 2, 15),
    ]


@given(
    start=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
    interval=st.sampled_from(INTERVALS),
    done=st.integers(min_value=0, max_value=30),
    days_ahead=st.integers(min_value=-100, max_value=2000),
    cap=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=150)
def test_due_runs_are_contiguous_ordered_and_never_in_the_future(
    start: date, interval: int, done: int, days_ahead: int, cap: int
) -> None:
    schedule = sched(start=start, interval=interval)
    today = start + timedelta(days=days_ahead)
    due = due_runs(schedule, done, today, cap=cap)

    assert len(due) <= cap
    assert [i for i, _ in due] == list(range(done, done + len(due)))  # no gaps
    assert all(on <= today for _, on in due)
    assert [on for _, on in due] == sorted(on for _, on in due)


# ===========================================================================
# indexation
# ===========================================================================


@pytest.mark.parametrize(
    ("on", "years"),
    [
        (date(2026, 1, 15), 0),  # the start itself
        (date(2026, 12, 31), 0),  # nearly a year: not yet
        (date(2027, 1, 14), 0),  # the day BEFORE the anniversary
        (date(2027, 1, 15), 1),  # the anniversary itself
        (date(2028, 1, 14), 1),
        (date(2028, 1, 15), 2),
        (date(2025, 1, 1), 0),  # before the start: never negative
    ],
)
def test_years_elapsed(on: date, years: int) -> None:
    assert years_elapsed(date(2026, 1, 15), on) == years


def test_no_indexation_before_the_first_anniversary() -> None:
    assert indexed_price(D("100"), D("3.5"), date(2026, 1, 15), date(2027, 1, 14)) == D("100.0000")


def test_the_first_anniversary_applies_one_step() -> None:
    # 100 x 1.035 = 103.50
    assert indexed_price(D("100"), D("3.5"), date(2026, 1, 15), date(2027, 1, 15)) == D("103.5000")


def test_indexation_compounds() -> None:
    # 100 x 1.035^2 = 107.1225 ; ^3 = 110.871... -> 110.8718 (four decimals, half up)
    start = date(2026, 1, 15)
    assert indexed_price(D("100"), D("3.5"), start, date(2028, 1, 15)) == D("107.1225")
    assert indexed_price(D("100"), D("3.5"), start, date(2029, 1, 15)) == D("110.8718")


def test_zero_indexation_never_changes_the_price() -> None:
    assert indexed_price(D("49.95"), D("0"), date(2020, 1, 1), date(2035, 1, 1)) == D("49.9500")


def test_rounding_happens_once_at_the_end_not_between_years() -> None:
    """Rounding each year's result would drift from compounding then rounding: 10 at
    3.3333% for 3 years is 10.9... either way, so use a case where they differ."""
    unrounded = D("10") * (D(1) + D("33.333") / D(100)) ** 3
    assert indexed_price(
        D("10"), D("33.333"), date(2020, 1, 1), date(2023, 1, 1)
    ) == unrounded.quantize(D("0.0001"))


def test_a_price_keeps_its_four_decimals() -> None:
    """A unit price of EUR 0.0350 is ordinary (0037): it is not rounded to the cent."""
    assert indexed_price(D("0.0350"), D("0"), date(2026, 1, 1), date(2026, 6, 1)) == D("0.0350")


def test_a_float_is_refused() -> None:
    with pytest.raises(TypeError):
        indexed_price(100.0, D("3"), date(2026, 1, 1), date(2027, 1, 1))  # type: ignore[arg-type]


@given(
    base=st.decimals(min_value="0.0001", max_value="100000", places=4),
    percent=st.decimals(min_value="0", max_value="100", places=3),
    years=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=150)
def test_indexation_never_lowers_a_price_and_only_moves_on_an_anniversary(
    base: Decimal, percent: Decimal, years: int
) -> None:
    start = date(2020, 3, 1)
    price = indexed_price(base, percent, start, date(2020 + years, 3, 1))
    assert price >= base.quantize(D("0.0001"))
    # The day before the first anniversary is always still the base price.
    assert indexed_price(base, percent, start, date(2021, 2, 28)) == base.quantize(D("0.0001"))


# ===========================================================================
# validate_definition
# ===========================================================================


def check(**overrides: object) -> None:
    args: dict[str, object] = dict(schedule=sched(), due_days=None, lines=[line()])
    args.update(overrides)
    validate_definition(**args)  # type: ignore[arg-type]


def test_a_sound_definition_is_accepted() -> None:
    check()
    check(schedule=sched(end_date=date(2027, 1, 1), max_runs=5, indexation_percent=D("3.5")))
    check(due_days=0)
    check(due_days=365)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"schedule": sched(interval=5)}, "interval"),
        ({"schedule": sched(interval=0)}, "interval"),
        ({"schedule": sched(end_date=date(2025, 1, 1))}, "end date"),
        ({"schedule": sched(max_runs=0)}, "at least once"),
        ({"schedule": sched(indexation_percent=D("-1"))}, "indexation"),
        ({"schedule": sched(indexation_percent=D("101"))}, "indexation"),
        ({"due_days": -1}, "payment term"),
        ({"due_days": 366}, "payment term"),
        ({"lines": []}, "at least one line"),
        ({"lines": [line(description="   ")]}, "description"),
        ({"lines": [line(quantity=D("0"))]}, "quantity"),
        ({"lines": [line(discount_percent=D("101"))]}, "discount"),
        ({"lines": [line(discount_percent=D("-1"))]}, "discount"),
        ({"lines": [line(vat_treatment=" ")]}, "VAT treatment"),
        ({"lines": [line() for _ in range(MAX_LINES + 1)]}, "at most"),
    ],
)
def test_an_unrunnable_definition_is_refused(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(DefinitionInvalid, match=match):
        check(**overrides)


def test_a_negative_price_is_allowed_for_a_standing_discount_line() -> None:
    check(lines=[line(), line(description="Vaste korting", unit_price=D("-10"))])

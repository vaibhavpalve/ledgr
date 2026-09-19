"""SI-04's rules - api.invoicing.dunning (ADR-071).

The expected figures are worked out by hand in the comments, not read back from
the code under test: a test that recomputes the formula proves only that the code
agrees with itself.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.invoicing.dunning import (
    DEFAULT_LADDER,
    FORMAL_NOTICE_DEADLINE_DAYS,
    MAX_STEPS,
    MIN_DAYS_BETWEEN_REMINDERS,
    WIK_MAXIMUM,
    WIK_MINIMUM,
    Blocker,
    InterestRate,
    InterestRateKind,
    LadderInvalid,
    LadderStep,
    NoInterestRate,
    StepKind,
    assess,
    collection_cost,
    days_overdue,
    next_step,
    statutory_interest,
    validate_ladder,
)

DUE = date(2026, 1, 1)
INVOICE = uuid.uuid4()
D = Decimal


def rates(*pairs: tuple[date, str]) -> list[InterestRate]:
    return [InterestRate(valid_from=on, rate=D(rate)) for on, rate in pairs]


RATE_10 = rates((date(2025, 1, 1), "10"))


# ===========================================================================
# days overdue
# ===========================================================================


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2025, 12, 31), 0),  # before the due date
        (DUE, 0),  # the due date itself is not overdue
        (DUE + timedelta(days=1), 1),
        (DUE + timedelta(days=30), 30),
    ],
)
def test_days_overdue(today: date, expected: int) -> None:
    assert days_overdue(DUE, today) == expected


def test_no_due_date_is_never_overdue() -> None:
    assert days_overdue(None, date(2030, 1, 1)) == 0


# ===========================================================================
# the ladder
# ===========================================================================


def test_the_default_ladder_is_valid() -> None:
    assert validate_ladder(DEFAULT_LADDER) == DEFAULT_LADDER


def test_an_empty_ladder_is_valid_and_means_no_chasing() -> None:
    assert validate_ladder(()) == ()


def test_the_ladder_is_returned_in_position_order() -> None:
    shuffled = (DEFAULT_LADDER[2], DEFAULT_LADDER[0], DEFAULT_LADDER[1])
    assert validate_ladder(shuffled) == DEFAULT_LADDER


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        (
            (LadderStep(1, 7, StepKind.FRIENDLY), LadderStep(3, 21, StepKind.REMINDER)),
            "no gaps",
        ),
        ((LadderStep(1, 0, StepKind.FRIENDLY),), "at least one day"),
        (
            (LadderStep(1, 10, StepKind.FRIENDLY), LadderStep(2, 10, StepKind.REMINDER)),
            "later than the one before",
        ),
        (
            (LadderStep(1, 10, StepKind.FRIENDLY), LadderStep(2, 5, StepKind.REMINDER)),
            "later than the one before",
        ),
        (
            (LadderStep(1, 7, StepKind.REMINDER, charge_collection_cost=True),),
            "only a formal notice",
        ),
        (
            (
                LadderStep(1, 7, StepKind.FORMAL_NOTICE, charge_collection_cost=True),
                LadderStep(2, 14, StepKind.FORMAL_NOTICE, charge_collection_cost=True),
            ),
            "once|at most one",
        ),
        (
            (
                LadderStep(1, 7, StepKind.FORMAL_NOTICE),
                LadderStep(2, 14, StepKind.REMINDER),
            ),
            "last step",
        ),
    ],
)
def test_an_unfollowable_or_overreaching_ladder_is_refused(
    steps: tuple[LadderStep, ...], match: str
) -> None:
    with pytest.raises(LadderInvalid, match=match):
        validate_ladder(steps)


def test_a_ladder_has_a_maximum_length() -> None:
    too_many = tuple(LadderStep(i, i * 3, StepKind.REMINDER) for i in range(1, MAX_STEPS + 2))
    with pytest.raises(LadderInvalid, match="at most"):
        validate_ladder(too_many)


# ===========================================================================
# escalation is a sequence
# ===========================================================================


def test_the_first_step_is_due_once_the_invoice_is_that_overdue() -> None:
    step, why = next_step(DEFAULT_LADDER, frozenset(), 7)
    assert (step, why) == (DEFAULT_LADDER[0], None)


def test_before_that_the_step_is_named_but_not_due() -> None:
    step, why = next_step(DEFAULT_LADDER, frozenset(), 6)
    assert (step, why) == (DEFAULT_LADDER[0], Blocker.STEP_NOT_DUE)


def test_an_invoice_far_overdue_still_gets_step_one_first() -> None:
    """A customer's first contact about a debt is never a demand with costs."""
    step, why = next_step(DEFAULT_LADDER, frozenset(), 90)
    assert (step, why) == (DEFAULT_LADDER[0], None)
    assert step is not None and step.kind is StepKind.FRIENDLY


def test_the_next_step_is_the_lowest_one_not_yet_sent() -> None:
    step, why = next_step(DEFAULT_LADDER, frozenset({1}), 30)
    assert (step, why) == (DEFAULT_LADDER[1], None)


def test_a_step_sent_out_of_order_is_skipped_not_repeated() -> None:
    step, _ = next_step(DEFAULT_LADDER, frozenset({1, 3}), 90)
    assert step == DEFAULT_LADDER[1]


def test_a_finished_ladder_says_so() -> None:
    step, why = next_step(DEFAULT_LADDER, frozenset({1, 2, 3}), 400)
    assert (step, why) == (None, Blocker.LADDER_COMPLETE)


def test_an_empty_ladder_is_complete_at_once() -> None:
    assert next_step((), frozenset(), 100) == (None, Blocker.LADDER_COMPLETE)


# ===========================================================================
# statutory interest - by hand
# ===========================================================================


def test_interest_for_thirty_days_at_ten_percent() -> None:
    # 1000 x 10% x 30 / 365 = 8.2191... -> 8.22. Days: 2 Jan .. 31 Jan = 30.
    assert statutory_interest(D("1000"), DUE, date(2026, 1, 31), RATE_10) == D("8.22")


def test_interest_starts_the_day_after_the_due_date() -> None:
    """One day overdue is one day of interest, not two and not zero."""
    # 3650 x 10% x 1 / 365 = 1.00 exactly.
    assert statutory_interest(D("3650"), DUE, DUE + timedelta(days=1), RATE_10) == D("1.00")


def test_no_interest_on_the_due_date_itself() -> None:
    assert statutory_interest(D("1000"), DUE, DUE, RATE_10) == D("0.00")


def test_a_rate_change_splits_the_period_and_prices_each_part() -> None:
    # 2 Jan..15 Jan = 14 days at 10%; 16 Jan..31 Jan = 16 days at 8%.
    # 1000 x .10 x 14/365 = 3.8356..;  1000 x .08 x 16/365 = 3.5068..;  sum 7.3425 -> 7.34.
    split = rates((date(2025, 1, 1), "10"), (date(2026, 1, 16), "8"))
    assert statutory_interest(D("1000"), DUE, date(2026, 1, 31), split) == D("7.34")


def test_a_rate_that_starts_after_the_period_is_ignored() -> None:
    later = rates((date(2025, 1, 1), "10"), (date(2027, 1, 1), "50"))
    assert statutory_interest(D("1000"), DUE, date(2026, 1, 31), later) == D("8.22")


def test_the_rate_in_force_on_the_first_day_is_used_even_if_it_started_long_ago() -> None:
    assert statutory_interest(D("3650"), DUE, DUE + timedelta(days=1), RATE_10) == D("1.00")


def test_no_rate_loaded_is_refused_not_treated_as_zero() -> None:
    with pytest.raises(NoInterestRate):
        statutory_interest(D("1000"), DUE, date(2026, 1, 31), [])


def test_a_rate_that_starts_after_interest_does_is_a_gap_not_a_zero() -> None:
    late = rates((date(2026, 1, 20), "10"))
    with pytest.raises(NoInterestRate):
        statutory_interest(D("1000"), DUE, date(2026, 1, 31), late)


def test_interest_refuses_a_float() -> None:
    with pytest.raises(TypeError):
        statutory_interest(1000.0, DUE, date(2026, 1, 31), RATE_10)  # type: ignore[arg-type]


def test_no_principal_no_interest() -> None:
    assert statutory_interest(D("0"), DUE, date(2026, 1, 31), RATE_10) == D("0.00")


@given(
    principal=st.decimals(min_value="0.01", max_value="1000000", places=2),
    days=st.integers(min_value=1, max_value=1500),
    rate=st.decimals(min_value="0.001", max_value="30", places=3),
)
@settings(max_examples=150)
def test_interest_never_goes_down_as_time_passes(
    principal: Decimal, days: int, rate: Decimal
) -> None:
    one_rate = [InterestRate(date(2020, 1, 1), rate)]
    earlier = statutory_interest(principal, DUE, DUE + timedelta(days=days), one_rate)
    later = statutory_interest(principal, DUE, DUE + timedelta(days=days + 1), one_rate)
    assert later >= earlier >= 0


@given(
    split=st.integers(min_value=1, max_value=200),
    total=st.integers(min_value=201, max_value=400),
    principal=st.decimals(min_value="1", max_value="100000", places=2),
)
@settings(max_examples=100)
def test_splitting_at_an_unchanged_rate_changes_nothing(
    split: int, total: int, principal: Decimal
) -> None:
    """A "rate change" to the same rate must not alter the answer: the total is
    rounded once, so it cannot depend on how many slices the period was cut into."""
    plain = rates((date(2020, 1, 1), "10"))
    cut = rates((date(2020, 1, 1), "10"), (DUE + timedelta(days=split), "10"))
    through = DUE + timedelta(days=total)
    assert statutory_interest(principal, DUE, through, plain) == statutory_interest(
        principal, DUE, through, cut
    )


# ===========================================================================
# collection cost (WIK) - by hand
# ===========================================================================


@pytest.mark.parametrize(
    ("principal", "expected", "why"),
    [
        ("0", "0.00", "no debt, no cost"),
        ("100", "40.00", "15% is 15.00, lifted to the 40.00 minimum"),
        ("266.67", "40.00", "15% is 40.0005, rounds to the minimum"),
        ("1000", "150.00", "15% of 1000"),
        ("2500", "375.00", "15% of the whole first tier"),
        ("5000", "625.00", "375 + 10% of the next 2500 = 250"),
        ("10000", "875.00", "625 + 5% of the next 5000 = 250"),
        ("200000", "2775.00", "875 + 1% of the next 190000 = 1900"),
        ("1000000", "6775.00", "2775 + 0.5% of 800000 = 4000 -> 6775 exactly at the cap"),
        ("2000000", "6775.00", "2775 + 9000 = 11775, capped"),
    ],
)
def test_collection_cost_by_tier(principal: str, expected: str, why: str) -> None:
    assert collection_cost(D(principal)) == D(expected), why


def test_collection_cost_is_bounded_by_the_minimum_and_the_maximum() -> None:
    assert collection_cost(D("0.01")) == WIK_MINIMUM
    assert collection_cost(D("999999999")) == WIK_MAXIMUM


def test_collection_cost_refuses_a_float() -> None:
    with pytest.raises(TypeError):
        collection_cost(100.0)  # type: ignore[arg-type]


@given(principal=st.decimals(min_value="0.01", max_value="5000000", places=2))
@settings(max_examples=200)
def test_collection_cost_is_within_bounds_and_monotonic(principal: Decimal) -> None:
    cost = collection_cost(principal)
    assert WIK_MINIMUM <= cost <= WIK_MAXIMUM
    assert collection_cost(principal + D("0.01")) >= cost


# ===========================================================================
# the assessment
# ===========================================================================


def _assess(
    *,
    outstanding: str = "1000.00",
    due: date | None = DUE,
    days: int = 40,
    sent: frozenset[int] = frozenset(),
    paused: bool = False,
    business: bool = True,
    rate_table: list[InterestRate] | None = None,
    steps: tuple[LadderStep, ...] = DEFAULT_LADDER,
    last_sent_days_ago: int | None = None,
):  # type: ignore[no-untyped-def]
    return assess(
        invoice_id=INVOICE,
        outstanding=D(outstanding),
        due_date=due,
        today=DUE + timedelta(days=days),
        steps=steps,
        sent_positions=sent,
        paused=paused,
        is_business=business,
        rates=RATE_10 if rate_table is None else rate_table,
        last_sent_on=(
            None
            if last_sent_days_ago is None
            else _today(days) - timedelta(days=last_sent_days_ago)
        ),
    )


def test_a_sendable_first_step_carries_no_claim() -> None:
    result = _assess(days=10)

    assert result.can_send
    assert result.step == DEFAULT_LADDER[0]
    assert result.interest is None
    assert result.collection_cost is None
    assert result.pay_by is None


def test_the_formal_notice_claims_interest_costs_and_a_deadline() -> None:
    result = _assess(days=36, sent=frozenset({1, 2}))

    assert result.can_send
    assert result.step is not None and result.step.kind is StepKind.FORMAL_NOTICE
    # 1000 x 10% x 36 / 365 = 9.8630.. -> 9.86
    assert result.interest == D("9.86")
    assert result.collection_cost == D("150.00")
    assert result.pay_by == DUE + timedelta(days=36 + FORMAL_NOTICE_DEADLINE_DAYS)


def test_costs_are_charged_on_the_principal_not_on_principal_plus_interest() -> None:
    result = _assess(outstanding="2500.00", days=36, sent=frozenset({1, 2}))
    assert result.collection_cost == D("375.00")  # 15% of 2500, not of 2509.xx


@pytest.mark.parametrize(
    ("kwargs", "blocker"),
    [
        ({"outstanding": "0.00"}, Blocker.NOTHING_OUTSTANDING),
        ({"outstanding": "-5.00"}, Blocker.NOTHING_OUTSTANDING),
        ({"paused": True}, Blocker.PAUSED),
        ({"due": None}, Blocker.NO_DUE_DATE),
        ({"days": 0}, Blocker.NOT_OVERDUE),
        ({"days": -5}, Blocker.NOT_OVERDUE),
        ({"days": 3}, Blocker.STEP_NOT_DUE),
        ({"days": 400, "sent": frozenset({1, 2, 3})}, Blocker.LADDER_COMPLETE),
    ],
)
def test_the_reasons_nothing_is_sent(kwargs: dict[str, object], blocker: Blocker) -> None:
    result = _assess(**kwargs)  # type: ignore[arg-type]
    assert result.blocker is blocker
    assert not result.can_send


def test_a_paid_invoice_is_never_chased_even_if_paused_or_overdue() -> None:
    """Nothing outstanding outranks every other reason."""
    assert _assess(outstanding="0.00", paused=True).blocker is Blocker.NOTHING_OUTSTANDING


def test_a_pause_beats_an_otherwise_due_step() -> None:
    result = _assess(days=100, paused=True)
    assert result.blocker is Blocker.PAUSED
    assert result.step is None  # nothing was even considered


def test_a_step_not_yet_due_still_names_the_step() -> None:
    result = _assess(days=3)
    assert result.blocker is Blocker.STEP_NOT_DUE
    assert result.step == DEFAULT_LADDER[0]


def test_a_missing_rate_blocks_the_step_that_needs_it() -> None:
    """A formal notice with an invented rate is a demand for money nobody is owed."""
    result = _assess(days=36, sent=frozenset({1, 2}), rate_table=[])

    assert result.blocker is Blocker.INTEREST_RATE_MISSING
    assert not result.can_send
    assert result.step is not None and result.step.kind is StepKind.FORMAL_NOTICE


def test_a_missing_rate_does_not_block_a_step_that_charges_no_interest() -> None:
    result = _assess(days=10, rate_table=[])
    assert result.can_send


def test_a_ladder_that_charges_no_interest_needs_no_rate() -> None:
    no_interest = (
        LadderStep(1, 7, StepKind.FRIENDLY),
        LadderStep(2, 21, StepKind.FORMAL_NOTICE, charge_collection_cost=True),
    )
    result = _assess(days=25, sent=frozenset({1}), rate_table=[], steps=no_interest)

    assert result.can_send
    assert result.interest is None
    assert result.collection_cost == D("150.00")


def test_a_consumer_is_assessed_under_the_consumer_rate_kind() -> None:
    assert _assess(business=False).interest_kind is InterestRateKind.CONSUMER
    assert _assess(business=True).interest_kind is InterestRateKind.COMMERCIAL


# ===========================================================================
# spacing: one reminder is never followed at once by the next
# ===========================================================================


def _today(days: int) -> date:
    return DUE + timedelta(days=days)


def test_the_gap_is_a_week() -> None:
    assert MIN_DAYS_BETWEEN_REMINDERS == 7


def test_a_far_overdue_invoice_cannot_be_escalated_straight_away() -> None:
    """The defect this rule closes: step 1 sent on day 40, and step 2's day (21) has
    long passed - so without a gap, pressing send again would send step 2 at once."""
    result = _assess(days=40, sent=frozenset({1}), last_sent_days_ago=0)

    assert result.blocker is Blocker.TOO_SOON
    assert not result.can_send
    assert result.step == DEFAULT_LADDER[1]  # named, so a screen can say what is next


@pytest.mark.parametrize(
    ("ago", "sendable"),
    [(0, False), (1, False), (6, False), (7, True), (8, True), (30, True)],
)
def test_the_gap_boundary(ago: int, sendable: bool) -> None:
    result = _assess(days=40, sent=frozenset({1}), last_sent_days_ago=ago)
    assert result.can_send is sendable


def test_the_first_reminder_has_nothing_to_wait_for() -> None:
    assert _assess(days=40, last_sent_days_ago=None).can_send


def test_the_gap_is_checked_before_a_missing_interest_rate() -> None:
    """A customer reminded yesterday is 'too soon', not also refused for a rate."""
    result = _assess(days=40, sent=frozenset({1, 2}), last_sent_days_ago=1, rate_table=[])
    assert result.blocker is Blocker.TOO_SOON


def test_a_finished_ladder_still_says_complete_not_too_soon() -> None:
    result = _assess(days=400, sent=frozenset({1, 2, 3}), last_sent_days_ago=1)
    assert result.blocker is Blocker.LADDER_COMPLETE


def test_a_step_not_yet_due_still_says_so_not_too_soon() -> None:
    result = _assess(days=3, last_sent_days_ago=1)
    assert result.blocker is Blocker.STEP_NOT_DUE

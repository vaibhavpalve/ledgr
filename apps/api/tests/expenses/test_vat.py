"""FR-EXP-001b's split, against the Python implementation.

Runs tests/expenses/vat_cases.py - the same table
tests/integration/test_expense_form.py runs against migration 0033's SQL - plus
the properties that have to hold for amounts nobody wrote a case for.

The property that matters most is `net + vat == gross`, for every gross and
every rate. A cent's disagreement there is a ledger that does not balance
(FR-GL-001), and it is exactly what independent rounding of both halves
produces.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, getcontext

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.expenses.vat import (
    MONEY,
    ROUNDING,
    VatError,
    round_money,
    split_gross,
    vat_from_gross,
)
from tests.expenses.vat_cases import AMBIGUOUS_BUT_VALID, REJECTED, VAT_CASES, VatCase

SETTINGS = settings(
    max_examples=400,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Every gross a numeric(19,2) column can hold, at the scale it holds it.
GROSS = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("99999999999999999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
#: Every rate vat_rate.rate can hold (numeric(6,3), 0..100).
RATES = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)
#: The rates the shipped Dutch ruleset actually contains.
DUTCH_RATES = st.sampled_from(
    [Decimal("0.00"), Decimal("6.00"), Decimal("9.00"), Decimal("19.00"), Decimal("21.00")]
)


def test_the_case_table_is_not_empty() -> None:
    """A table-driven suite that finds no rows passes completely and
    guarantees nothing.
    """
    assert len(VAT_CASES) >= 10
    assert REJECTED


@pytest.mark.parametrize("case", VAT_CASES, ids=lambda c: f"{c.gross}@{c.rate}")
def test_the_split_matches_the_shared_table(case: VatCase) -> None:
    split = split_gross(case.gross_amount, case.rate_percent)

    assert split.vat == case.expected_vat, case.why
    assert split.net == case.expected_net, case.why
    assert split.gross == case.gross_amount


@pytest.mark.parametrize(
    ("gross", "rate", "why"), REJECTED, ids=lambda v: v if isinstance(v, str) else str(v)
)
def test_rejected_inputs_are_refused(gross: str, rate: str, why: str) -> None:
    with pytest.raises(VatError):
        split_gross(Decimal(gross), Decimal(rate))


@pytest.mark.parametrize(("gross", "rate"), AMBIGUOUS_BUT_VALID)
def test_a_small_but_real_rate_is_accepted(gross: str, rate: str) -> None:
    """0.21 means 0.21%, and is a rate `vat_rate` can hold.

    Recorded as a decision: a validator that guessed the caller meant 21%
    would be wrong for whichever real rate it excluded. The protection is that
    the form takes a TREATMENT, so there is no field for the mistake to arrive
    through.
    """
    assert split_gross(Decimal(gross), Decimal(rate)).vat >= 0


# ===========================================================================
# The rounding mode, which is chosen rather than inherited
# ===========================================================================


def test_rounding_is_half_up_not_bankers() -> None:
    """Python's default decimal context is ROUND_HALF_EVEN, so `quantize()`
    without an explicit mode rounds 0.025 to 0.02.

    That is not how a Dutch tax figure is rounded and not how anybody checking
    the sum by hand would round it. Asserted directly on the primitive, because
    contriving a gross and a rate that land exactly on a half is harder to read
    than the thing being tested.
    """
    assert ROUNDING is ROUND_HALF_UP
    assert round_money(Decimal("0.025")) == Decimal("0.03")
    assert round_money(Decimal("0.035")) == Decimal("0.04")

    # What the default would have given, so the difference is visible.
    assert Decimal("0.025").quantize(MONEY, rounding=ROUND_HALF_EVEN) == Decimal("0.02")


def test_the_module_does_not_depend_on_the_ambient_context() -> None:
    """A rounding mode inherited from the process context is one somebody else
    can change. Every call site states it, so this holds even when the context
    says otherwise.
    """
    original = getcontext().rounding
    try:
        getcontext().rounding = ROUND_HALF_EVEN
        assert round_money(Decimal("0.025")) == Decimal("0.03")
        assert vat_from_gross(Decimal("0.03"), Decimal("21")) == Decimal("0.01")
    finally:
        getcontext().rounding = original


# ===========================================================================
# Float is refused, never converted
# ===========================================================================


@pytest.mark.parametrize("bad", [7299.70, 0.1, 121.0])
def test_a_float_gross_is_refused(bad: float) -> None:
    """NFR-031 / CLAUDE.md rule four. A float that reaches this boundary has
    already lost the precision; converting it would launder the loss into a
    calculation that looks exact.
    """
    with pytest.raises(VatError, match="float"):
        split_gross(bad, Decimal("21"))  # type: ignore[arg-type]


def test_a_float_rate_is_refused() -> None:
    with pytest.raises(VatError, match="float"):
        split_gross(Decimal("121.00"), 21.0)  # type: ignore[arg-type]


def test_the_float_trap_sum_splits_correctly() -> None:
    """4335.09 + 2964.61 is 7299.700000000001 in binary floating point - the
    exact sum api.ledger.model names. The decimal total splits cleanly.
    """
    gross = Decimal("4335.09") + Decimal("2964.61")
    assert gross == Decimal("7299.70")

    split = split_gross(gross, Decimal("21"))
    assert split.net + split.vat == Decimal("7299.70")


# ===========================================================================
# Properties, over every amount nobody wrote a case for
# ===========================================================================


@given(gross=GROSS, rate=RATES)
@SETTINGS
def test_net_plus_vat_is_always_exactly_gross(gross: Decimal, rate: Decimal) -> None:
    """The property the whole module exists for.

    A cent's disagreement here is a ledger that does not balance (FR-GL-001),
    and it is precisely what rounding both halves independently produces.
    """
    split = split_gross(gross, rate)
    assert split.net + split.vat == split.gross


@given(gross=GROSS, rate=RATES)
@SETTINGS
def test_both_halves_are_whole_cents(gross: Decimal, rate: Decimal) -> None:
    """numeric(19,2) would round a third decimal on the way in, and the
    difference would appear in an amount nobody chose.
    """
    split = split_gross(gross, rate)
    assert split.vat == split.vat.quantize(MONEY)
    assert split.net == split.net.quantize(MONEY)


@given(gross=GROSS, rate=RATES)
@SETTINGS
def test_neither_half_is_negative(gross: Decimal, rate: Decimal) -> None:
    """A negative net would mean the VAT exceeded the total paid."""
    split = split_gross(gross, rate)
    assert split.vat >= 0
    assert split.net >= 0


@given(gross=GROSS, rate=RATES)
@SETTINGS
def test_vat_never_exceeds_gross(gross: Decimal, rate: Decimal) -> None:
    split = split_gross(gross, rate)
    assert split.vat <= split.gross


@given(gross=GROSS)
@SETTINGS
def test_a_zero_rate_leaves_the_whole_amount_as_net(gross: Decimal) -> None:
    """btw_0, btw_vrijgesteld, btw_verlegd, btw_icp and btw_export all carry
    rate 0 in the shipped ruleset.
    """
    split = split_gross(gross, Decimal("0"))
    assert split.vat == Decimal("0.00")
    assert split.net == gross


@given(gross=GROSS, lower=DUTCH_RATES, higher=DUTCH_RATES)
@SETTINGS
def test_a_higher_rate_never_contains_less_vat(
    gross: Decimal, lower: Decimal, higher: Decimal
) -> None:
    """Monotonic in the rate. Obvious, and rounding is exactly the kind of
    thing that breaks obvious properties at the boundaries.
    """
    if lower > higher:
        lower, higher = higher, lower
    assert vat_from_gross(gross, lower) <= vat_from_gross(gross, higher)


@given(rate=DUTCH_RATES)
@SETTINGS
def test_an_exact_gross_splits_without_a_residue(rate: Decimal) -> None:
    """A gross of (100 + rate) contains exactly `rate` of VAT and 100 of net -
    the case anybody checks by hand, over every shipped rate.
    """
    gross = (Decimal(100) + rate).quantize(MONEY)
    split = split_gross(gross, rate)

    assert split.vat == rate.quantize(MONEY)
    assert split.net == Decimal("100.00")

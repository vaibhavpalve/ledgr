"""Effective-date boundaries, as data (CMP-014).

    CMP-014  Rate and rule changes are effective-dated, so historical periods
             keep the rules that applied at the time.

Every case is a date and the rate that applied on it. The interesting ones are
all within a day of a change, because that is the only place an off-by-one in
`valid_from <= date` can hide: get the comparison wrong and every date except
the boundary still answers correctly.

Executed twice - against the in-memory rules in tests/vat/test_rules.py and
against migration 0028 in tests/integration/test_effective_dated_rules.py - so
a resolution the two disagree about fails rather than diverging quietly. The
same bargain tests/ledger/cases.py makes for the posting invariants.

--- Why these dates ---

The Dutch rate changes are real and are the reason the requirement exists:

    2012-10-01  standard rate 19% -> 21%
    2019-01-01  reduced rate   6% -> 9%

An invoice dated 2018-12-31 is 6% forever. That is the whole of CMP-014 in one
sentence, and the two cases either side of that midnight are the ones worth
having.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RateCase:
    name: str
    treatment: str
    on_date: date
    #: None means no rule covers the date - a data gap, not a rate of zero.
    expected: Decimal | None


RATE_CASES: tuple[RateCase, ...] = (
    # -- the reduced rate, either side of 2019-01-01 ----------------------
    RateCase(
        name="reduced rate the day before it rose",
        treatment="btw_9",
        on_date=date(2018, 12, 31),
        expected=Decimal("6.000"),
    ),
    RateCase(
        name="reduced rate on the day it rose",
        treatment="btw_9",
        on_date=date(2019, 1, 1),
        expected=Decimal("9.000"),
    ),
    RateCase(
        name="reduced rate the day after it rose",
        treatment="btw_9",
        on_date=date(2019, 1, 2),
        expected=Decimal("9.000"),
    ),
    # -- the standard rate, either side of 2012-10-01 ---------------------
    RateCase(
        name="standard rate the day before it rose",
        treatment="btw_21",
        on_date=date(2012, 9, 30),
        expected=Decimal("19.000"),
    ),
    RateCase(
        name="standard rate on the day it rose",
        treatment="btw_21",
        on_date=date(2012, 10, 1),
        expected=Decimal("21.000"),
    ),
    # -- far from any boundary --------------------------------------------
    RateCase(
        name="standard rate long after the change",
        treatment="btw_21",
        on_date=date(2026, 6, 30),
        expected=Decimal("21.000"),
    ),
    RateCase(
        name="the zero rate is zero, not absent",
        treatment="btw_0",
        on_date=date(2026, 6, 30),
        expected=Decimal("0.000"),
    ),
    RateCase(
        name="reverse charge carries no VAT for the seller",
        treatment="btw_verlegd",
        on_date=date(2026, 6, 30),
        expected=Decimal("0.000"),
    ),
    # -- before the ruleset begins ----------------------------------------
    RateCase(
        # The floor rows start at 2001-01-01. A date before that resolves to
        # nothing, and nothing must not read as zero: a return computed at 0%
        # because the ruleset had a hole is wrong in a way no later check
        # catches.
        name="a date before the ruleset begins has no rate at all",
        treatment="btw_21",
        on_date=date(2000, 12, 31),
        expected=None,
    ),
)


#: Which box each treatment reports into, on a date well clear of any change.
#: Thin on purpose: the mapping's CORRECTNESS is FR-VAT-001's, and the shipped
#: dataset says so. What is asserted here is that a mapping resolves at all and
#: that the seller-side VAT box is absent exactly where the seller charges no
#: VAT - which is a structural claim, not a tax one.
RUBRIEK_EXPECTATIONS: dict[str, tuple[str, str | None]] = {
    "btw_21": ("1a", "1a"),
    "btw_9": ("1b", "1b"),
    "btw_0": ("1e", None),
    "btw_vrijgesteld": ("1e", None),
    "btw_verlegd": ("1e", None),
    "btw_icp": ("3b", None),
    "btw_export": ("3a", None),
    "btw_marge": ("1a", "1a"),
}

#: FR-AR-002's eight, which is also what 0024's CHECK on
#: ledger_account.default_vat_code allows. A chart can already name any of
#: them, so a ruleset that covered only seven would leave one unpriceable.
ALL_TREATMENTS: tuple[str, ...] = tuple(sorted(RUBRIEK_EXPECTATIONS))

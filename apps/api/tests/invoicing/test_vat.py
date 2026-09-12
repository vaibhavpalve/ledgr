"""Invoice VAT arithmetic - FR-AR-001, FR-AR-002, NFR-031."""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.expenses.vat import VatError
from api.invoicing.vat import (
    InvoiceLineAmounts,
    VatGroup,
    group_vat,
    line_net,
    totals_for,
)
from api.vat.rules import TreatmentRole

STANDARD = Decimal("21")
REDUCED = Decimal("9")


def net(treatment: str, role: TreatmentRole, amount: str) -> InvoiceLineAmounts:
    return InvoiceLineAmounts(treatment=treatment, role=role, net=Decimal(amount))


# ---------------------------------------------------------------------------
# FR-AR-001: quantity, unit price, discount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "unit_price", "discount", "expected"),
    [
        ("1", "100.00", "0", "100.00"),
        ("3", "12.50", "0", "37.50"),
        # Four decimals on the unit price is the reason the column is
        # numeric(19,4): rounding the PRICE to the cent would lose it.
        ("1000", "0.0350", "0", "35.00"),
        ("10", "9.99", "10", "89.91"),
        ("1", "100.00", "100", "0.00"),
        ("1", "33.33", "33.3333", "22.22"),
        # A negative quantity is a credit note line (service.credit negates the
        # quantity, not the price, so the price a customer was charged stays
        # legible on the correction).
        ("-2", "50.00", "0", "-100.00"),
    ],
)
def test_line_net(quantity: str, unit_price: str, discount: str, expected: str) -> None:
    assert line_net(
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        discount_percent=Decimal(discount),
    ) == Decimal(expected)


def test_line_net_rounds_half_up_at_the_line() -> None:
    # Rounded here, once, because this is the figure printed on the line and
    # added up by hand. 2 x 0.125 = 0.25 exactly; 3 x 0.125 = 0.375 -> 0.38.
    assert line_net(quantity=Decimal(3), unit_price=Decimal("0.125")) == Decimal("0.38")


@pytest.mark.parametrize("discount", ["-1", "101"])
def test_line_net_refuses_a_discount_outside_nought_to_a_hundred(discount: str) -> None:
    with pytest.raises(VatError, match="percentage"):
        line_net(quantity=Decimal(1), unit_price=Decimal(10), discount_percent=Decimal(discount))


def test_line_net_refuses_a_float() -> None:
    # NFR-031 and CLAUDE.md rule four. A float has already lost the precision;
    # converting it here would launder the loss into a figure that looks exact.
    with pytest.raises(VatError, match="float"):
        line_net(quantity=Decimal(1), unit_price=100.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FR-AR-002: VAT per treatment group
# ---------------------------------------------------------------------------


def test_vat_is_computed_on_the_group_not_per_line() -> None:
    """The decision with money in it.

    Twelve lines of 0.05 at 21% are twelve lots of 0.0105. Rounded per line
    that is 12 x 0.01 = 0.12; rounded on the group total it is round(0.63)
    = 0.13. Art. 226 requires the invoice to show the VAT per RATE, so the
    group is the answer - and it is also the figure that reaches the aangifte.
    """
    lines = [net("btw_21", TreatmentRole.STANDARD, "0.05") for _ in range(12)]

    totals = totals_for(lines, rate_for={"btw_21": STANDARD})

    assert totals.net == Decimal("0.60")
    assert totals.vat == Decimal("0.13")
    assert totals.vat != Decimal("0.12")


def test_two_rates_produce_two_groups() -> None:
    totals = totals_for(
        [
            net("btw_21", TreatmentRole.STANDARD, "100.00"),
            net("btw_9", TreatmentRole.REDUCED, "200.00"),
        ],
        rate_for={"btw_21": STANDARD, "btw_9": REDUCED},
    )

    # Ordered by treatment CODE, so "btw_21" precedes "btw_9" - '2' sorts
    # before '9'. Ugly to read and the right property to have: the order is
    # stable, so an invoice rendered twice puts its VAT blocks in the same
    # place both times.
    assert [(g.treatment, g.taxable, g.vat) for g in totals.groups] == [
        ("btw_21", Decimal("100.00"), Decimal("21.00")),
        ("btw_9", Decimal("200.00"), Decimal("18.00")),
    ]
    assert (totals.net, totals.vat, totals.gross) == (
        Decimal("300.00"),
        Decimal("39.00"),
        Decimal("339.00"),
    )


def test_groups_are_ordered_so_an_invoice_renders_the_same_way_twice() -> None:
    totals = totals_for(
        [
            net("btw_9", TreatmentRole.REDUCED, "1.00"),
            net("btw_21", TreatmentRole.STANDARD, "1.00"),
            net("btw_0", TreatmentRole.ZERO, "1.00"),
        ],
        rate_for={"btw_21": STANDARD, "btw_9": REDUCED, "btw_0": Decimal(0)},
    )

    assert [g.treatment for g in totals.groups] == ["btw_0", "btw_21", "btw_9"]


@pytest.mark.parametrize(
    ("treatment", "role"),
    [
        ("btw_0", TreatmentRole.ZERO),
        ("btw_vrijgesteld", TreatmentRole.EXEMPT),
        ("btw_verlegd", TreatmentRole.REVERSE_CHARGE),
        ("btw_icp", TreatmentRole.INTRA_COMMUNITY),
        ("btw_export", TreatmentRole.EXPORT),
        ("btw_marge", TreatmentRole.MARGIN),
    ],
)
def test_the_six_treatments_that_charge_no_vat(treatment: str, role: TreatmentRole) -> None:
    # Same number, six different legal meanings. They are kept apart because
    # the wording differs, the rubriek differs, and for two of them the
    # customer's VAT number becomes mandatory.
    totals = totals_for([net(treatment, role, "500.00")], rate_for={treatment: Decimal(0)})

    assert totals.vat == Decimal("0.00")
    assert totals.net == Decimal("500.00")
    assert totals.gross == Decimal("500.00")
    assert totals.groups[0].role is role


def test_the_margin_scheme_states_no_rate_at_all() -> None:
    # Under the margeregeling VAT is due on the margin, not the sale price, and
    # stating a rate on the sale price is not permitted. None, not Decimal(0):
    # btw_0's answer is a rate that happens to be nought, which is a different
    # fact.
    totals = totals_for(
        [net("btw_marge", TreatmentRole.MARGIN, "750.00")],
        rate_for={"btw_marge": Decimal("21")},
    )

    assert totals.groups[0].rate is None
    assert totals.groups[0].vat == Decimal("0.00")


def test_a_zero_rated_group_keeps_a_rate_of_zero() -> None:
    totals = totals_for([net("btw_0", TreatmentRole.ZERO, "10.00")], rate_for={"btw_0": Decimal(0)})

    assert totals.groups[0].rate == Decimal(0)


def test_a_margin_group_cannot_be_constructed_with_a_rate() -> None:
    with pytest.raises(VatError, match="margin scheme"):
        VatGroup(
            treatment="btw_marge",
            role=TreatmentRole.MARGIN,
            rate=Decimal("21"),
            taxable=Decimal("100.00"),
            vat=Decimal("0.00"),
        )


def test_a_no_vat_treatment_cannot_be_constructed_with_vat() -> None:
    with pytest.raises(VatError, match="charges the customer none"):
        VatGroup(
            treatment="btw_verlegd",
            role=TreatmentRole.REVERSE_CHARGE,
            rate=None,
            taxable=Decimal("100.00"),
            vat=Decimal("21.00"),
        )


def test_a_missing_rate_is_refused_rather_than_treated_as_zero() -> None:
    # CMP-014. Computing zero for a date the ruleset does not cover would put a
    # wrong figure on the invoice AND on the return, with nothing to show it
    # was wrong.
    with pytest.raises(VatError, match="no VAT rate is defined"):
        group_vat(role=TreatmentRole.STANDARD, taxable=Decimal("100.00"), rate=None)


def test_net_plus_vat_is_exactly_gross() -> None:
    # The identity the module asserts on the way out. Property-ish: a spread of
    # awkward amounts across two rates.
    lines = [
        net("btw_21", TreatmentRole.STANDARD, amount)
        for amount in ("0.01", "0.03", "0.07", "19.99", "1234.56")
    ] + [net("btw_9", TreatmentRole.REDUCED, amount) for amount in ("0.02", "0.05", "99.99")]

    totals = totals_for(lines, rate_for={"btw_21": STANDARD, "btw_9": REDUCED})

    assert totals.net + totals.vat == totals.gross
    assert sum(g.vat for g in totals.groups) == totals.vat
    assert sum(g.taxable for g in totals.groups) == totals.net


def test_an_empty_invoice_totals_to_nothing() -> None:
    totals = totals_for([], rate_for={})

    assert (totals.net, totals.vat, totals.gross) == (Decimal(0), Decimal(0), Decimal(0))
    assert totals.groups == ()


def test_one_treatment_cannot_carry_two_roles() -> None:
    # A corrupt lookup rather than a user error - and it would put two
    # different legal statements under one heading on the document.
    with pytest.raises(VatError, match="more than one role"):
        totals_for(
            [
                net("btw_21", TreatmentRole.STANDARD, "1.00"),
                net("btw_21", TreatmentRole.EXEMPT, "1.00"),
            ],
            rate_for={"btw_21": STANDARD},
        )


def test_group_vat_refuses_a_float_taxable_amount() -> None:
    with pytest.raises(VatError, match="float"):
        group_vat(role=TreatmentRole.STANDARD, taxable=100.0, rate=STANDARD)  # type: ignore[arg-type]

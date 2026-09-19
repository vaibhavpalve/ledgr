"""SI-10's rules - api.invoicing.bad_debt (ADR-076)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.invoicing.bad_debt import (
    VAT_RECLAIM_WAIT_MONTHS,
    VatGroupTotal,
    WriteOffInvalid,
    add_months,
    split_outstanding,
    vat_reclaim_eligible_on,
)

D = Decimal


def group(treatment: str, taxable: str, vat: str) -> VatGroupTotal:
    return VatGroupTotal(treatment=treatment, taxable=D(taxable), vat=D(vat))


# --- the VAT inside what is unpaid ----------------------------------------
def test_an_unpaid_whole_invoice_carries_all_of_its_vat() -> None:
    (share,) = split_outstanding([group("btw_21", "1000.00", "210.00")], D("1210.00"))
    assert (share.treatment, share.amount, share.vat) == ("btw_21", D("1210.00"), D("210.00"))


def test_a_half_paid_invoice_carries_half_the_vat() -> None:
    (share,) = split_outstanding([group("btw_21", "1000.00", "210.00")], D("605.00"))
    assert (share.amount, share.vat) == (D("605.00"), D("105.00"))


def test_the_unpaid_balance_is_spread_over_groups_in_proportion_to_their_gross() -> None:
    # 1210.00 at 21% and 109.00 at 9%: gross 1319.00. Half unpaid = 659.50.
    shares = split_outstanding(
        [group("btw_21", "1000.00", "210.00"), group("btw_9", "100.00", "9.00")], D("659.50")
    )
    assert [s.treatment for s in shares] == ["btw_21", "btw_9"]
    assert sum(s.amount for s in shares) == D("659.50")
    assert shares[0].amount == D("605.00") and shares[0].vat == D("105.00")
    assert shares[1].amount == D("54.50") and shares[1].vat == D("4.50")


def test_a_group_without_vat_contributes_no_vat() -> None:
    shares = split_outstanding(
        [group("btw_21", "100.00", "21.00"), group("btw_0", "100.00", "0.00")], D("221.00")
    )
    assert shares[1].vat == D("0.00")
    assert shares[0].vat == D("21.00")


def test_a_group_with_no_gross_is_skipped() -> None:
    shares = split_outstanding(
        [group("btw_21", "100.00", "21.00"), group("btw_0", "0.00", "0.00")], D("121.00")
    )
    assert [s.treatment for s in shares] == ["btw_21"]


def test_the_shares_add_up_to_the_outstanding_balance_to_the_cent() -> None:
    """Three equal groups and a balance that does not divide by three: the spare cent goes
    to the first group (largest remainder, ties to the earlier) so nothing is lost or invented."""
    groups = [group(f"t{i}", "10.00", "0.00") for i in range(3)]
    shares = split_outstanding(groups, D("10.00"))
    assert sum(s.amount for s in shares) == D("10.00")
    assert [s.amount for s in shares] == [D("3.34"), D("3.33"), D("3.33")]


@given(
    balance=st.decimals(min_value="0.01", max_value="100000", places=2),
    taxables=st.lists(
        st.decimals(min_value="1", max_value="50000", places=2), min_size=1, max_size=4
    ),
    rate=st.sampled_from([D("0"), D("9"), D("21")]),
)
def test_the_split_is_always_exact_and_vat_never_exceeds_its_share(
    balance: Decimal, taxables: list[Decimal], rate: Decimal
) -> None:
    groups = [
        VatGroupTotal(
            treatment=f"t{i}",
            taxable=t,
            vat=(t * rate / 100).quantize(D("0.01")),
        )
        for i, t in enumerate(taxables)
    ]
    shares = split_outstanding(groups, balance)
    assert sum(s.amount for s in shares) == balance
    for share in shares:
        assert D("0") <= share.vat <= share.amount


def test_nothing_outstanding_or_a_float_is_refused() -> None:
    groups = [group("btw_21", "100.00", "21.00")]
    with pytest.raises(WriteOffInvalid, match="nothing outstanding"):
        split_outstanding(groups, D("0.00"))
    with pytest.raises(WriteOffInvalid, match="float"):
        split_outstanding(groups, 10.0)  # type: ignore[arg-type]
    with pytest.raises(WriteOffInvalid, match="float"):
        split_outstanding([VatGroupTotal("t", 1.0, D("0"))], D("1.00"))  # type: ignore[arg-type]


def test_an_invoice_with_no_total_is_refused() -> None:
    with pytest.raises(WriteOffInvalid, match="no total"):
        split_outstanding([], D("10.00"))


# --- when the VAT may be claimed ----------------------------------------
def test_the_wait_runs_from_the_due_date() -> None:
    assert VAT_RECLAIM_WAIT_MONTHS == 12
    assert vat_reclaim_eligible_on(
        due_date=date(2025, 10, 15), invoice_date=date(2025, 9, 15), customer_insolvent=False
    ) == date(2026, 10, 15)


def test_an_invoice_without_a_due_date_waits_from_its_invoice_date() -> None:
    assert vat_reclaim_eligible_on(
        due_date=None, invoice_date=date(2025, 9, 15), customer_insolvent=False
    ) == date(2026, 9, 15)


def test_an_insolvent_customer_waives_the_wait() -> None:
    assert (
        vat_reclaim_eligible_on(
            due_date=date(2026, 9, 1), invoice_date=date(2026, 8, 1), customer_insolvent=True
        )
        is None
    )


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),  # clamped to the month's last day
        (date(2027, 1, 31), 1, date(2027, 2, 28)),
        (date(2027, 12, 15), 1, date(2028, 1, 15)),  # crosses the year
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # leap year
        (date(2026, 3, 1), 12, date(2027, 3, 1)),
    ],
)
def test_add_months(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected

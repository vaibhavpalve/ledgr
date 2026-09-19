"""SI-06's arithmetic - api.invoicing.receivables (ADR-072).

Bucket boundaries are the whole point of an ageing report, so each is pinned on
both sides of the edge with dates worked out by hand.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.invoicing.receivables import (
    AgeBucket,
    AgeingReport,
    CustomerAgeing,
    MovementKind,
    OpenItem,
    StatementMovement,
    age_items,
    bucket_for,
    build_statement,
)

D = Decimal
AS_OF = date(2026, 6, 30)


def item(
    *,
    due: date | None,
    amount: str = "100.00",
    customer: uuid.UUID | None = None,
    name: str = "De Vries Holding B.V.",
    reference: str = "2026-1",
) -> OpenItem:
    return OpenItem(
        invoice_id=uuid.uuid4(),
        invoice_reference=reference,
        invoice_date=date(2026, 1, 1),
        due_date=due,
        customer_id=customer,
        customer_name=name,
        outstanding=D(amount),
    )


# ===========================================================================
# bucket boundaries
# ===========================================================================


@pytest.mark.parametrize(
    ("days_late", "expected"),
    [
        (-30, AgeBucket.CURRENT),  # not due for a month yet
        (-1, AgeBucket.CURRENT),
        (0, AgeBucket.CURRENT),  # the due date itself is not late
        (1, AgeBucket.DAYS_1_30),  # the first late day
        (30, AgeBucket.DAYS_1_30),
        (31, AgeBucket.DAYS_31_60),
        (60, AgeBucket.DAYS_31_60),
        (61, AgeBucket.DAYS_61_90),
        (90, AgeBucket.DAYS_61_90),
        (91, AgeBucket.OVER_90),
        (3650, AgeBucket.OVER_90),
    ],
)
def test_bucket_boundaries(days_late: int, expected: AgeBucket) -> None:
    assert bucket_for(AS_OF - timedelta(days=days_late), AS_OF) is expected


def test_no_due_date_has_its_own_bucket_and_is_never_guessed_late() -> None:
    assert bucket_for(None, AS_OF) is AgeBucket.NO_DUE_DATE


# ===========================================================================
# the report
# ===========================================================================


def test_an_empty_report_is_all_zeros() -> None:
    report = age_items([], AS_OF)

    assert report.grand_total == D("0.00")
    assert report.customers == ()
    assert set(report.bucket_totals) == set(AgeBucket)
    assert all(total == D("0.00") for total in report.bucket_totals.values())


def test_items_land_in_their_buckets_and_the_totals_add_up() -> None:
    items = [
        item(due=AS_OF + timedelta(days=5), amount="10.00"),  # current
        item(due=AS_OF - timedelta(days=10), amount="20.00"),  # 1-30
        item(due=AS_OF - timedelta(days=45), amount="30.00"),  # 31-60
        item(due=AS_OF - timedelta(days=75), amount="40.00"),  # 61-90
        item(due=AS_OF - timedelta(days=120), amount="50.00"),  # over 90
        item(due=None, amount="60.00"),  # no due date
    ]
    report = age_items(items, AS_OF)

    assert report.bucket_totals == {
        AgeBucket.CURRENT: D("10.00"),
        AgeBucket.DAYS_1_30: D("20.00"),
        AgeBucket.DAYS_31_60: D("30.00"),
        AgeBucket.DAYS_61_90: D("40.00"),
        AgeBucket.OVER_90: D("50.00"),
        AgeBucket.NO_DUE_DATE: D("60.00"),
    }
    assert report.grand_total == D("210.00")


def test_a_customer_is_grouped_across_invoices_and_cut_by_bucket() -> None:
    customer = uuid.uuid4()
    report = age_items(
        [
            item(due=AS_OF - timedelta(days=10), amount="100.00", customer=customer),
            item(due=AS_OF - timedelta(days=100), amount="250.00", customer=customer),
            item(due=AS_OF - timedelta(days=10), amount="5.00", name="Someone Else"),
        ],
        AS_OF,
    )

    biggest, smallest = report.customers
    assert biggest.customer_id == customer
    assert biggest.total == D("350.00")
    assert biggest.buckets[AgeBucket.DAYS_1_30] == D("100.00")
    assert biggest.buckets[AgeBucket.OVER_90] == D("250.00")
    assert smallest.total == D("5.00")


def test_customers_are_largest_debt_first_then_by_name() -> None:
    report = age_items(
        [
            item(due=AS_OF, amount="50.00", name="Zeta"),
            item(due=AS_OF, amount="50.00", name="Alfa"),
            item(due=AS_OF, amount="900.00", name="Middle"),
        ],
        AS_OF,
    )
    assert [c.customer_name for c in report.customers] == ["Middle", "Alfa", "Zeta"]


def test_a_one_off_customer_invoiced_twice_under_one_name_is_one_debtor() -> None:
    report = age_items(
        [
            item(due=AS_OF, amount="10.00", name="Jansen Bouw", reference="2026-1"),
            item(due=AS_OF, amount="15.00", name="  jansen bouw ", reference="2026-2"),
        ],
        AS_OF,
    )
    (only,) = report.customers
    assert only.total == D("25.00")
    assert len(only.items) == 2


def test_two_different_customer_records_with_one_name_stay_two_debtors() -> None:
    """The master record is the identity; the name is only a fallback for one-offs."""
    report = age_items(
        [
            item(due=AS_OF, amount="10.00", customer=uuid.uuid4(), name="Same Name"),
            item(due=AS_OF, amount="10.00", customer=uuid.uuid4(), name="Same Name"),
        ],
        AS_OF,
    )
    assert len(report.customers) == 2


def test_the_drill_down_carries_each_items_bucket_and_days_late() -> None:
    customer = uuid.uuid4()
    late = item(due=AS_OF - timedelta(days=40), customer=customer)
    report = age_items([late], AS_OF)

    ((found, bucket, days_late),) = report.customers[0].items
    assert found.invoice_id == late.invoice_id  # FR-RPT-002: down to the invoice
    assert (bucket, days_late) == (AgeBucket.DAYS_31_60, 40)


def test_items_within_a_customer_are_oldest_due_first() -> None:
    customer = uuid.uuid4()
    report = age_items(
        [
            item(due=AS_OF - timedelta(days=5), customer=customer, reference="B"),
            item(due=AS_OF - timedelta(days=50), customer=customer, reference="A"),
            item(due=None, customer=customer, reference="C"),
        ],
        AS_OF,
    )
    assert [row[0].invoice_reference for row in report.customers[0].items] == ["A", "B", "C"]


def test_a_settled_item_is_ignored() -> None:
    report = age_items([item(due=AS_OF, amount="0.00"), item(due=AS_OF, amount="-5.00")], AS_OF)
    assert report.customers == ()
    assert report.grand_total == D("0.00")


def test_a_float_is_refused() -> None:
    bad = OpenItem(uuid.uuid4(), "x", AS_OF, AS_OF, None, "n", 10.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        age_items([bad], AS_OF)


def test_a_report_that_does_not_add_up_cannot_be_constructed() -> None:
    """Three cuts of the same money must agree, or the report refuses to exist."""
    customer = CustomerAgeing(
        customer_id=None,
        customer_name="x",
        buckets={b: D("0.00") for b in AgeBucket} | {AgeBucket.CURRENT: D("10.00")},
        items=(),
    )
    with pytest.raises(ValueError, match="does not add up"):
        AgeingReport(
            as_of=AS_OF,
            bucket_totals={b: D("0.00") for b in AgeBucket} | {AgeBucket.CURRENT: D("99.00")},
            customers=(customer,),
            grand_total=D("10.00"),
        )


@given(
    rows=st.lists(
        st.tuples(
            st.integers(min_value=-200, max_value=400),
            st.decimals(min_value="0.01", max_value="100000", places=2),
            st.integers(min_value=0, max_value=4),
            st.booleans(),
        ),
        max_size=40,
    )
)
@settings(max_examples=150)
def test_the_three_cuts_always_agree(rows: list[tuple[int, Decimal, int, bool]]) -> None:
    """Whatever the mix, bucket totals == customer totals == grand total == the sum
    of what went in - `AgeingReport` would raise otherwise."""
    customers = [uuid.uuid4() for _ in range(5)]
    items = [
        item(
            due=None if no_due else AS_OF - timedelta(days=days),
            amount=str(amount),
            customer=customers[c],
        )
        for days, amount, c, no_due in rows
    ]
    report = age_items(items, AS_OF)

    assert report.grand_total == sum((i.outstanding for i in items), D("0.00"))
    assert sum(report.bucket_totals.values(), D("0.00")) == report.grand_total
    assert sum((c.total for c in report.customers), D("0.00")) == report.grand_total


# ===========================================================================
# the statement
# ===========================================================================


def move(
    on: date, kind: MovementKind, *, debit: str = "0.00", credit: str = "0.00", ref: str = "2026-1"
) -> StatementMovement:
    return StatementMovement(
        movement_date=on,
        kind=kind,
        reference=ref,
        invoice_id=None,
        debit=D(debit),
        credit=D(credit),
    )


JAN, FEB, MAR = date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)

MOVEMENTS = [
    move(JAN, MovementKind.INVOICE, debit="1210.00", ref="2026-1"),
    move(FEB, MovementKind.PAYMENT, credit="400.00", ref="2026-1"),
    move(MAR, MovementKind.INVOICE, debit="605.00", ref="2026-2"),
    move(MAR, MovementKind.CREDIT_NOTE, credit="605.00", ref="2026-2"),
]


def test_a_statement_for_the_whole_history_has_no_opening_balance() -> None:
    statement = build_statement(MOVEMENTS, date(2026, 1, 1), date(2026, 12, 31))

    assert statement.opening_balance == D("0.00")
    assert [line.balance for line in statement.lines] == [
        D("1210.00"),
        D("810.00"),  # 1210 - 400
        D("1415.00"),  # + 605
        D("810.00"),  # - 605 credit note
    ]
    assert statement.closing_balance == D("810.00")
    assert statement.total_debit == D("1815.00")
    assert statement.total_credit == D("1005.00")


def test_earlier_movements_are_folded_into_the_opening_balance() -> None:
    """A statement for March starts from what was owed at the end of February."""
    statement = build_statement(MOVEMENTS, date(2026, 3, 1), date(2026, 3, 31))

    assert statement.opening_balance == D("810.00")  # 1210 - 400
    assert len(statement.lines) == 2
    assert statement.closing_balance == D("810.00")
    assert statement.total_debit == D("605.00")


def test_movements_after_the_period_are_ignored() -> None:
    statement = build_statement(MOVEMENTS, date(2026, 1, 1), date(2026, 1, 31))
    assert statement.closing_balance == D("1210.00")
    assert len(statement.lines) == 1


def test_the_period_bounds_are_inclusive() -> None:
    on = date(2026, 5, 1)
    statement = build_statement([move(on, MovementKind.INVOICE, debit="10.00")], on, on)
    assert len(statement.lines) == 1


def test_a_customer_in_credit_shows_a_negative_balance() -> None:
    """A credit note after a full payment leaves the customer owed money; the
    statement says so instead of hiding it at zero."""
    statement = build_statement(
        [
            move(JAN, MovementKind.INVOICE, debit="100.00"),
            move(FEB, MovementKind.PAYMENT, credit="100.00"),
            move(MAR, MovementKind.CREDIT_NOTE, credit="100.00"),
        ],
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert statement.closing_balance == D("-100.00")


def test_a_voided_payment_owes_the_amount_again() -> None:
    statement = build_statement(
        [
            move(JAN, MovementKind.INVOICE, debit="100.00"),
            move(FEB, MovementKind.PAYMENT, credit="100.00"),
            move(MAR, MovementKind.PAYMENT_VOID, debit="100.00"),
        ],
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert statement.closing_balance == D("100.00")


def test_unsorted_movements_are_ordered_by_date_stably() -> None:
    shuffled = [MOVEMENTS[2], MOVEMENTS[0], MOVEMENTS[3], MOVEMENTS[1]]
    statement = build_statement(shuffled, date(2026, 1, 1), date(2026, 12, 31))

    assert [line.movement.movement_date for line in statement.lines] == sorted(
        m.movement_date for m in MOVEMENTS
    )
    assert statement.closing_balance == D("810.00")


def test_an_inverted_period_is_refused() -> None:
    with pytest.raises(ValueError, match="before it starts"):
        build_statement(MOVEMENTS, date(2026, 12, 31), date(2026, 1, 1))


def test_a_float_amount_is_refused() -> None:
    bad = StatementMovement(JAN, MovementKind.INVOICE, "x", None, 10.0, D("0"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_statement([bad], date(2026, 1, 1), date(2026, 12, 31))


@given(
    amounts=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=300),
            st.decimals(min_value="0.01", max_value="50000", places=2),
            st.booleans(),
        ),
        max_size=30,
    ),
    start=st.integers(min_value=0, max_value=300),
    length=st.integers(min_value=0, max_value=300),
)
@settings(max_examples=150)
def test_opening_plus_movements_is_closing_for_any_period(
    amounts: list[tuple[int, Decimal, bool]], start: int, length: int
) -> None:
    """The invariant that makes a statement trustworthy: for ANY period, opening +
    debits - credits == closing, and the last line's balance IS the closing one.
    (`Statement` raises if not; this drives it with arbitrary histories.)"""
    origin = date(2026, 1, 1)
    movements = [
        move(
            origin + timedelta(days=days),
            MovementKind.INVOICE if is_debit else MovementKind.PAYMENT,
            debit=str(amount) if is_debit else "0.00",
            credit="0.00" if is_debit else str(amount),
        )
        for days, amount, is_debit in amounts
    ]
    date_from = origin + timedelta(days=start)
    statement = build_statement(movements, date_from, date_from + timedelta(days=length))

    assert (
        statement.opening_balance + statement.total_debit - statement.total_credit
        == statement.closing_balance
    )
    if statement.lines:
        assert statement.lines[-1].balance == statement.closing_balance
    else:
        assert statement.closing_balance == statement.opening_balance

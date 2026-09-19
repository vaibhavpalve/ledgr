"""Migration 0054 against a real Postgres - ADR-072.

What only the database can establish: that `invoicing.receivable_items` answers "as
of a date" by filtering every movement by its own date (so a past report does not
rewrite itself), that it agrees with `invoicing.invoice_balances` when nothing is
dated after the report, that `invoicing.customer_movements` lists exactly the four
movements the ledger records, that a customer's closing balance reconciles to the
ledger's own sub-ledger, and that another tenant reads none of it.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0054.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from api.invoicing.receivables import MovementKind, build_statement
from api.invoicing.receivables_repository import SqlReceivablesRepository
from tests.integration.test_dunning import _overdue_invoice
from tests.integration.test_sales_invoice_payment import _balance, _pay
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

# The invoice is dated 2026-09-09 (the shared seed), due 2026-09-30, owing 1210.00.
INVOICE_DATE = date(2026, 9, 9)


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


async def _items(tenants: SeededTenants, as_of: date) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT invoice_id, gross, credited, paid, outstanding "
        "  FROM invoicing.receivable_items(:admin, :as_of)",
        admin=str(tenants.admin_a),
        as_of=as_of,
    )
    return list(result)


async def _movements(tenants: SeededTenants, customer: uuid.UUID) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT movement_date, kind, reference, invoice_id, debit, credit "
        "  FROM invoicing.customer_movements(:admin, :customer)",
        admin=str(tenants.admin_a),
        customer=str(customer),
    )
    return list(result)


# --- as of a date -----------------------------------------------------------------------


async def test_an_unpaid_invoice_owes_its_gross_as_of_any_date_after_it_was_issued(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)

    (row,) = await _items(two_organizations, date(2026, 12, 31))

    assert row.invoice_id == owed["invoice"]  # type: ignore[attr-defined]
    assert Decimal(row.outstanding) == Decimal("1210.00")  # type: ignore[attr-defined]


async def test_an_invoice_is_not_a_receivable_before_its_date(
    two_organizations: SeededTenants,
) -> None:
    await _overdue_invoice(two_organizations)
    assert await _items(two_organizations, date(2026, 9, 8)) == []  # the day before


async def test_a_paid_invoice_drops_out(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "1210.00")
    assert await _items(two_organizations, date(2026, 12, 31)) == []


async def test_a_payment_counts_only_from_the_day_it_was_received(
    two_organizations: SeededTenants,
) -> None:
    """The point of an as-of report: a payment dated 15 September was not a
    payment on 14 September, so the report for the 14th still shows it owed."""
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")  # paid_on 2026-09-15

    before = await _items(two_organizations, date(2026, 9, 14))
    on_the_day = await _items(two_organizations, date(2026, 9, 15))

    assert Decimal(before[0].outstanding) == Decimal("1210.00")  # type: ignore[attr-defined]
    assert Decimal(on_the_day[0].outstanding) == Decimal("810.00")  # type: ignore[attr-defined]


async def test_a_payment_voided_later_was_still_a_payment_on_the_earlier_date(
    two_organizations: SeededTenants,
) -> None:
    """A report for 16 September, run again after a void on 18 September, must not
    change: the payment WAS received then. From the void date on it is owed again."""
    owed = await _overdue_invoice(two_organizations)
    payment = await _pay(two_organizations, owed, "400.00")  # paid_on 2026-09-15
    reversal = await _pay_reversal_entry(two_organizations, owed)
    await _exec(
        two_organizations,
        "UPDATE sales_invoice_payment SET voided_at = :at, voided_by_user_id = :user, "
        "  void_journal_entry_id = :entry WHERE id = :id",
        at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        user=str(two_organizations.owner_a),
        entry=str(reversal),
        id=str(payment),
    )

    counted = await _items(two_organizations, date(2026, 9, 16))
    voided_day = await _items(two_organizations, date(2026, 9, 18))

    assert Decimal(counted[0].outstanding) == Decimal("810.00")  # type: ignore[attr-defined]
    assert Decimal(voided_day[0].outstanding) == Decimal("1210.00")  # type: ignore[attr-defined]


async def _pay_reversal_entry(tenants: SeededTenants, owed: dict[str, uuid.UUID]) -> uuid.UUID:
    from tests.integration.test_sales_invoice_payment import _receipt

    return await _receipt(
        tenants,
        owed,
        bank=owed["bank"],
        journal=owed["bank_journal"],
        party=owed["party"],
        amount="1.00",
    )


async def test_as_of_after_every_payment_agrees_with_invoice_balances(
    two_organizations: SeededTenants,
) -> None:
    """The two definitions differ only for a payment dated AFTER the report date,
    which is exactly where they should."""
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")

    (row,) = await _items(two_organizations, date(2026, 12, 31))

    assert Decimal(row.outstanding) == await _balance(two_organizations, owed)  # type: ignore[attr-defined]


async def test_a_credit_note_counts_only_from_its_own_date(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _credit_note(two_organizations, owed, on=date(2026, 10, 1))

    # Before the credit note the invoice is owed in full; from its date, not at all.
    assert Decimal((await _items(two_organizations, date(2026, 9, 30)))[0].outstanding) == (  # type: ignore[attr-defined]
        Decimal("1210.00")
    )
    assert await _items(two_organizations, date(2026, 10, 1)) == []


async def _credit_note(
    tenants: SeededTenants, owed: dict[str, uuid.UUID], *, on: date
) -> uuid.UUID:
    """An issued credit note for the whole of `owed`'s invoice."""
    year = owed["year"]
    note = await _scalar(
        tenants,
        "INSERT INTO sales_invoice (organization_id, administration_id, fiscal_year_id, "
        "  invoice_date, customer_name, customer_address, customer_country, "
        "  customer_language, credits_invoice_id) "
        "VALUES (:org, :admin, :year, :on, 'De Vries Holding B.V.', 'Damrak 70', 'NL', 'nl', "
        "  :original) RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        year=str(year),
        on=on,
        original=str(owed["invoice"]),
    )
    await _exec(
        tenants,
        "INSERT INTO sales_invoice_vat_total (invoice_id, organization_id, "
        "  administration_id, vat_treatment, rate, taxable_amount, vat_amount) "
        "VALUES (:note, :org, :admin, 'btw_21', 21, -1000.00, -210.00)",
        note=str(note),
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )
    await _exec(tenants, "UPDATE sales_invoice SET status = 'issued' WHERE id = :id", id=str(note))
    return note


# --- the statement's movements ------------------------------------------------------------


async def test_the_four_kinds_of_movement_are_listed_oldest_first(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    owed = await _overdue_invoice(two_organizations, customer_id=customer)
    payment = await _pay(two_organizations, owed, "400.00")  # 15 Sep
    reversal = await _pay_reversal_entry(two_organizations, owed)
    await _exec(
        two_organizations,
        "UPDATE sales_invoice_payment SET voided_at = :at, voided_by_user_id = :user, "
        "  void_journal_entry_id = :entry WHERE id = :id",
        at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        user=str(two_organizations.owner_a),
        entry=str(reversal),
        id=str(payment),
    )

    rows = await _movements(two_organizations, customer)

    assert [(r.movement_date, r.kind, r.debit, r.credit) for r in rows] == [  # type: ignore[attr-defined]
        (date(2026, 9, 9), "invoice", Decimal("1210.00"), Decimal("0")),
        (date(2026, 9, 15), "payment", Decimal("0"), Decimal("400.00")),
        (date(2026, 9, 18), "payment_void", Decimal("400.00"), Decimal("0")),
    ]


async def test_a_customer_with_nothing_has_no_movements(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    assert await _movements(two_organizations, customer) == []


# --- reconciliation -------------------------------------------------------------------------


async def test_a_statements_closing_balance_reconciles_to_the_ledger_subledger(
    two_organizations: SeededTenants,
) -> None:
    """FR-AR-012's statement and FR-GL-006's sub-ledger are two views of one set of
    entries. If they ever disagree, one of them is wrong - so they are compared."""
    customer = await _customer(two_organizations)
    # Link the customer to the ledger party the invoice was posted against, as
    # `SalesPostingService` does on first posting (0040).
    owed = await _overdue_invoice(two_organizations, customer_id=customer)
    await _exec(
        two_organizations,
        "UPDATE customer SET subledger_party_id = :party WHERE id = :customer",
        party=str(owed["party"]),
        customer=str(customer),
    )
    await _pay(two_organizations, owed, "400.00")

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        from sqlalchemy.ext.asyncio import AsyncSession

        movements = await SqlReceivablesRepository(AsyncSession(bind=conn)).movements(
            administration_id=two_organizations.admin_a, customer_id=customer
        )
    statement = build_statement(movements, date(2026, 1, 1), date(2026, 12, 31))

    ledger = await _exec(
        two_organizations,
        "SELECT balance FROM ledger.subledger_balance(:admin, 'accounts_receivable') "
        " WHERE party_id = :party",
        admin=str(two_organizations.admin_a),
        party=str(owed["party"]),
    )
    assert statement.closing_balance == Decimal(ledger.scalar_one()) == Decimal("810.00")
    assert [line.movement.kind for line in statement.lines] == [
        MovementKind.INVOICE,
        MovementKind.PAYMENT,
    ]


# --- the repository ---------------------------------------------------------------------------


async def test_the_repository_reads_items_customer_and_movements(
    two_organizations: SeededTenants,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    customer = await _customer(two_organizations)
    owed = await _overdue_invoice(two_organizations, customer_id=customer)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        repository = SqlReceivablesRepository(AsyncSession(bind=conn))
        items = await repository.open_items(
            administration_id=two_organizations.admin_a, as_of=date(2026, 12, 31)
        )
        name = await repository.customer_name(
            administration_id=two_organizations.admin_a, customer_id=customer
        )
        unknown = await repository.customer_name(
            administration_id=two_organizations.admin_a, customer_id=uuid.uuid4()
        )

    (item,) = items
    assert item.invoice_id == owed["invoice"]
    assert item.outstanding == Decimal("1210.00")
    assert item.due_date == date(2026, 9, 30)
    assert item.customer_id == customer
    assert name == "De Vries Holding B.V."
    assert unknown is None


# --- tenant isolation -------------------------------------------------------------------------


async def test_another_tenant_reads_none_of_it(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    await _overdue_invoice(two_organizations, customer_id=customer)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        items = (
            await conn.execute(
                text("SELECT count(*) FROM invoicing.receivable_items(:admin, :as_of)"),
                {"admin": str(two_organizations.admin_a), "as_of": date(2026, 12, 31)},
            )
        ).scalar_one()
        movements = (
            await conn.execute(
                text("SELECT count(*) FROM invoicing.customer_movements(:admin, :customer)"),
                {"admin": str(two_organizations.admin_a), "customer": str(customer)},
            )
        ).scalar_one()

    assert (items, movements) == (0, 0)

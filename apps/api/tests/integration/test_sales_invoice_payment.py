"""Migration 0052 against a real Postgres - ADR-070.

The properties here are the database's, which no fake can establish: that a
payment cannot exceed what is owed even when two are recorded at once, that only
an issued non-credit-note invoice can be paid and only into a plain asset
account, that a payment is immutable apart from one void, that `ledgr_app` cannot
delete one, that `invoicing.invoice_balances` is the one definition of
"outstanding", and that another tenant cannot see a payment at all.

Reuses `test_sales_invoice_posting`'s world (a chart, a journal, an open period)
and its issued-and-posted invoice. Skipped without TENANT_ISOLATION_TESTS_ENABLED=1
(see this package's conftest), and needs migrations through 0052 applied and the
VAT reference data loaded.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.integration.test_sales_invoice_posting import (
    _exec,
    _issued_and_posted,
    _party,
    _scalar,
    _world,
)
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

GROSS = Decimal("1210.00")


async def _bank(tenants: SeededTenants) -> uuid.UUID:
    """A plain asset account - what a payment may land in."""
    return await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(tenants.admin_a),
    )


async def _bank_journal(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'BNK', 'Bank', 'bank')).id",
        admin=str(tenants.admin_a),
    )


async def _receipt(
    tenants: SeededTenants,
    world: dict[str, uuid.UUID],
    *,
    bank: uuid.UUID,
    journal: uuid.UUID,
    party: uuid.UUID,
    amount: str,
) -> uuid.UUID:
    """`Dr bank / Cr Debiteuren`, through `ledger.post_entry` - the only way in."""
    lines = [
        {
            "account_id": str(bank),
            "debit": amount,
            "credit": "0.00",
            "subledger_party_id": None,
            "cost_centre_id": None,
            "description": "Betaling 2026-1",
            "vat_treatment": None,
        },
        {
            "account_id": str(world["receivable"]),
            "debit": "0.00",
            "credit": amount,
            "subledger_party_id": str(party),
            "cost_centre_id": None,
            "description": "Betaling 2026-1",
            "vat_treatment": None,
        },
    ]
    return await _scalar(
        tenants,
        "SELECT (ledger.post_entry("
        "  p_administration_id  => :admin,"
        "  p_journal_id         => :journal,"
        "  p_period_id          => :period,"
        "  p_entry_date         => DATE '2026-09-15',"
        "  p_description        => 'Betaling 2026-1',"
        "  p_document_reference => '2026-1',"
        "  p_posted_by_user_id  => :user,"
        "  p_source_system      => 'invoicing',"
        "  p_lines              => cast(:lines as jsonb)"
        ")).id",
        admin=str(tenants.admin_a),
        journal=str(journal),
        period=str(world["period"]),
        lines=json.dumps(lines),
        user=str(tenants.owner_a),
    )


async def _owed_invoice(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    """An issued invoice owing GROSS, with the bank and journal a payment needs."""
    world = await _world(tenants)
    party = await _party(tenants)
    invoice, _ = await _issued_and_posted(tenants, world, party)
    await _exec(
        tenants,
        "INSERT INTO sales_invoice_vat_total (invoice_id, organization_id, "
        "  administration_id, vat_treatment, rate, taxable_amount, vat_amount) "
        "VALUES (:invoice, :org, :admin, 'btw_21', 21, 1000.00, 210.00)",
        invoice=str(invoice),
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )
    return {
        **world,
        "party": party,
        "invoice": invoice,
        "bank": await _bank(tenants),
        "bank_journal": await _bank_journal(tenants),
    }


async def _pay(
    tenants: SeededTenants,
    owed: dict[str, uuid.UUID],
    amount: str,
    *,
    account: uuid.UUID | None = None,
) -> uuid.UUID:
    entry = await _receipt(
        tenants,
        owed,
        bank=owed["bank"],
        journal=owed["bank_journal"],
        party=owed["party"],
        amount=amount,
    )
    return await _scalar(
        tenants,
        "INSERT INTO sales_invoice_payment (organization_id, administration_id, "
        "  invoice_id, amount, paid_on, method, bank_account_id, journal_entry_id, "
        "  recorded_by_user_id) "
        "VALUES (:org, :admin, :invoice, :amount, DATE '2026-09-15', 'bank_transfer', "
        "  :bank, :entry, :user) RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(owed["invoice"]),
        amount=amount,
        bank=str(account or owed["bank"]),
        entry=str(entry),
        user=str(tenants.owner_a),
    )


async def _balance(tenants: SeededTenants, owed: dict[str, uuid.UUID]) -> Decimal:
    result = await _exec(
        tenants,
        "SELECT outstanding FROM invoicing.invoice_balances(:admin, :invoice)",
        admin=str(tenants.admin_a),
        invoice=str(owed["invoice"]),
    )
    return Decimal(result.scalar_one())


# --- what an invoice owes -------------------------------------------------------


async def test_an_unpaid_invoice_owes_its_gross(two_organizations: SeededTenants) -> None:
    owed = await _owed_invoice(two_organizations)
    assert await _balance(two_organizations, owed) == GROSS


async def test_payments_reduce_the_balance_and_the_subledger_falls_with_it(
    two_organizations: SeededTenants,
) -> None:
    """The point of posting through the ledger: the invoice's outstanding and the
    debtor's sub-ledger balance are two views of one set of entries, so they fall
    together and there is no second balance to drift."""
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")

    assert await _balance(two_organizations, owed) == Decimal("810.00")

    result = await _exec(
        two_organizations,
        "SELECT balance FROM ledger.subledger_balance(:admin, 'accounts_receivable') "
        " WHERE party_id = :party",
        admin=str(two_organizations.admin_a),
        party=str(owed["party"]),
    )
    # 1210.00 owed by the invoice's entry, less the 400.00 receipt.
    assert Decimal(result.scalar_one()) == Decimal("810.00")


# --- the overpayment guard ------------------------------------------------------


async def test_a_payment_above_the_outstanding_balance_is_refused(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, "1000.00")

    with pytest.raises(Exception, match="(?i)still outstanding|FR-BNK-005"):
        await _pay(two_organizations, owed, "210.01")


async def test_the_exact_remaining_balance_is_accepted(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, "1000.00")
    await _pay(two_organizations, owed, "210.00")

    assert await _balance(two_organizations, owed) == Decimal("0.00")


async def test_a_non_positive_amount_is_refused(two_organizations: SeededTenants) -> None:
    owed = await _owed_invoice(two_organizations)
    with pytest.raises(Exception, match="(?i)check|amount"):
        await _pay(two_organizations, owed, "0.00")


# --- what may be paid, and into what --------------------------------------------


async def test_a_draft_cannot_be_paid(two_organizations: SeededTenants) -> None:
    from tests.integration.test_sales_invoice_posting import _draft

    world = await _world(two_organizations)
    party = await _party(two_organizations)
    draft = await _draft(two_organizations, world)
    bank = await _bank(two_organizations)
    journal = await _bank_journal(two_organizations)
    entry = await _receipt(
        two_organizations, world, bank=bank, journal=journal, party=party, amount="10.00"
    )

    with pytest.raises(Exception, match="(?i)cannot be paid|only an issued"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_invoice_payment (organization_id, administration_id, "
            "  invoice_id, amount, paid_on, method, bank_account_id, journal_entry_id, "
            "  recorded_by_user_id) "
            "VALUES (:org, :admin, :invoice, 10.00, DATE '2026-09-15', 'cash', "
            "  :bank, :entry, :user)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            invoice=str(draft),
            bank=str(bank),
            entry=str(entry),
            user=str(two_organizations.owner_a),
        )


async def test_a_payment_cannot_land_in_the_receivable_itself(
    two_organizations: SeededTenants,
) -> None:
    """Debiting Debiteuren against Debiteuren would move nothing and settle
    nothing."""
    owed = await _owed_invoice(two_organizations)

    with pytest.raises(Exception, match="(?i)plain asset|control account"):
        await _pay(two_organizations, owed, "100.00", account=owed["receivable"])


async def test_a_payment_cannot_land_in_a_revenue_account(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)

    with pytest.raises(Exception, match="(?i)plain asset"):
        await _pay(two_organizations, owed, "100.00", account=owed["revenue"])


# --- immutability -----------------------------------------------------------------


async def test_a_payment_cannot_be_edited(two_organizations: SeededTenants) -> None:
    owed = await _owed_invoice(two_organizations)
    payment = await _pay(two_organizations, owed, "400.00")

    with pytest.raises(Exception, match="(?i)only voiding|record of money received"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_payment SET amount = 1.00 WHERE id = :id",
            id=str(payment),
        )


async def test_a_payment_can_be_voided_once_and_then_is_history(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)
    payment = await _pay(two_organizations, owed, "400.00")
    reversal = await _receipt(
        two_organizations,
        owed,
        bank=owed["bank"],
        journal=owed["bank_journal"],
        party=owed["party"],
        amount="1.00",
    )

    void = (
        "UPDATE sales_invoice_payment SET voided_at = now(), voided_by_user_id = :user, "
        "  void_journal_entry_id = :entry WHERE id = :id"
    )
    await _exec(
        two_organizations,
        void,
        user=str(two_organizations.owner_a),
        entry=str(reversal),
        id=str(payment),
    )
    # A voided payment stops counting: the invoice is owed again in full.
    assert await _balance(two_organizations, owed) == GROSS

    with pytest.raises(Exception, match="(?i)voided|history"):
        await _exec(
            two_organizations,
            void,
            user=str(two_organizations.owner_a),
            entry=str(reversal),
            id=str(payment),
        )


async def test_a_void_must_be_complete(two_organizations: SeededTenants) -> None:
    """`voided_at`, its user and its reversing entry are set together or not at
    all - a half-void would stop the payment counting with no reversal in the
    books."""
    owed = await _owed_invoice(two_organizations)
    payment = await _pay(two_organizations, owed, "400.00")

    with pytest.raises(Exception, match="(?i)void_is_complete|check"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_payment SET voided_at = now() WHERE id = :id",
            id=str(payment),
        )


async def test_the_application_role_cannot_delete_a_payment(
    two_organizations: SeededTenants,
) -> None:
    """CMP-009's posture: the record that money arrived is never removed."""
    owed = await _owed_invoice(two_organizations)
    payment = await _pay(two_organizations, owed, "400.00")

    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "DELETE FROM sales_invoice_payment WHERE id = :id",
            id=str(payment),
        )


# --- tenant isolation -------------------------------------------------------------


async def test_another_tenant_cannot_see_a_payment(two_organizations: SeededTenants) -> None:
    """RLS as the first line of defence (IAM-001): organization B's context reads
    none of organization A's payments, even selecting every row."""
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        seen = (await conn.execute(text("SELECT count(*) FROM sales_invoice_payment"))).scalar_one()
        balances = (
            await conn.execute(
                text("SELECT count(*) FROM invoicing.invoice_balances(:admin)"),
                {"admin": str(two_organizations.admin_a)},
            )
        ).scalar_one()

    assert seen == 0
    assert balances == 0


async def test_two_concurrent_payments_cannot_both_fit(two_organizations: SeededTenants) -> None:
    """The row lock, exercised rather than reasoned about.

    Two transactions each try to record 700.00 against an invoice owing 1210.00.
    Either alone fits; together they overpay. The first holds the invoice's row
    lock (uncommitted); the second must WAIT for it, and once the first commits
    must see that payment and be refused - not slip through on a stale balance.
    """
    import asyncio

    owed = await _owed_invoice(two_organizations)
    entries = [
        await _receipt(
            two_organizations,
            owed,
            bank=owed["bank"],
            journal=owed["bank_journal"],
            party=owed["party"],
            amount="700.00",
        )
        for _ in range(2)
    ]
    insert = text(
        "INSERT INTO sales_invoice_payment (organization_id, administration_id, invoice_id, "
        "  amount, paid_on, method, bank_account_id, journal_entry_id, recorded_by_user_id) "
        "VALUES (:org, :admin, :invoice, 700.00, DATE '2026-09-15', 'bank_transfer', "
        "  :bank, :entry, :user)"
    )

    def params(entry: uuid.UUID) -> dict[str, str]:
        return {
            "org": str(two_organizations.org_a),
            "admin": str(two_organizations.admin_a),
            "invoice": str(owed["invoice"]),
            "bank": str(owed["bank"]),
            "entry": str(entry),
            "user": str(two_organizations.owner_a),
        }

    async with app_engine.connect() as first, app_engine.connect() as second:
        for conn in (first, second):
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )

        await first.execute(insert, params(entries[0]))  # takes the invoice's row lock
        racing = asyncio.create_task(second.execute(insert, params(entries[1])))
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second insert should be waiting on the first's lock"

        await first.commit()
        with pytest.raises(Exception, match="(?i)still outstanding|FR-BNK-005"):
            await racing
        await second.rollback()

    assert await _balance(two_organizations, owed) == Decimal("510.00")  # 1210 - 700

"""Migration 0040 against a real Postgres - FR-GL-006, FR-TPL-017.

The properties here are the ones no fake can establish, because they are the
database's: that `ledgr_app` cannot write a posting itself, that the AR control
account refuses a line naming no party, that the sub-ledger reconciles to the
control account by construction, and that neither link on an issued invoice can
be moved once written.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


async def _exec(tenants: SeededTenants, sql: str, **params: object):  # type: ignore[no-untyped-def]
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        return await conn.execute(text(sql), params)


async def _scalar(tenants: SeededTenants, sql: str, **params: object) -> uuid.UUID:
    result = await _exec(tenants, sql, **params)
    return result.scalar_one()  # type: ignore[no-any-return]


async def _world(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    """A chart, a journal, an open period and the two mappings a sale needs."""
    admin, org = tenants.admin_a, tenants.org_a

    year = await _scalar(
        tenants,
        "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
        "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id",
        org=str(org),
        admin=str(admin),
    )
    period = await _scalar(
        tenants,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, "
        "  period_number, start_date, end_date, status) "
        "VALUES (:org, :admin, :year, 9, '2026-09-01', '2026-09-30', 'open') RETURNING id",
        org=str(org),
        admin=str(admin),
        year=str(year),
    )

    # Accounts are created through the ledger's own function - `ledgr_app` has
    # no INSERT on ledger_account, which is the point.
    receivable = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1300', 'Debiteuren', 'asset', "
        "  NULL, NULL, 'accounts_receivable')).id",
        admin=str(admin),
    )
    revenue = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '8000', 'Omzet hoog tarief', 'revenue', "
        "  NULL, 'btw_21', NULL)).id",
        admin=str(admin),
    )
    vat_out = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1500', 'Te betalen omzetbelasting', "
        "  'liability', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    journal = await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'VRK', 'Verkoopboek', 'sales')).id",
        admin=str(admin),
    )

    for purpose, treatment, account in (
        ("revenue", "btw_21", revenue),
        ("vat_output", None, vat_out),
    ):
        await _exec(
            tenants,
            "INSERT INTO sales_posting_account (organization_id, administration_id, "
            "  purpose, vat_treatment_key, account_id) "
            "VALUES (:org, :admin, :purpose, :treatment, :account)",
            org=str(org),
            admin=str(admin),
            purpose=purpose,
            treatment=treatment,
            account=str(account),
        )

    return {
        "year": year,
        "period": period,
        "receivable": receivable,
        "revenue": revenue,
        "vat_out": vat_out,
        "journal": journal,
    }


# --- the bounded context ------------------------------------------------------


async def test_the_application_role_cannot_write_a_posting(
    two_organizations: SeededTenants,
) -> None:
    """CLAUDE.md's first non-negotiable, as a privilege rather than a
    convention. Every other guarantee in this file rests on this one: if
    `ledgr_app` could INSERT here, none of the ledger's triggers would be on the
    only path in.
    """
    world = await _world(two_organizations)

    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "INSERT INTO journal_entry (organization_id, administration_id, "
            "  fiscal_year_id, period_id, journal_id, entry_date, description) "
            "VALUES (:org, :admin, :year, :period, :journal, '2026-09-09', 'x')",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            year=str(world["year"]),
            period=str(world["period"]),
            journal=str(world["journal"]),
        )


# --- FR-GL-006 ---------------------------------------------------------------


async def test_a_control_account_line_without_a_party_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The biconditional that makes the AR sub-ledger reconcile BY
    CONSTRUCTION: there is no unattributed bucket for a receivable to land in,
    so the control account's balance is always the sum of its parties'.
    """
    world = await _world(two_organizations)

    with pytest.raises(Exception, match="(?i)FR-GL-006|sub-ledger|party"):
        await _post(two_organizations, world, party_id=None)


async def test_a_posting_with_a_party_is_accepted(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    party = await _party(two_organizations)

    entry = await _post(two_organizations, world, party_id=party)
    assert entry is not None


async def test_the_subledger_reconciles_to_the_control_account(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-006's "reconciled to control accounts continuously". The rows ARE
    the control account's own lines grouped by party, so `difference` is
    structurally zero - a non-zero value would mean the trigger above is gone.
    """
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    await _post(two_organizations, world, party_id=party)

    result = await _exec(
        two_organizations,
        "SELECT control_kind, control_balance, subledger_balance, difference "
        "  FROM ledger.control_account_reconciliation(:admin)",
        admin=str(two_organizations.admin_a),
    )
    rows = result.fetchall()

    receivables = [r for r in rows if r.control_kind == "accounts_receivable"]
    assert receivables, "no AR reconciliation row"
    assert receivables[0].difference == Decimal("0.00")
    assert receivables[0].control_balance == Decimal("1210.00")


async def test_the_debtor_balance_is_the_invoice_gross(
    two_organizations: SeededTenants,
) -> None:
    """FR-AR-012 will read exactly this."""
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    await _post(two_organizations, world, party_id=party)

    result = await _exec(
        two_organizations,
        "SELECT party_name, balance FROM ledger.subledger_balance(:admin, 'accounts_receivable')",
        admin=str(two_organizations.admin_a),
    )
    rows = result.fetchall()

    assert len(rows) == 1
    assert rows[0].balance == Decimal("1210.00")


# --- 0040's own constraints ---------------------------------------------------


async def test_a_revenue_mapping_must_point_at_a_revenue_account(
    two_organizations: SeededTenants,
) -> None:
    """A revenue mapping pointing at an expense account would understate
    turnover and overstate costs by the same amount, leaving the trial balance
    perfectly balanced and both figures wrong.
    """
    world = await _world(two_organizations)
    expense_account = await _scalar(
        two_organizations,
        "SELECT (ledger.create_account(:admin, '4000', 'Kantoorkosten', 'expense', "
        "  NULL, NULL, NULL)).id",
        admin=str(two_organizations.admin_a),
    )

    with pytest.raises(Exception, match="(?i)revenue account"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_posting_account (organization_id, administration_id, "
            "  purpose, vat_treatment_key, account_id) "
            "VALUES (:org, :admin, 'revenue', 'btw_9', :account)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            account=str(expense_account),
        )
    assert world  # the fixture is what made the account creatable


async def test_two_fallback_rows_for_one_purpose_are_refused(
    two_organizations: SeededTenants,
) -> None:
    """Two mappings for one purpose would make the posting depend on which row
    the planner reached first.
    """
    world = await _world(two_organizations)

    with pytest.raises(Exception, match="(?i)duplicate key|unique"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_posting_account (organization_id, administration_id, "
            "  purpose, vat_treatment_key, account_id) "
            "VALUES (:org, :admin, 'vat_output', NULL, :account)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            account=str(world["vat_out"]),
        )


async def test_a_draft_cannot_carry_a_posting(two_organizations: SeededTenants) -> None:
    """`sales_invoice_draft_is_unposted`. A draft has asserted nothing and owes
    nobody, so a posting against one would be a receivable from a document that
    does not exist.
    """
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    entry = await _post(two_organizations, world, party_id=party)
    invoice = await _draft(two_organizations, world)

    with pytest.raises(Exception, match="(?i)check constraint"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice SET journal_entry_id = :entry, posted_at = now()  WHERE id = :id",
            entry=str(entry),
            id=str(invoice),
        )


async def test_the_entry_link_cannot_be_moved(two_organizations: SeededTenants) -> None:
    """`sales_invoice_posting_is_final_trg`. Repointing would leave a posted
    entry with nothing pointing at it and an invoice free to post again - a
    receivable recorded twice with no trace of the first.
    """
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    invoice, _ = await _issued_and_posted(two_organizations, world, party)

    other = await _post(two_organizations, world, party_id=party, gross="1.00")

    with pytest.raises(Exception, match="(?i)already posted|reversing entry"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice SET journal_entry_id = :entry WHERE id = :id",
            entry=str(other),
            id=str(invoice),
        )


async def test_the_document_link_cannot_be_moved(
    two_organizations: SeededTenants,
) -> None:
    """FR-TPL-017, as a trigger. The whole content of that requirement is that
    the bytes a customer received are the bytes that stay, and a second
    `document_id` is that undone.
    """
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    invoice, _ = await _issued_and_posted(two_organizations, world, party)

    with pytest.raises(Exception, match="(?i)FR-TPL-017|not re-rendered"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice SET document_id = NULL WHERE id = :id",
            id=str(invoice),
        )


async def test_one_entry_per_invoice(two_organizations: SeededTenants) -> None:
    """`sales_invoice_journal_entry_idx`. The double-count NFR-032 exists to
    prevent, arriving by a route idempotency keys do not cover because the two
    requests are minutes apart.
    """
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    _, entry = await _issued_and_posted(two_organizations, world, party)
    second = await _draft(two_organizations, world)
    await _exec(
        two_organizations,
        "UPDATE sales_invoice SET status = 'issued' WHERE id = :id",
        id=str(second),
    )

    with pytest.raises(Exception, match="(?i)duplicate key|unique"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice SET journal_entry_id = :entry, posted_at = now()  WHERE id = :id",
            entry=str(entry),
            id=str(second),
        )


async def test_a_customer_party_link_is_set_once(
    two_organizations: SeededTenants,
) -> None:
    """Repointing a customer at a different party would move receivables
    between debtors with no posting at all.
    """
    customer = await _customer(two_organizations)
    first = await _party(two_organizations)
    second = await _party(two_organizations, name="Andere B.V.")

    await _exec(
        two_organizations,
        "UPDATE customer SET subledger_party_id = :party WHERE id = :id",
        party=str(first),
        id=str(customer),
    )

    with pytest.raises(Exception, match="(?i)FR-GL-006|receivables between debtors"):
        await _exec(
            two_organizations,
            "UPDATE customer SET subledger_party_id = :party WHERE id = :id",
            party=str(second),
            id=str(customer),
        )


# --- the reports --------------------------------------------------------------


async def test_posting_of_reads_the_entry_back_through_the_link(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    party = await _party(two_organizations)
    invoice, _ = await _issued_and_posted(two_organizations, world, party)

    result = await _exec(
        two_organizations,
        "SELECT account_code, party_name, debit, credit, vat_treatment "
        "  FROM invoicing.posting_of(:id)",
        id=str(invoice),
    )
    rows = result.fetchall()

    assert [r.account_code for r in rows] == ["1300", "8000", "1500"]
    assert rows[0].party_name == "De Vries Holding B.V."
    assert rows[0].debit == Decimal("1210.00")
    assert rows[1].vat_treatment == "btw_21"


async def test_unposted_invoices_finds_an_issued_invoice_with_no_entry(
    two_organizations: SeededTenants,
) -> None:
    """Structurally empty while `issue` stays atomic. The report exists for the
    same reason 0020's gap report does: a check that should always be empty is
    the check on the thing that makes it empty.
    """
    world = await _world(two_organizations)
    invoice = await _draft(two_organizations, world)
    await _exec(
        two_organizations,
        "UPDATE sales_invoice SET status = 'issued' WHERE id = :id",
        id=str(invoice),
    )

    result = await _exec(
        two_organizations,
        "SELECT invoice_id, has_document FROM invoicing.unposted_invoices(:admin)",
        admin=str(two_organizations.admin_a),
    )
    rows = result.fetchall()

    assert [r.invoice_id for r in rows] == [invoice]
    assert rows[0].has_document is False


# --- helpers ------------------------------------------------------------------


async def _party(tenants: SeededTenants, name: str = "De Vries Holding B.V.") -> uuid.UUID:
    return await _scalar(
        tenants,
        "SELECT (ledger.create_party(:admin, 'customer', :name, NULL)).id",
        admin=str(tenants.admin_a),
        name=name,
    )


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, "
        "  delivery_channel, invoice_email) "
        "VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


async def _post(
    tenants: SeededTenants,
    world: dict[str, uuid.UUID],
    *,
    party_id: uuid.UUID | None,
    gross: str = "1210.00",
) -> uuid.UUID:
    """One sales entry, through `ledger.post_entry` - the only way in."""
    net = Decimal(gross) / Decimal("1.21")
    net = net.quantize(Decimal("0.01"))
    vat = Decimal(gross) - net

    lines = [
        {
            "account_id": str(world["receivable"]),
            "debit": gross,
            "credit": "0.00",
            "subledger_party_id": str(party_id) if party_id else None,
            "cost_centre_id": None,
            "description": "2026-1",
            "vat_treatment": None,
        },
        {
            "account_id": str(world["revenue"]),
            "debit": "0.00",
            "credit": f"{net:.2f}",
            "subledger_party_id": None,
            "cost_centre_id": None,
            "description": "2026-1",
            "vat_treatment": "btw_21",
        },
        {
            "account_id": str(world["vat_out"]),
            "debit": "0.00",
            "credit": f"{vat:.2f}",
            "subledger_party_id": None,
            "cost_centre_id": None,
            "description": "2026-1",
            "vat_treatment": "btw_21",
        },
    ]

    import json

    # Named notation rather than positional: `ledger.post_entry` takes eleven
    # arguments and puts `p_lines` ninth, after the document reference and the
    # actor. Getting that order wrong positionally produces a type error at
    # best and a silently mis-assigned argument at worst.
    return await _scalar(
        tenants,
        "SELECT (ledger.post_entry("
        "  p_administration_id  => :admin,"
        "  p_journal_id         => :journal,"
        "  p_period_id          => :period,"
        "  p_entry_date         => DATE '2026-09-09',"
        "  p_description        => '2026-1 - De Vries Holding B.V.',"
        "  p_document_reference => '2026-1',"
        "  p_posted_by_user_id  => :user,"
        "  p_source_system      => 'invoicing',"
        "  p_lines              => cast(:lines as jsonb)"
        ")).id",
        admin=str(tenants.admin_a),
        journal=str(world["journal"]),
        period=str(world["period"]),
        lines=json.dumps(lines),
        user=str(tenants.owner_a),
    )


async def _draft(tenants: SeededTenants, world: dict[str, uuid.UUID]) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO sales_invoice (organization_id, administration_id, fiscal_year_id, "
        "  invoice_date, customer_name, customer_address, customer_country, "
        "  customer_language) "
        "VALUES (:org, :admin, :year, '2026-09-09', 'De Vries Holding B.V.', "
        "  'Damrak 70', 'NL', 'nl') RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        year=str(world["year"]),
    )


async def _issued_and_posted(
    tenants: SeededTenants, world: dict[str, uuid.UUID], party: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    invoice = await _draft(tenants, world)
    entry = await _post(tenants, world, party_id=party)
    document = await _document(tenants, world)

    await _exec(
        tenants,
        "UPDATE sales_invoice SET status = 'issued' WHERE id = :id",
        id=str(invoice),
    )
    await _exec(
        tenants,
        "UPDATE sales_invoice SET journal_entry_id = :entry, document_id = :document, "
        "  posted_at = now() WHERE id = :id",
        entry=str(entry),
        document=str(document),
        id=str(invoice),
    )
    return invoice, entry


async def _document(tenants: SeededTenants, world: dict[str, uuid.UUID]) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO document (organization_id, administration_id, fiscal_year_id, "
        "  storage_key, content_hash, byte_size, content_type, retention_basis, "
        "  scan_status, scanned_at, scanner) "
        "VALUES (:org, :admin, :year, :key, decode(repeat('ab', 32), 'hex'), 1024, "
        "  'application/pdf', 'standard', 'clean', now(), 'test') RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        year=str(world["year"]),
        key=f"test/{uuid.uuid4()}",
    )

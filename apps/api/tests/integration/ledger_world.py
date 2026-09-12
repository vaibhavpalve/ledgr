"""Builds a World over a real Postgres with migration 0020 applied.

The counterpart of tests/ledger/fake_world.py. Same World, same fixtures,
same names - which is what lets tests/ledger/cases.py execute unchanged
against either backing, and is how a drift between the in-memory fake and the
database becomes a failure instead of a silent divergence.

Everything is created the way production creates it: master data through the
`ledger.*` functions (there is no other way - `ledgr_app` has no INSERT on
those tables), fiscal years and periods through ordinary tenant-scoped
INSERTs, since those are 0001's tables and not the ledger's.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from api.ledger.model import EntryInput
from tests.ledger.world import World
from tests.support.seed import SeededTenants

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


async def post_entry(conn: AsyncConnection, candidate: EntryInput) -> uuid.UUID:
    """Calls the narrow API with an EntryInput, so a case table's entries
    reach Postgres unchanged.

    Amounts arrive as decimal strings (model.amount_to_string). That is not
    cosmetic: ledger.post_entry rejects a JSON number outright, which is
    NFR-031's tripwire at the database boundary.
    """
    result = await conn.execute(
        text(
            "SELECT id FROM ledger.post_entry("
            "  p_administration_id  => :administration_id,"
            "  p_journal_id         => :journal_id,"
            "  p_period_id          => :period_id,"
            "  p_entry_date         => cast(:entry_date as date),"
            "  p_description        => :description,"
            "  p_document_reference => :document_reference,"
            "  p_posted_by_user_id  => :posted_by_user_id,"
            "  p_source_system      => :source_system,"
            "  p_lines              => cast(:lines as jsonb),"
            "  p_reverses_entry_id  => :reverses_entry_id,"
            "  p_idempotency_key    => :idempotency_key)"
        ),
        {
            "administration_id": str(candidate.administration_id),
            "journal_id": str(candidate.journal_id),
            "period_id": str(candidate.period_id),
            "entry_date": candidate.entry_date,
            "description": candidate.description,
            "document_reference": candidate.document_reference,
            "posted_by_user_id": (
                str(candidate.posted_by_user_id) if candidate.posted_by_user_id else None
            ),
            "source_system": candidate.source_system,
            "lines": json.dumps(candidate.as_line_payload()),
            "reverses_entry_id": (
                str(candidate.reverses_entry_id) if candidate.reverses_entry_id else None
            ),
            "idempotency_key": candidate.idempotency_key,
        },
    )
    return result.scalar_one()  # type: ignore[no-any-return]


async def _scalar(conn: AsyncConnection, sql: str, **params: object) -> uuid.UUID:
    result = await conn.execute(text(sql), params)
    return result.scalar_one()  # type: ignore[no-any-return]


async def _fiscal_year(conn: AsyncConnection, *, org: uuid.UUID, admin: uuid.UUID) -> uuid.UUID:
    return await _scalar(
        conn,
        "INSERT INTO fiscal_year (organization_id, administration_id, "
        "start_date, end_date) VALUES (:org, :admin, '2026-01-01', '2026-12-31') "
        "RETURNING id",
        org=str(org),
        admin=str(admin),
    )


async def _period(
    conn: AsyncConnection,
    *,
    org: uuid.UUID,
    admin: uuid.UUID,
    year: uuid.UUID,
    number: int,
    start: date,
    end: date,
    status: str = "open",
) -> uuid.UUID:
    # Inserted with its final status rather than inserted-then-updated:
    # period_status_transition_trg guards UPDATE, and seeding a vat_filed
    # period by transitioning into it would exercise the guard rather than the
    # fixture. INSERT is not a transition.
    return await _scalar(
        conn,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, "
        "period_number, start_date, end_date, status) "
        "VALUES (:org, :admin, :year, :number, :start, :end, :status) RETURNING id",
        org=str(org),
        admin=str(admin),
        year=str(year),
        number=number,
        start=start,
        end=end,
        status=status,
    )


async def _account(
    conn: AsyncConnection,
    *,
    admin: uuid.UUID,
    code: str,
    name: str,
    account_type: str,
    control_kind: str | None = None,
) -> uuid.UUID:
    return await _scalar(
        conn,
        "SELECT id FROM ledger.create_account("
        "  p_administration_id => :admin, p_code => :code, p_name => :name,"
        "  p_account_type => :account_type, p_control_kind => :control_kind)",
        admin=str(admin),
        code=code,
        name=name,
        account_type=account_type,
        control_kind=control_kind,
    )


async def _journal(
    conn: AsyncConnection, *, admin: uuid.UUID, code: str, name: str, kind: str
) -> uuid.UUID:
    return await _scalar(
        conn,
        "SELECT id FROM ledger.create_journal(:admin, :code, :name, :kind)",
        admin=str(admin),
        code=code,
        name=name,
        kind=kind,
    )


async def _party(conn: AsyncConnection, *, admin: uuid.UUID, kind: str, name: str) -> uuid.UUID:
    return await _scalar(
        conn,
        "SELECT id FROM ledger.create_party(:admin, :kind, :name, null)",
        admin=str(admin),
        kind=kind,
        name=name,
    )


async def build_db_world(conn: AsyncConnection, tenants: SeededTenants) -> World:
    """Seeds org A's administration plus a SECOND administration in the same
    organization.

    The second one is what makes FR-GL-002's "belongs to this administration"
    cases meaningful. An id from another ORGANIZATION would be invisible under
    RLS and the case would pass on tenant isolation instead of on the
    constraint under test - a green test proving the wrong thing.
    """
    org = tenants.org_a
    admin = tenants.admin_a

    # is_local = FALSE, so the tenant survives a commit.
    #
    # Production uses transaction-local context deliberately (ADR-003): a
    # pooled connection must not carry one request's tenant into the next. A
    # test connection is not pooled and belongs to one tenant for its whole
    # life, and almost every caller here commits partway through and keeps
    # going - at which point a transaction-local setting is gone and the next
    # statement reads an empty tenant. RLS then fails closed and the failure
    # arrives as `period ... does not exist` from inside a trigger, which says
    # nothing about the tenant context that actually went missing.
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, false)"),
        {"org": str(org)},
    )

    other_admin = await _scalar(
        conn,
        "INSERT INTO administration (organization_id, legal_name, legal_form) "
        "VALUES (:org, 'Tweede Administratie B.V.', 'BV') RETURNING id",
        org=str(org),
    )

    year = await _fiscal_year(conn, org=org, admin=admin)
    other_year = await _fiscal_year(conn, org=org, admin=other_admin)

    open_period = await _period(
        conn,
        org=org,
        admin=admin,
        year=year,
        number=1,
        start=PERIOD_START,
        end=PERIOD_END,
    )
    locked_period = await _period(
        conn,
        org=org,
        admin=admin,
        year=year,
        number=2,
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        status="locked",
    )
    filed_period = await _period(
        conn,
        org=org,
        admin=admin,
        year=year,
        number=3,
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        status="vat_filed",
    )
    other_period = await _period(
        conn,
        org=org,
        admin=other_admin,
        year=other_year,
        number=1,
        start=PERIOD_START,
        end=PERIOD_END,
    )

    journal = await _journal(conn, admin=admin, code="MEM", name="Memoriaal", kind="memorial")
    blocked_journal = await _journal(conn, admin=admin, code="OLD", name="Retired", kind="memorial")
    await conn.execute(
        text("SELECT id FROM ledger.set_journal_status(:id, 'blocked')"),
        {"id": str(blocked_journal)},
    )
    other_journal = await _journal(
        conn, admin=other_admin, code="MEM", name="Memoriaal", kind="memorial"
    )

    cash = await _account(conn, admin=admin, code="1000", name="Kas", account_type="asset")
    revenue = await _account(conn, admin=admin, code="8000", name="Omzet", account_type="revenue")
    blocked = await _account(
        conn, admin=admin, code="9999", name="Vervallen", account_type="expense"
    )
    await conn.execute(
        text("SELECT id FROM ledger.set_account_status(:id, 'blocked')"),
        {"id": str(blocked)},
    )
    receivables = await _account(
        conn,
        admin=admin,
        code="1300",
        name="Debiteuren",
        account_type="asset",
        control_kind="accounts_receivable",
    )
    payables = await _account(
        conn,
        admin=admin,
        code="1600",
        name="Crediteuren",
        account_type="liability",
        control_kind="accounts_payable",
    )
    other_account = await _account(
        conn, admin=other_admin, code="1000", name="Kas", account_type="asset"
    )

    customer = await _party(conn, admin=admin, kind="customer", name="Jansen B.V.")
    supplier = await _party(conn, admin=admin, kind="supplier", name="De Vries Groothandel")

    return World(
        administration_id=admin,
        fiscal_year_id=year,
        actor_user_id=tenants.owner_a,
        open_period_id=open_period,
        locked_period_id=locked_period,
        vat_filed_period_id=filed_period,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        journal_id=journal,
        blocked_journal_id=blocked_journal,
        cash_account_id=cash,
        revenue_account_id=revenue,
        blocked_account_id=blocked,
        receivables_control_id=receivables,
        payables_control_id=payables,
        customer_party_id=customer,
        supplier_party_id=supplier,
        other_administration_id=other_admin,
        other_journal_id=other_journal,
        other_period_id=other_period,
        other_account_id=other_account,
    )

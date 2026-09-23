"""The sales invoice list's per-row facts, against real Postgres.

`SqlInvoiceRepository.list_facts` reads `invoicing.receivable_items`, the
payments and the deliveries for a whole page in three queries. The unit tests
drive a fake; this is the one that proves the SQL itself - what is owed, the
latest unvoided payment date, the latest delivery - and that another
administration's invoice id yields nothing (IAM-005).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.invoicing.repository import SqlInvoiceRepository
from tests.integration.test_sales_invoice_payment import GROSS, _owed_invoice, _pay
from tests.integration.test_sales_invoice_posting import _exec
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

TODAY = date(2026, 9, 24)


async def _facts(tenants: SeededTenants, admin: uuid.UUID, invoice: uuid.UUID):  # type: ignore[no-untyped-def]
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        repository = SqlInvoiceRepository(AsyncSession(bind=conn))
        return await repository.list_facts(
            administration_id=admin, invoice_ids=[invoice], as_of=TODAY
        )


async def test_an_unpaid_invoice_owes_its_gross_and_has_no_payment_or_delivery(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)

    facts = (await _facts(two_organizations, two_organizations.admin_a, owed["invoice"]))[
        owed["invoice"]
    ]

    assert facts.outstanding == GROSS
    assert facts.paid == Decimal("0.00")
    assert facts.last_paid_on is None
    assert facts.delivery_channel is None


async def test_a_paid_invoice_owes_nothing_and_reports_its_payment_date_and_delivery(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, str(GROSS))
    await _exec(
        two_organizations,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at) "
        "VALUES (:org, :admin, :invoice, 'email', 'sent', 'klant@example.nl', 'nl', now())",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
        invoice=str(owed["invoice"]),
    )

    facts = (await _facts(two_organizations, two_organizations.admin_a, owed["invoice"]))[
        owed["invoice"]
    ]

    # Nothing owed: `receivable_items` leaves the invoice out, so no outstanding.
    assert facts.outstanding is None
    assert facts.last_paid_on == date(2026, 9, 15)
    assert (facts.delivery_channel, facts.delivery_status) == ("email", "sent")


async def test_another_administration_gets_no_facts_for_this_invoice(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed_invoice(two_organizations)
    await _pay(two_organizations, owed, str(GROSS))

    # Same organization context, the OTHER administration's id: the
    # administration predicate on every read is the second line of defence.
    facts = (await _facts(two_organizations, two_organizations.admin_b, owed["invoice"]))[
        owed["invoice"]
    ]

    assert facts.outstanding is None
    assert facts.last_paid_on is None
    assert facts.delivery_channel is None

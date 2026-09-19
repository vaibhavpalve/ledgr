"""SI-12 against a real Postgres: the duplicate check never reads across tenants.

SI-12 adds no endpoint - its warnings ride on `GET .../sales-invoices/{id}`, whose
isolation tests are in test_sales_invoice_isolation.py. What it adds is a NEW
QUERY, `SqlInvoiceRepository.duplicate_candidates`, and that is the data path
IAM-001 says must carry a tenant predicate.

The scenario is the worst case for it: both organizations hold an invoice for a
customer of the SAME NAME, on the SAME date, with the SAME lines - exactly what
the check would match if it read across the boundary. The candidate pool asked
for as organization A must contain A's invoice and never B's, and the reverse.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.invoicing.repository import SqlInvoiceRepository
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

DAY = date(2026, 9, 9)
CUSTOMER = "De Vries Holding B.V."


async def _exec(org: uuid.UUID, sql: str, **params: object):  # type: ignore[no-untyped-def]
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        return await conn.execute(text(sql), params)


async def _seed_invoice(org: uuid.UUID, admin: uuid.UUID) -> uuid.UUID:
    """A draft for CUSTOMER dated DAY with one 10 x 95.00 line, in `org`."""
    year = (
        await _exec(
            org,
            "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
            "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id",
            org=str(org),
            admin=str(admin),
        )
    ).scalar_one()
    invoice = (
        await _exec(
            org,
            "INSERT INTO sales_invoice (organization_id, administration_id, fiscal_year_id, "
            "  invoice_date, customer_name, customer_address, customer_country, "
            "  customer_language) "
            "VALUES (:org, :admin, :year, :day, :customer, 'Damrak 70', 'NL', 'nl') "
            "RETURNING id",
            org=str(org),
            admin=str(admin),
            year=str(year),
            day=DAY,
            customer=CUSTOMER,
        )
    ).scalar_one()
    await _exec(
        org,
        "INSERT INTO sales_invoice_line (organization_id, administration_id, invoice_id, "
        "  position, description, quantity, unit_price, discount_percent, vat_treatment) "
        "VALUES ((SELECT organization_id FROM sales_invoice WHERE id = :invoice), "
        "  :admin, :invoice, 1, 'Consultancy', 10, 95.00, 0, 'btw_21')",
        admin=str(admin),
        invoice=str(invoice),
    )
    return invoice  # type: ignore[no-any-return]


async def _candidates(org: uuid.UUID, admin: uuid.UUID, *, exclude: uuid.UUID) -> set[uuid.UUID]:
    """What the repository returns when asked as `org`, for `admin`."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        pool = await SqlInvoiceRepository(AsyncSession(bind=conn)).duplicate_candidates(
            administration_id=admin,
            exclude_invoice_id=exclude,
            customer_id=None,
            customer_name=CUSTOMER,
            since=date(2026, 8, 1),
            until=date(2026, 10, 31),
        )
    return {candidate.invoice_id for candidate in pool}


async def test_the_candidate_pool_never_contains_another_tenants_invoice(
    two_organizations: SeededTenants,
) -> None:
    a_first = await _seed_invoice(two_organizations.org_a, two_organizations.admin_a)
    b_only = await _seed_invoice(two_organizations.org_b, two_organizations.admin_b)
    # A second invoice in A so there is something in A's pool to find.
    a_second = (
        await _exec(
            two_organizations.org_a,
            "INSERT INTO sales_invoice (organization_id, administration_id, fiscal_year_id, "
            "  invoice_date, customer_name, customer_address, customer_country, "
            "  customer_language) "
            "SELECT organization_id, administration_id, fiscal_year_id, invoice_date, "
            "  customer_name, customer_address, customer_country, customer_language "
            "FROM sales_invoice WHERE id = :first RETURNING id",
            first=str(a_first),
        )
    ).scalar_one()

    as_a = await _candidates(two_organizations.org_a, two_organizations.admin_a, exclude=a_first)
    as_b = await _candidates(two_organizations.org_b, two_organizations.admin_b, exclude=b_only)

    assert as_a == {a_second}
    assert b_only not in as_a
    # B has only the invoice it is asking about, so its own pool is empty - and
    # in particular contains neither of A's identical invoices.
    assert as_b == set()


async def test_asking_as_one_tenant_about_anothers_administration_finds_nothing(
    two_organizations: SeededTenants,
) -> None:
    """Even naming B's administration id, organization A's context sees no row:
    RLS is the first line and the `administration_id` predicate the second."""
    await _seed_invoice(two_organizations.org_a, two_organizations.admin_a)
    b_invoice = await _seed_invoice(two_organizations.org_b, two_organizations.admin_b)

    leaked = await _candidates(
        two_organizations.org_a, two_organizations.admin_b, exclude=uuid.uuid4()
    )

    assert b_invoice not in leaked
    assert leaked == set()

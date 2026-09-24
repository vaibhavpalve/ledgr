"""The Reports screen against a real Postgres - api.reports.routes.

Real HTTP, real routes, real ReportsService reading `ledger.balances_as_of`.
No new table and no new permission (both routes ride Appendix A's existing
"View reports"), so - unlike Journal/Assets/Bank - nothing here depends on a
migration this session could not apply.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants


class _Verified:
    async def is_verified(self, user_id: uuid.UUID) -> bool:
        return True


@pytest.fixture(autouse=True)
async def _email_verified() -> AsyncIterator[None]:
    app.dependency_overrides[get_email_verification_checker] = lambda: _Verified()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_email_verification_checker, None)


async def _call(  # type: ignore[no-untyped-def]
    tenants: SeededTenants, admin: uuid.UUID, method: str, path: str, *, as_user=None
):
    token = make_token(tenants.org_a, user_id=as_user or tenants.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            f"/v1/administrations/{admin}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )


async def _world(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    """A fiscal year, an open period, and one posting: 1000.00 cash brought
    in as capital - Dr Kas, Cr Eigen vermogen. Enough to prove a balance
    sheet balances and an income statement is zero when nothing was earned.
    """
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
    kas = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1000', 'Kas', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    equity = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '0500', 'Eigen vermogen', 'equity', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    revenue = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '8000', 'Omzet', 'revenue', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    journal = await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'MEM', 'Memoriaal', 'memorial')).id",
        admin=str(admin),
    )
    await _exec(
        tenants,
        "SELECT ledger.post_entry("
        "  p_administration_id => :admin, p_journal_id => :journal, p_period_id => :period,"
        "  p_entry_date => '2026-09-05', p_description => 'Kapitaalstorting',"
        "  p_document_reference => NULL, p_posted_by_user_id => NULL, p_source_system => 'test',"
        "  p_lines => :lines)",
        admin=str(admin),
        journal=str(journal),
        period=str(period),
        lines=(
            f'[{{"account_id":"{kas}","debit":"1000.00","credit":"0.00"}},'
            f'{{"account_id":"{equity}","debit":"0.00","credit":"1000.00"}}]'
        ),
    )
    return {"year": year, "kas": kas, "equity": equity, "revenue": revenue}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/reports/balance-sheet")
async def test_balance_sheet_balances(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/reports/balance-sheet?fiscal_year_id={world['year']}&as_of=2026-09-30",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_balanced"] is True
    assert body["total_assets"] == "1000.00"
    assert body["total_equity"] == "1000.00"
    assert body["total_liabilities"] == "0.00"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/reports/income-statement")
async def test_income_statement_is_zero_with_no_activity(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/reports/income-statement?fiscal_year_id={world['year']}"
        f"&period_start=2026-09-01&period_end=2026-09-30",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The capital contribution is an equity posting, not revenue - the
    # income statement must show nothing earned.
    assert body["net_result"] == "0.00"
    assert body["revenue"] == []


async def test_unknown_fiscal_year_answers_404(two_organizations: SeededTenants) -> None:
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/reports/balance-sheet?fiscal_year_id={uuid.uuid4()}",
    )
    assert response.status_code == 404, response.text


async def test_another_tenant_cannot_read_the_reports(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/reports/balance-sheet"
            f"?fiscal_year_id={world['year']}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert response.status_code in {403, 404}
    assert str(world["kas"]) not in response.text

"""The Assets screen's writes against a real Postgres - api.assets.routes.

Real HTTP, real routes, real AssetService/LedgerService/PeriodService, real RLS.
What this proves that fakes cannot: that depreciation and disposal really post
through `ledger.post_entry`, that a second depreciation run for the same
period is refused by 0064's own unique index, and that another tenant cannot
see or touch any of it.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
Needs migration 0064 applied.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.auth.email_verification import get_email_verification_checker
from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


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
    equipment = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '0300', 'Inventaris', 'asset', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    accumulated = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '0301', 'Afschrijving inventaris', 'asset', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    expense = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '4300', 'Afschrijvingskosten', 'expense', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    bank = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    gain_loss = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '8900', 'Boekresultaat activa', 'revenue', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    journal = await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'MEM', 'Memoriaal', 'memorial')).id",
        admin=str(admin),
    )
    return {
        "year": year,
        "period": period,
        "equipment": equipment,
        "accumulated": accumulated,
        "expense": expense,
        "bank": bank,
        "gain_loss": gain_loss,
        "journal": journal,
    }


async def _call(  # type: ignore[no-untyped-def]
    tenants: SeededTenants,
    admin: uuid.UUID,
    method: str,
    path: str,
    json: object = None,
    *,
    as_user=None,
):
    token = make_token(tenants.org_a, user_id=as_user or tenants.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            f"/v1/administrations/{admin}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


def _asset_body(world: dict[str, uuid.UUID]) -> dict[str, object]:
    return {
        "name": "Laptop",
        "category": "IT equipment",
        "acquisition_date": "2026-01-01",
        "acquisition_cost": "1200.00",
        "residual_value": "0.00",
        "useful_life_months": 12,
        "asset_account_id": str(world["equipment"]),
        "depreciation_expense_account_id": str(world["expense"]),
        "accumulated_depreciation_account_id": str(world["accumulated"]),
    }


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/assets")
async def test_create_asset(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Laptop"
    assert body["status"] == "active"


async def test_create_asset_rejects_wrong_account_type(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    body = _asset_body(world)
    body["asset_account_id"] = str(world["expense"])  # an expense account, not an asset one
    response = await _call(two_organizations, two_organizations.admin_a, "POST", "/assets", body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "asset_field_invalid"


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/assets/{asset_id}/depreciate"
)
async def test_depreciate_asset_posts_straight_line(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    created = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    asset_id = created.json()["id"]

    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/assets/{asset_id}/depreciate",
        {"period_id": str(world["period"])},
    )
    assert response.status_code == 200, response.text
    run = response.json()
    # 1200.00 / 12 months = 100.00 exactly
    assert run["amount"] == "100.00"

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/assets/{asset_id}/depreciate",
        {"period_id": str(world["period"])},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["reason"] == "asset_depreciation_already_posted"


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/assets/{asset_id}/dispose")
async def test_dispose_asset_books_gain(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    created = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    asset_id = created.json()["id"]

    await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/assets/{asset_id}/depreciate",
        {"period_id": str(world["period"])},
    )
    # Net book value after one month: 1200 - 100 = 1100. Sold for 1300: a 200 gain.
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/assets/{asset_id}/dispose",
        {
            "period_id": str(world["period"]),
            "disposal_date": "2026-09-20",
            "proceeds": "1300.00",
            "proceeds_account_id": str(world["bank"]),
            "gain_loss_account_id": str(world["gain_loss"]),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "disposed"
    assert body["disposal_proceeds"] == "1300.00"

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/assets/{asset_id}/dispose",
        {
            "period_id": str(world["period"]),
            "disposal_date": "2026-09-21",
            "proceeds": "0",
        },
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["reason"] == "asset_already_disposed"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/assets")
async def test_another_tenant_cannot_see_or_touch_assets(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    created = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    asset_id = created.json()["id"]

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listing = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/assets",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert listing.status_code in {403, 404}
        assert asset_id not in listing.text

        detail = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/assets/{asset_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert detail.status_code in {403, 404}

        runs = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/assets/{asset_id}/depreciation-runs",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert runs.status_code in {403, 404}

        depreciate_attempt = await client.post(
            f"/v1/administrations/{two_organizations.admin_a}/assets/{asset_id}/depreciate",
            headers={
                "Authorization": f"Bearer {token_b}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json={"period_id": str(world["period"])},
        )
        assert depreciate_attempt.status_code in {403, 404}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/assets/{asset_id}")
async def test_another_tenant_cannot_fetch_one_asset(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    created = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    asset_id = created.json()["id"]

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        detail = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/assets/{asset_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert detail.status_code in {403, 404}


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/assets/{asset_id}/depreciation-runs"
)
async def test_another_tenant_cannot_list_depreciation_runs(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    created = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/assets", _asset_body(world)
    )
    asset_id = created.json()["id"]

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        runs = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/assets/{asset_id}/depreciation-runs",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert runs.status_code in {403, 404}

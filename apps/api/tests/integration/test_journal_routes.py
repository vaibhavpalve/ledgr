"""The Journal screen's writes against a real Postgres - api.ledger.routes.

Real HTTP, real routes, real LedgerService/PeriodService, real RLS. What this
proves that fakes cannot: that a manual entry really posts through
`ledger.post_entry`, that a reversal really binds through
`ledger.reverse_entry`, that period locking really blocks a posting, and that
another tenant cannot see or touch any of it.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
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
    """A fiscal year, two open periods, a memorial journal and two accounts -
    enough for a hand-typed, balanced two-line entry.
    """
    admin, org = tenants.admin_a, tenants.org_a

    year = await _scalar(
        tenants,
        "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
        "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id",
        org=str(org),
        admin=str(admin),
    )
    period_a = await _scalar(
        tenants,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, "
        "  period_number, start_date, end_date, status) "
        "VALUES (:org, :admin, :year, 9, '2026-09-01', '2026-09-30', 'open') RETURNING id",
        org=str(org),
        admin=str(admin),
        year=str(year),
    )
    period_b = await _scalar(
        tenants,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, "
        "  period_number, start_date, end_date, status) "
        "VALUES (:org, :admin, :year, 10, '2026-10-01', '2026-10-31', 'open') RETURNING id",
        org=str(org),
        admin=str(admin),
        year=str(year),
    )
    kas = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1000', 'Kas', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    diversen = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '4900', 'Diversen', 'expense', "
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
        "period_a": period_a,
        "period_b": period_b,
        "kas": kas,
        "diversen": diversen,
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


def _entry_body(world: dict[str, uuid.UUID], *, period_key: str = "period_a") -> dict[str, object]:
    return {
        "journal_id": str(world["journal"]),
        "period_id": str(world[period_key]),
        "entry_date": "2026-09-15" if period_key == "period_a" else "2026-10-15",
        "description": "Kascorrectie",
        "lines": [
            {"account_id": str(world["kas"]), "debit": "50.00"},
            {"account_id": str(world["diversen"]), "credit": "50.00"},
        ],
    }


# --- posting -------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/journal-entries")
async def test_post_manual_journal_entry(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/journal-entries", _entry_body(world)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry_number"] >= 1
    assert body["description"] == "Kascorrectie"
    debits = {line["account_id"]: line["debit"] for line in body["lines"]}
    assert debits[str(world["kas"])] == "50.00"


async def test_unbalanced_entry_is_refused(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    body = {
        "journal_id": str(world["journal"]),
        "period_id": str(world["period_a"]),
        "entry_date": "2026-09-15",
        "description": "Kascorrectie",
        "lines": [
            {"account_id": str(world["kas"]), "debit": "50.00"},
            {"account_id": str(world["diversen"]), "credit": "40.00"},
        ],
    }
    response = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/journal-entries", body
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "journal_entry_invalid"


async def test_locked_period_refuses_a_posting(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    lock = await _call(
        two_organizations, two_organizations.admin_a, "POST", f"/periods/{world['period_a']}/lock"
    )
    assert lock.status_code == 200, lock.text

    response = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/journal-entries", _entry_body(world)
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "period_invalid"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/journals")
async def test_another_tenant_cannot_see_journals_or_post(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    posted = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/journal-entries", _entry_body(world)
    )
    assert posted.status_code == 200, posted.text
    entry_id = posted.json()["id"]

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Reading admin A's journals/entries from org B's identity: 403 or 404,
        # never the journal itself or its posted entry.
        journals = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/journals",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert journals.status_code in {403, 404}
        assert str(world["journal"]) not in journals.text

        post_attempt = await client.post(
            f"/v1/administrations/{two_organizations.admin_a}/journal-entries",
            headers={
                "Authorization": f"Bearer {token_b}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=_entry_body(world),
        )
        assert post_attempt.status_code in {403, 404}

        reverse_attempt = await client.post(
            f"/v1/administrations/{two_organizations.admin_a}/journal-entries/{entry_id}/reverse",
            headers={
                "Authorization": f"Bearer {token_b}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json={"period_id": str(world["period_a"]), "entry_date": "2026-09-16"},
        )
        assert reverse_attempt.status_code in {403, 404}


# --- reversal --------------------------------------------------------------


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/journal-entries/{entry_id}/reverse",
)
async def test_reverse_journal_entry(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    posted = await _call(
        two_organizations, two_organizations.admin_a, "POST", "/journal-entries", _entry_body(world)
    )
    entry_id = posted.json()["id"]

    reversal = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/journal-entries/{entry_id}/reverse",
        {"period_id": str(world["period_a"]), "entry_date": "2026-09-16"},
    )
    assert reversal.status_code == 200, reversal.text
    body = reversal.json()
    assert body["reverses_entry_id"] == entry_id
    debits = {line["account_id"]: line["debit"] for line in body["lines"]}
    # The reversal mirrors the original: diversen was credited, so it is
    # debited here.
    assert debits[str(world["diversen"])] == "50.00"

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/journal-entries/{entry_id}/reverse",
        {"period_id": str(world["period_a"]), "entry_date": "2026-09-17"},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["reason"] == "journal_entry_already_reversed"


# --- periods -----------------------------------------------------------


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/periods")
async def test_list_periods(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/periods?fiscal_year_id={world['year']}",
    )
    assert response.status_code == 200, response.text
    numbers = {row["period_number"] for row in response.json()["periods"]}
    assert numbers == {9, 10}


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/periods/{period_id}/lock")
async def test_lock_and_unlock_period(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    lock = await _call(
        two_organizations, two_organizations.admin_a, "POST", f"/periods/{world['period_a']}/lock"
    )
    assert lock.status_code == 200, lock.text
    assert lock.json()["status"] == "locked"

    unlock_missing_reason = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/periods/{world['period_a']}/unlock",
        {"reason": ""},
    )
    assert unlock_missing_reason.status_code == 422, unlock_missing_reason.text


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/periods/{period_id}/unlock")
async def test_unlock_period_with_reason(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    await _call(
        two_organizations, two_organizations.admin_a, "POST", f"/periods/{world['period_a']}/lock"
    )

    unlock = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/periods/{world['period_a']}/unlock",
        {"reason": "Correction requested by the bookkeeper"},
    )
    assert unlock.status_code == 200, unlock.text
    assert unlock.json()["status"] == "open"

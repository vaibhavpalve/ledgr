"""IAM-005: the Grootboek screen's reads answer nothing across a tenant
boundary (docs/founder-review-2026-09-14.md §4.3).

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

Same shape as `test_dashboard_isolation.py`: organization A's Owner asks for
organization B's chart, trial balance and journal and must be refused -
by authorization (403) or by RLS (404); which one is not the property under
test. Ids are invented rather than seeded for the reason that file gives: a
caller with no access to administration B must be refused before any id is
looked up, so a fabricated one proves more than a real one would.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

REFUSED = {403, 404}

_BASE = "/v1/administrations/{admin}"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/chart-of-accounts")
async def test_reading_another_tenants_chart_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_b)}/chart-of-accounts",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/trial-balance")
async def test_reading_another_tenants_trial_balance_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_b)}/trial-balance"
            f"?fiscal_year_id={uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/journal-entries")
async def test_listing_another_tenants_journal_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_b)}/journal-entries",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code in REFUSED

        # And the caller's OWN journal answers, empty, through the same route
        # - so the refusal above is a refusal and not a broken endpoint.
        own = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_a)}/journal-entries",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert own.status_code == 200, own.text
    assert own.json() == {
        "administration_id": str(two_organizations.admin_a),
        "items": [],
        "next_cursor": None,
    }


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/journal-entries/{entry_id}")
async def test_reading_another_tenants_journal_entry_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_b)}/journal-entries/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


async def test_a_garbled_cursor_is_a_validation_error_not_a_crash(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_a)}/journal-entries?cursor=not-a-cursor",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "invalid_cursor"

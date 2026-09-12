"""IAM-005 isolation tests for the /v1/administrations routes, run against a
real Postgres with RLS enabled — these are the tests that actually prove
isolation holds, complementing test_isolation_coverage.py's structural
check that a test exists at all for every route.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.support.isolation import (
    assert_cannot_fetch_foreign_record,
    assert_tenant_isolated,
    make_token,
)
from tests.support.seed import SeededTenants


@pytest.mark.isolation("GET", "/v1/administrations")
async def test_list_administrations_does_not_leak_other_tenants(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/administrations",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.admin_b],
        )

        # Not just "org B's record is absent" — org A's own record, seeded
        # with the same legal_name and legal_form, must still be present.
        # Isolation that also hid your own data would pass the leak check
        # for the wrong reason.
        ids = {item["id"] for item in response.json()}
        assert str(two_organizations.admin_a) in ids
        assert str(two_organizations.admin_b) not in ids


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}")
async def test_get_administration_hides_other_tenants_record(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await assert_cannot_fetch_foreign_record(
            client,
            "/v1/administrations/{id}",
            foreign_id=two_organizations.admin_b,
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            # 403, not 404: this route carries an administration-scoped
            # authorization check (IAM-030), which runs before the query
            # and denies on org B's administration without ever reaching
            # RLS. Both layers independently refuse; this asserts the
            # outer one, and test_authorization_isolation.py asserts the
            # inner one still holds on its own.
            expected_status=403,
        )

        # Sanity check on the same client/route: org A can fetch its OWN
        # record. Without this, a bug that refused every request (not just
        # foreign ones) would pass the isolation check above for free.
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        own = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert own.status_code == 200
        assert own.json()["id"] == str(two_organizations.admin_a)

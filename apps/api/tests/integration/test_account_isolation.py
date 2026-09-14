"""IAM-005: `GET /v1/me` names only the caller's own memberships.

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

The route is authorization-exempt (see api.authz.dependencies), which makes
this test the whole guarantee rather than a second line: nothing between the
tenant middleware and the handler filters what it returns. Organization A's
Owner asks who they are and must see their own organization and
administration, and nothing that identifies organization B - whose
administration and organization were seeded with the same legal name on
purpose, so only the ids can tell them apart.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants, seed_session

pytestmark = pytest.mark.anyio


@pytest.mark.isolation("GET", "/v1/me")
async def test_me_names_only_the_callers_own_memberships(
    two_organizations: SeededTenants,
) -> None:
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["organization"]["id"] == str(two_organizations.org_a)
    assert body["organization"]["kind"] == "business"
    assert body["user"]["id"] == str(two_organizations.owner_a)

    ids = {entry["id"] for entry in body["administrations"]}
    assert ids == {str(two_organizations.admin_a)}
    # The founding Owner's organization-scoped grant is the role shown on the
    # administrations that organization owns (ADR-011's cascade).
    assert body["administrations"][0]["role"] == "Owner"
    assert body["administrations"][0]["role_is_system"] is True
    assert body["onboarding"]["needs_administration"] is False
    # No administration has been switched into on this fresh session.
    assert body["active_administration_id"] is None

    assert str(two_organizations.org_b) not in response.text
    assert str(two_organizations.admin_b) not in response.text
    assert str(two_organizations.owner_b) not in response.text

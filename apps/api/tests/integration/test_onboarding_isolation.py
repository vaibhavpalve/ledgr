"""IAM-005 for the onboarding surface (docs/founder-review-2026-09-14.md
§4.2), plus the end-to-end run that proves a fresh signup ends up with a
usable administration.

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

The refusal may be 403 (authorization says no) or 404 (RLS filtered the row
out) - both are correct, and which one arrives is not the property under
test. For `POST /v1/administrations`, which names no foreign id, the property
is the other way round: whatever organization A creates lands in A and is
invisible to B.

The two functional tests at the end need a current RGS version loaded
(FR-ONB-005 seeds the chart from it). `make load-rgs` does that for the local
stack; a database without one skips them rather than failing on a missing
dataset, and the isolation assertions above still hold either way because a
503 from the seed carries no tenant data.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_session,
    seed_user,
    signup_firm_organization,
)

pytestmark = pytest.mark.anyio

REFUSED = {403, 404}

_CREATE_BODY = {
    "legal_name": "Van Doorn Bouw B.V.",
    "trade_name": "Van Doorn",
    "legal_form": "BV",
    "kvk_number": "34281907",
    "vat_number": "NL001234567B01",
    "formatting_locale": "nl-NL",
    "fiscal_year": {
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "period_scheme": "monthly",
    },
}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


async def _rgs_is_loaded() -> bool:
    async with app_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM rgs_version WHERE status = 'current') AS loaded")
        )
        return bool(result.scalar_one())


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/administrations")
async def test_an_administration_created_by_one_tenant_is_invisible_to_another(
    two_organizations: SeededTenants,
) -> None:
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    token_a = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )
    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/administrations", headers=_headers(token_a), json=_CREATE_BODY
        )
        # 503 only when no RGS version is loaded (see the module docstring);
        # then nothing was created and there is nothing to leak.
        assert created.status_code in {200, 503}, created.text
        assert str(two_organizations.org_b) not in created.text
        assert str(two_organizations.admin_b) not in created.text

        if created.status_code == 200:
            new_id = created.json()["id"]
            foreign = await client.get(
                f"/v1/administrations/{new_id}", headers={"Authorization": f"Bearer {token_b}"}
            )
            assert foreign.status_code in REFUSED
            assert new_id not in foreign.text

            me_b = await client.get("/v1/me", headers={"Authorization": f"Bearer {token_b}"})
            assert me_b.status_code == 200
            assert new_id not in me_b.text


@pytest.mark.isolation("GET", "/v1/fiscal-years/preview")
async def test_the_period_preview_names_no_tenant_at_all(
    two_organizations: SeededTenants,
) -> None:
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/fiscal-years/preview"
            "?start_date=2026-03-15&end_date=2026-12-31&period_scheme=monthly",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    periods = response.json()["periods"]
    assert len(periods) == 10
    assert periods[0]["is_stub"] is True
    assert str(two_organizations.admin_b) not in response.text
    assert str(two_organizations.org_b) not in response.text


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/fiscal-years")
async def test_listing_another_tenants_fiscal_years_is_refused(
    two_organizations: SeededTenants,
) -> None:
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/fiscal-years",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/fiscal-years")
async def test_opening_a_fiscal_year_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/fiscal-years",
            headers=_headers(token),
            json=_CREATE_BODY["fiscal_year"],
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("PATCH", "/v1/administrations/{administration_id}")
async def test_editing_another_tenants_administration_is_refused(
    two_organizations: SeededTenants,
) -> None:
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(
            f"/v1/administrations/{two_organizations.admin_b}",
            headers=_headers(token),
            json={"trade_name": "Overgenomen"},
        )

    assert response.status_code in REFUSED

    # And the row is untouched, read back as its own tenant.
    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        own = await client.get("/v1/me", headers={"Authorization": f"Bearer {token_b}"})
    assert own.status_code == 200
    assert "Overgenomen" not in own.text


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


async def test_a_fresh_business_ends_up_with_a_usable_administration(
    two_organizations: SeededTenants,
) -> None:
    """The golden path in one POST (brief §4.2): the row, the chart, the
    fiscal year and the session's active client, then read back through
    GET /v1/me and the trial balance.
    """
    if not await _rgs_is_loaded():
        pytest.skip("no current RGS version loaded - run `make load-rgs` first")

    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/administrations", headers=_headers(token), json=_CREATE_BODY
        )
        assert created.status_code == 200, created.text
        body = created.json()

        assert body["legal_name"] == "Van Doorn Bouw B.V."
        assert body["trade_name"] == "Van Doorn"
        assert body["legal_form"] == "BV"
        assert body["initials"] == "VD"
        assert body["colour"]
        assert body["role"] == "Owner"
        assert body["chart"]["seeded"] > 0
        assert body["chart"]["rgs_version"]
        [year] = body["fiscal_years"]
        assert year["start_date"] == "2026-01-01"
        assert year["period_scheme"] == "monthly"
        assert year["is_current"] is True

        me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["active_administration_id"] == body["id"]
        assert body["id"] in {entry["id"] for entry in me.json()["administrations"]}
        assert me.json()["onboarding"]["needs_administration"] is False

        balance = await client.get(
            f"/v1/administrations/{body['id']}/trial-balance?fiscal_year_id={year['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert balance.status_code == 200, balance.text
        assert balance.json()["balanced"] is True
        assert len(balance.json()["rows"]) == body["chart"]["seeded"]
        assert balance.json()["total_debit"] == "0.00"

        # A second year opens through the dedicated route; a third that
        # overlaps it is refused with the overlap message, not a 500.
        second = await client.post(
            f"/v1/administrations/{body['id']}/fiscal-years",
            headers=_headers(token),
            json={"start_date": "2027-01-01", "end_date": "2027-12-31"},
        )
        assert second.status_code == 200, second.text
        overlapping = await client.post(
            f"/v1/administrations/{body['id']}/fiscal-years",
            headers=_headers(token),
            json={"start_date": "2027-06-01", "end_date": "2028-05-31"},
        )
        assert overlapping.status_code == 409, overlapping.text
        assert overlapping.json()["detail"]["reason"] == "fiscal_year_overlaps"

        edited = await client.patch(
            f"/v1/administrations/{body['id']}",
            headers=_headers(token),
            json={"trade_name": None, "vat_number": "NL999999999B01"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["trade_name"] is None
        assert edited.json()["vat_number"] == "NL999999999B01"
        assert edited.json()["legal_name"] == "Van Doorn Bouw B.V."


async def test_an_unknown_legal_form_is_refused_before_anything_is_written(
    two_organizations: SeededTenants,
) -> None:
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/administrations",
            headers=_headers(token),
            json={**_CREATE_BODY, "legal_form": "Ltd"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "unknown_legal_form"
        assert "bv" in response.json()["detail"]["accepted"]

        me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert "Van Doorn" not in me.text


async def test_a_firm_creating_a_client_can_work_in_it(
    two_organizations: SeededTenants,
) -> None:
    """FR-MDL-004 through the same route: the client organization, its
    administration and the engagement come from
    app.create_firm_client_administration, and the creating firm user ends
    up holding Accountant on the new client (ADR-059's founding grant) - so
    they can seed it, open its year, switch into it and read its books.
    """
    if not await _rgs_is_loaded():
        pytest.skip("no current RGS version loaded - run `make load-rgs` first")

    suffix = uuid.uuid4().hex[:8]
    firm = await signup_firm_organization(app_engine, name="Kantoor De Jong", kvk="33333333")
    partner = await seed_user(app_engine, email=f"partner+{suffix}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=partner,
        role_name="Owner",
        scope_type="organization",
        scope_id=firm,
    )
    session_id = await seed_session(app_engine, user_id=partner)
    token = make_token(firm, user_id=partner, session_id=session_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/administrations", headers=_headers(token), json=_CREATE_BODY
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["role"] == "Accountant"
        assert body["chart"]["seeded"] > 0

        me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["organization"]["kind"] == "firm"
        assert me.json()["active_administration_id"] == body["id"]
        assert [entry["id"] for entry in me.json()["administrations"]] == [body["id"]]

        chart = await client.get(
            f"/v1/administrations/{body['id']}/chart-of-accounts",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert chart.status_code == 200, chart.text
        assert len(chart.json()["accounts"]) == body["chart"]["seeded"]

        # The client that was just created is nobody else's: organization A
        # cannot see it.
        token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        foreign = await client.get(
            f"/v1/administrations/{body['id']}", headers={"Authorization": f"Bearer {token_a}"}
        )
        assert foreign.status_code in REFUSED

    # IAM-107/IAM-109: the grant is recorded as the FIRM's, on the client's
    # administration - the shape the client can see and revoke.
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(firm)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT ra.granted_by_organization_id, a.organization_id AS owner "
                    "FROM role_assignment ra JOIN administration a ON a.id = ra.scope_id "
                    "WHERE ra.user_id = :user_id AND ra.scope_type = 'administration' "
                    "  AND ra.scope_id = :admin"
                ),
                {"user_id": str(partner), "admin": body["id"]},
            )
        ).one()
    assert row.granted_by_organization_id == firm
    assert row.owner != firm

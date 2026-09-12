"""IAM-005 isolation tests for the receipt-capture routes.

A capture session is where somebody's receipts and their amounts arrive, so a
leak here is one client's expenses appearing in another's review list — and the
wrong-client failure FR-FRM-000a calls the worst in this product, reached
through a table nobody was watching.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"a" * 64


async def _fiscal_year(administration_id: uuid.UUID, organization_id: uuid.UUID) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text("SELECT (ledger.open_fiscal_year(:a, :s, :e, 'monthly', null)).id"),
            {
                "a": str(administration_id),
                "s": date(2025, 1, 1),
                "e": date(2025, 12, 31),
            },
        )
        return result.scalar_one()


async def _seed_session(
    administration_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO capture_session ("
                "  organization_id, administration_id, opened_by_user_id"
                ") VALUES (:org, :admin, :user) RETURNING id"
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "user": str(user_id),
            },
        )
        return result.scalar_one()


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/capture-sessions")
async def test_opening_a_session_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/capture-sessions",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"capture-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        count = await conn.execute(
            text("SELECT count(*) FROM capture_session WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )
    assert count.scalar_one() == 0


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/capture-sessions/{session_id}/pages"
)
async def test_capturing_into_another_tenants_session_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The write that would put one client's receipt into another's books."""
    year = await _fiscal_year(two_organizations.admin_b, two_organizations.org_b)
    foreign_session = await _seed_session(
        two_organizations.admin_b, two_organizations.org_b, two_organizations.owner_b
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/capture-sessions"
            f"/{foreign_session}/pages?fiscal_year_id={year}&source=upload",
            content=JPEG,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "image/jpeg",
                "Idempotency-Key": f"capture-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        pages = await conn.execute(
            text("SELECT count(*) FROM capture_page WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )
        expenses = await conn.execute(
            text("SELECT count(*) FROM expense WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )
    assert pages.scalar_one() == 0
    assert expenses.scalar_one() == 0, "no claim was created in the other tenant"


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/capture-sessions/{session_id}"
)
async def test_another_tenants_review_list_is_not_readable(
    two_organizations: SeededTenants,
) -> None:
    foreign_session = await _seed_session(
        two_organizations.admin_b, two_organizations.org_b, two_organizations.owner_b
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/administrations/{two_organizations.admin_b}/capture-sessions/{foreign_session}",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign_session, two_organizations.admin_b],
        )

    assert response.status_code in (403, 404)


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/capture-sessions/{session_id}/finalise"
)
async def test_another_tenants_session_cannot_be_finalised(
    two_organizations: SeededTenants,
) -> None:
    """Finalising somebody else's sitting would release their drafts as claims
    on a decision they never made.
    """
    foreign_session = await _seed_session(
        two_organizations.admin_b, two_organizations.org_b, two_organizations.owner_b
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}"
            f"/capture-sessions/{foreign_session}/finalise",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"finalise-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        result = await conn.execute(
            text("SELECT finalised_at FROM capture_session WHERE id = :id"),
            {"id": str(foreign_session)},
        )
    assert result.scalar_one() is None, "the other tenant's session is still open"


async def test_rls_hides_capture_rows_from_the_other_tenant(
    two_organizations: SeededTenants,
) -> None:
    """The data layer on its own, with no HTTP in the way - CLAUDE.md rule 1's
    first line of defence.
    """
    mine = await _seed_session(
        two_organizations.admin_a, two_organizations.org_a, two_organizations.owner_a
    )
    theirs = await _seed_session(
        two_organizations.admin_b, two_organizations.org_b, two_organizations.owner_b
    )

    async def visible(organization_id: uuid.UUID) -> set[uuid.UUID]:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization_id)},
            )
            result = await conn.execute(text("SELECT id FROM capture_session"))
            return {row.id for row in result}

    assert mine in await visible(two_organizations.org_a)
    assert theirs not in await visible(two_organizations.org_a)
    assert theirs in await visible(two_organizations.org_b)
    assert mine not in await visible(two_organizations.org_b)

"""IAM-005: no template-asset endpoint answers across a tenant boundary.

Mirrors `tests/integration/test_document_isolation.py`'s pattern (a source
document is evidence; a logo is smaller-stakes, but the tenant boundary is
identical) and `tests/integration/test_invoice_template_isolation.py`'s
REFUSED set exactly: which refusal arrives - 403 (no grant on administration
B at all) or 404 (RLS filtered the row out) - is not the property under test,
only that no request crosses.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

REFUSED = {403, 404}

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _headers(token: str, content_type: str = "image/png") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": content_type,
    }


async def _seed_asset(administration_id: uuid.UUID, organization_id: uuid.UUID) -> uuid.UUID:
    """A row written directly, the same posture
    `tests/integration/test_document_isolation.py::_seed_document` takes: the
    point of the download test is the READ path, and going through the
    service would also need an encryption key and a scanner already exercised
    in `tests/templates/test_asset_service.py`.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text(
                """
                INSERT INTO template_asset (
                    organization_id, administration_id, storage_key, content_type, sanitized
                ) VALUES (
                    :org, :admin, :key, 'image/png', true
                ) RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "key": f"{administration_id}/{uuid.uuid4().hex}",
            },
        )
        return result.scalar_one()


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/template-assets")
async def test_uploading_into_another_tenants_administration_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """Org A's owner naming org B's administration must not put a logo in org
    B's asset store - and nothing is left behind either way.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/template-assets",
            content=PNG,
            headers=_headers(token),
        )

    assert response.status_code in REFUSED, response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        count = await conn.execute(
            text("SELECT count(*) FROM template_asset WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )
    assert count.scalar_one() == 0, "nothing was written into the other tenant's asset store"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/template-assets/{asset_id}")
async def test_another_tenants_logo_bytes_are_not_downloadable(
    two_organizations: SeededTenants,
) -> None:
    foreign = await _seed_asset(two_organizations.admin_b, two_organizations.org_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/template-assets/{foreign}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED, response.text
    assert str(foreign) not in response.text


async def test_rls_hides_template_assets_from_the_other_tenant_directly(
    two_organizations: SeededTenants,
) -> None:
    """The data layer on its own, with no HTTP in the way - CLAUDE.md rule
    one's first line of defence, the same proof
    `test_document_isolation.py::test_rls_hides_documents_from_the_other_tenant_directly`
    gives for the document archive.
    """
    mine = await _seed_asset(two_organizations.admin_a, two_organizations.org_a)
    theirs = await _seed_asset(two_organizations.admin_b, two_organizations.org_b)

    async def visible(organization_id: uuid.UUID) -> set[uuid.UUID]:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization_id)},
            )
            result = await conn.execute(text("SELECT id FROM template_asset"))
            return {row.id for row in result}

    assert mine in await visible(two_organizations.org_a)
    assert theirs not in await visible(two_organizations.org_a)
    assert theirs in await visible(two_organizations.org_b)
    assert mine not in await visible(two_organizations.org_b)

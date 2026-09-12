"""IAM-005: no invoice-template endpoint answers across a tenant boundary.

One test per route in `api.templates.routes`, each asking as organization A's
owner for something in organization B's administration and expecting a
refusal - 403 (authorization says no, since the caller holds no grant on
administration B at all) or 404 (RLS filtered the row out and the service
cannot tell it from a row that never existed), mirroring
`tests/integration/test_sales_invoice_isolation.py`'s pattern and REFUSED set
exactly: which refusal arrives is not the property under test, only that no
request crosses.

Template ids are invented rather than seeded, for the same reason the sales
invoice isolation tests invent invoice ids: a caller with no access to
administration B must be refused before any id is looked up, so a fabricated
one proves more than a real one would.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

REFUSED = {403, 404}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _template_body(name: str = "Classic") -> dict[str, object]:
    """A minimal, compliant body - the defaults `api.templates.routes.
    TemplateBody` fills in (columns, blocks) are already FR-TPL-009-compliant,
    so this is enough to reach the service layer these tests are probing.
    """
    return {"name": name}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/invoice-templates")
async def test_listing_another_tenants_templates_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/invoice-templates",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/invoice-templates")
async def test_creating_a_template_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/invoice-templates",
            headers=_headers(token),
            json=_template_body(),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/invoice-templates/{template_id}"
)
async def test_reading_another_tenants_template_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/invoice-templates/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "PUT", "/v1/administrations/{administration_id}/invoice-templates/{template_id}"
)
async def test_editing_another_tenants_template_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        body = _template_body()
        body["version"] = 1
        response = await client.put(
            f"/v1/administrations/{two_organizations.admin_b}/invoice-templates/{uuid.uuid4()}",
            headers=_headers(token),
            json=body,
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/invoice-templates/{template_id}/preview",
)
async def test_previewing_another_tenants_template_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The one that would render a preview using another tenant's real
    supplier VAT and KvK numbers (`api.templates.service.
    InvoiceTemplateService.preview` merges them into the legal_identity
    block), so a leak here is a statutory-identity leak, not just a settings
    leak.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}"
            f"/invoice-templates/{uuid.uuid4()}/preview",
            headers=_headers(token),
            json=_template_body(),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/invoice-templates/{template_id}/duplicate",
)
async def test_duplicating_another_tenants_template_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """FR-TPL-019: a duplicate would copy another tenant's template - logo,
    colours, columns, blocks and all - into a brand-new row. This must be
    refused before any source row is even read.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}"
            f"/invoice-templates/{uuid.uuid4()}/duplicate",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/invoice-templates/{template_id}/reset",
)
async def test_resetting_another_tenants_template_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """FR-TPL-019's reset overwrites the target row's design in place - a
    write to another tenant's template, and must be refused before it reaches
    the service layer.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}"
            f"/invoice-templates/{uuid.uuid4()}/reset",
            headers=_headers(token),
            json={"version": 1},
        )

    assert response.status_code in REFUSED

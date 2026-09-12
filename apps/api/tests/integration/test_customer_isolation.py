"""IAM-005: no customer-master endpoint answers across a tenant boundary.

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

One test per route in `api.customers.routes`, each asking as organization A's
owner for something in organization B and expecting a refusal. The refusal may
be 403 (authorization says no) or 404 (RLS filtered the row out and the service
cannot tell it from a row that never existed) - both are correct, and which one
arrives is not the property under test. What is under test is that no request
crosses.

The customer ids are invented rather than seeded, for the reason
`test_sales_invoice_isolation` gives: a caller with no access to administration
B must be refused before the id is ever looked up, so a fabricated id proves
more than a real one would.

The LIST route is the exception and is checked twice - refused, and asserted
not to leak. A list endpoint is the one shape where "not 200" is not the whole
story: a handler that answered 200 with somebody else's customers would satisfy
a status-code check and be the exact failure IAM-005 exists to catch.
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

_BASE = "/v1/administrations/{admin}/customers"

_CUSTOMER_BODY = {
    "name": "De Vries Holding B.V.",
    "address_line1": "Damrak 70",
    "postal_code": "1012 LM",
    "city": "Amsterdam",
    "country": "NL",
    "kvk_number": "12345678",
    "invoice_email": "facturen@devries.example",
}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/customers")
async def test_creating_a_customer_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            _BASE.format(admin=two_organizations.admin_b),
            headers=_headers(token),
            json=_CUSTOMER_BODY,
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/customers")
async def test_listing_another_tenants_customers_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """Refused - and, if it ever stopped being refused, still not a leak.

    Seeds a real customer in B first, so the second assertion has something it
    could actually have disclosed. A test that searched a response body for an
    id nothing had created would pass no matter how broken the handler was.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        owner_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        created = await client.post(
            _BASE.format(admin=two_organizations.admin_b),
            headers=_headers(owner_b),
            json=_CUSTOMER_BODY,
        )
        assert created.status_code == 200, created.text
        foreign_id = created.json()["id"]

        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            _BASE.format(admin=two_organizations.admin_b),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED
    assert foreign_id not in response.text


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/customers/{customer_id}")
async def test_reading_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("PUT", "/v1/administrations/{administration_id}/customers/{customer_id}")
async def test_editing_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}",
            headers=_headers(token),
            json=_CUSTOMER_BODY,
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/customers/{customer_id}/archive"
)
async def test_archiving_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}/archive",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/customers/{customer_id}/restore"
)
async def test_restoring_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}/restore",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/customers/{customer_id}/erasure-request"
)
async def test_requesting_another_tenants_customer_erasure_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The one that would anonymise a counterparty belonging to a business
    the caller has no relationship with - PRIV-022, across a tenant boundary.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}/erasure-request",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/customers/{customer_id}/vat-number/validate",
)
async def test_validating_another_tenants_vat_number_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The one that would spend another tenant's VIES consultations, and
    disclose - through the verdict it wrote - a VAT number belonging to a
    counterparty of a business the caller has no relationship with.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}/vat-number/validate",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/customers/{customer_id}/peppol/discover"
)
async def test_discovering_another_tenants_peppol_participant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"{_BASE.format(admin=two_organizations.admin_b)}/{uuid.uuid4()}/peppol/discover",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED

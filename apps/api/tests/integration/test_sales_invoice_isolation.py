"""IAM-005: no sales-invoicing endpoint answers across a tenant boundary.

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

One test per route in `api.invoicing.routes`, each asking as organization A's
owner for something in organization B and expecting a refusal. The refusal may
be 403 (authorization says no) or 404 (RLS filtered the row out and the service
cannot tell it from a row that never existed) - both are correct, and which one
arrives is not the property under test. What is under test is that no request
crosses.

The invoice ids are invented rather than seeded: a caller with no access to
administration B must be refused before the id is ever looked up, so a real one
would prove less than a fabricated one.
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


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/sales-invoices")
async def test_creating_an_invoice_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices",
            headers=_headers(token),
            json={
                "fiscal_year_id": str(uuid.uuid4()),
                "invoice_date": "2026-09-09",
                "customer_name": "De Vries Holding B.V.",
                "customer_address": "Damrak 70, Amsterdam",
                "lines": [],
            },
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/sales-invoices")
async def test_listing_another_tenants_invoices_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """MOB-005's View tab list - the new endpoint, checked the same way every
    other route on this resource already is: no request naming another
    tenant's administration may succeed.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}")
async def test_reading_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "PUT", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/lines"
)
async def test_editing_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/lines",
            headers=_headers(token),
            json={"lines": []},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "DELETE", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}"
)
async def test_discarding_another_tenants_draft_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.delete(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/issue"
)
async def test_issuing_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    # The one that would draw a number out of another tenant's gapless series
    # (FR-AR-004) as well as reading their customer's details.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/issue",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/credit"
)
async def test_crediting_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/credit",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/send"
)
async def test_sending_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The one that would put another tenant's invoice, and their customer's
    name and balance, into an inbox chosen by the caller. An address in the
    body makes the request an exfiltration attempt rather than a mistake, so
    it is supplied here deliberately.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/send",
            headers=_headers(token),
            json={"to": "attacker@example.com"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/deliveries"
)
async def test_reading_another_tenants_delivery_status_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """A delivery record carries the recipient address, so this leaks a
    customer's e-mail address as well as the fact that they were invoiced.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}"
            f"/sales-invoices/{uuid.uuid4()}/deliveries",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/payments"
)
async def test_recording_a_payment_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """ADR-070. The one that would move another tenant's debtor balance and post
    a receipt into their books, so it is asked with a body that would otherwise
    be perfectly valid."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/payments",
            headers=_headers(token),
            json={
                "amount": "100.00",
                "paid_on": "2026-09-15",
                "method": "bank_transfer",
                "bank_account_id": str(uuid.uuid4()),
            },
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/payments"
)
async def test_listing_another_tenants_payments_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/payments",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/payments/{payment_id}/void",
)
async def test_voiding_another_tenants_payment_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}"
            f"/payments/{uuid.uuid4()}/void",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED

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


# -- SI-04: the reminder ladder (ADR-071) ------------------------------------------
#
# Six routes, each asked as organization A's owner for something in organization
# B. The send is the one that would mail another tenant's customer, so it is asked
# with a request that would otherwise be perfectly valid.


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/dunning")
async def test_reading_another_tenants_dunning_overview_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/dunning",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation("PUT", "/v1/administrations/{administration_id}/dunning/ladder")
async def test_configuring_another_tenants_ladder_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            f"/v1/administrations/{two_organizations.admin_b}/dunning/ladder",
            headers=_headers(token),
            json={"steps": []},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/dunning"
)
async def test_assessing_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}/dunning",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/dunning/send"
)
async def test_sending_a_reminder_for_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/sales-invoices/{uuid.uuid4()}"
            f"/dunning/send",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "PUT", "/v1/administrations/{administration_id}/customers/{customer_id}/dunning-pause"
)
async def test_pausing_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            f"/v1/administrations/{two_organizations.admin_b}/customers/{uuid.uuid4()}"
            f"/dunning-pause",
            headers=_headers(token),
            json={"reason": "x"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "DELETE", "/v1/administrations/{administration_id}/customers/{customer_id}/dunning-pause"
)
async def test_resuming_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.delete(
            f"/v1/administrations/{two_organizations.admin_b}/customers/{uuid.uuid4()}"
            f"/dunning-pause",
            headers=_headers(token),
        )

    assert response.status_code in REFUSED


# -- SI-06: aged receivables and the customer statement (ADR-072) ---------------------


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/receivables/ageing")
async def test_reading_another_tenants_ageing_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/receivables/ageing",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/customers/{customer_id}/statement"
)
async def test_reading_another_tenants_statement_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/customers/{uuid.uuid4()}/statement",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


# -- SI-11: chase everything overdue (ADR-073) ------------------------------------------
#
# The bulk send is the route that could mail another tenant's whole customer list, so
# it is asked with a body that would otherwise be a perfectly valid explicit selection.


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/dunning/chase")
async def test_chasing_another_tenants_overdue_invoices_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        everything = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/dunning/chase",
            headers=_headers(token),
        )
        chosen = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/dunning/chase",
            headers=_headers(token),
            json={"items": [{"invoice_id": str(uuid.uuid4()), "step_position": 1}]},
        )

    assert everything.status_code in REFUSED
    assert chosen.status_code in REFUSED


# -- SI-07: recurring invoices (ADR-074) --------------------------------------------------
#
# Seven routes, each asked as organization A's owner for something in organization B.
# The run is the one that would raise (and possibly issue and post) invoices in another
# tenant's books, so it is asked exactly as a real call would be.

_RECURRING = "/v1/administrations/{administration_id}/recurring-invoices"


def _recurring_body() -> dict[str, object]:
    return {
        "customer_id": str(uuid.uuid4()),
        "name": "x",
        "interval_months": 1,
        "start_date": "2026-07-31",
        "lines": [
            {"description": "x", "quantity": "1", "unit_price": "1", "vat_treatment": "btw_21"}
        ],
    }


async def _as_a(method: str, path: str, tenants: SeededTenants, json: object = None) -> int:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(tenants.org_a, user_id=tenants.owner_a)
        response = await client.request(
            method,
            f"/v1/administrations/{tenants.admin_b}{path}",
            headers=_headers(token),
            json=json,
        )
    return response.status_code


@pytest.mark.isolation("POST", _RECURRING)
async def test_creating_a_recurring_invoice_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    status = await _as_a("POST", "/recurring-invoices", two_organizations, _recurring_body())
    assert status in REFUSED


@pytest.mark.isolation("GET", _RECURRING)
async def test_listing_another_tenants_recurring_invoices_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", "/recurring-invoices", two_organizations) in REFUSED


@pytest.mark.isolation("POST", _RECURRING + "/run")
async def test_running_another_tenants_recurring_invoices_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("POST", "/recurring-invoices/run", two_organizations) in REFUSED


@pytest.mark.isolation("GET", _RECURRING + "/{recurring_id}")
async def test_reading_another_tenants_recurring_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/recurring-invoices/{uuid.uuid4()}"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("PUT", _RECURRING + "/{recurring_id}")
async def test_editing_another_tenants_recurring_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/recurring-invoices/{uuid.uuid4()}"
    assert await _as_a("PUT", path, two_organizations, _recurring_body()) in REFUSED


@pytest.mark.isolation("POST", _RECURRING + "/{recurring_id}/pause")
async def test_pausing_another_tenants_recurring_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/recurring-invoices/{uuid.uuid4()}/pause"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _RECURRING + "/{recurring_id}/resume")
async def test_resuming_another_tenants_recurring_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/recurring-invoices/{uuid.uuid4()}/resume"
    assert await _as_a("POST", path, two_organizations) in REFUSED


# -- SI-08: quotes and order confirmations (ADR-075) ----------------------------------------
_QUOTES = "/v1/administrations/{administration_id}/quotes"


def _quote_body() -> dict[str, object]:
    return {
        "kind": "quote",
        "customer_id": str(uuid.uuid4()),
        "lines": [
            {
                "description": "Advies",
                "quantity": "1",
                "unit_price": "100",
                "vat_treatment": "btw_21",
            }
        ],
    }


@pytest.mark.isolation("POST", _QUOTES)
async def test_creating_a_quote_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("POST", "/quotes", two_organizations, _quote_body()) in REFUSED


@pytest.mark.isolation("GET", _QUOTES)
async def test_listing_another_tenants_quotes_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", "/quotes", two_organizations) in REFUSED


@pytest.mark.isolation("GET", _QUOTES + "/{quote_id}")
async def test_reading_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", f"/quotes/{uuid.uuid4()}", two_organizations) in REFUSED


@pytest.mark.isolation("PUT", _QUOTES + "/{quote_id}")
async def test_editing_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}"
    assert await _as_a("PUT", path, two_organizations, _quote_body()) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/sent")
async def test_sending_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/sent"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/accept")
async def test_accepting_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/accept"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/decline")
async def test_declining_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/decline"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/cancel")
async def test_cancelling_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/cancel"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/extend")
async def test_extending_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/extend"
    body = {"valid_until": "2099-01-01"}
    assert await _as_a("POST", path, two_organizations, body) in REFUSED


@pytest.mark.isolation("POST", _QUOTES + "/{quote_id}/convert")
async def test_converting_another_tenants_quote_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/quotes/{uuid.uuid4()}/convert"
    assert await _as_a("POST", path, two_organizations) in REFUSED


# -- SI-10: bad-debt write-off (ADR-076) ----------------------------------------------------
_WRITE_OFFS = "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/write-offs"


@pytest.mark.isolation("POST", _WRITE_OFFS)
async def test_writing_off_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/write-offs"
    body = {"expense_account_id": str(uuid.uuid4()), "reason": "oninbaar"}
    assert await _as_a("POST", path, two_organizations, body) in REFUSED


@pytest.mark.isolation("GET", _WRITE_OFFS)
async def test_listing_another_tenants_write_offs_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/write-offs"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _WRITE_OFFS + "/{write_off_id}/reclaim-vat")
async def test_reclaiming_vat_on_another_tenants_write_off_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/write-offs/{uuid.uuid4()}/reclaim-vat"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _WRITE_OFFS + "/{write_off_id}/void")
async def test_voiding_another_tenants_write_off_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/write-offs/{uuid.uuid4()}/void"
    assert await _as_a("POST", path, two_organizations) in REFUSED


# -- SI-09: SEPA direct debit (ADR-077) ---------------------------------------------------
_SEPA = "/v1/administrations/{administration_id}"


@pytest.mark.isolation("POST", _SEPA + "/customers/{customer_id}/sepa-mandates")
async def test_registering_a_mandate_for_another_tenants_customer_is_refused(
    two_organizations: SeededTenants,
) -> None:
    body = {
        "signed_on": "2026-01-10",
        "debtor_name": "De Vries Holding B.V.",
        "debtor_iban": "NL02ABNA0123456789",
    }
    path = f"/customers/{uuid.uuid4()}/sepa-mandates"
    assert await _as_a("POST", path, two_organizations, body) in REFUSED


@pytest.mark.isolation("GET", _SEPA + "/customers/{customer_id}/sepa-mandates")
async def test_listing_another_tenants_mandates_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/customers/{uuid.uuid4()}/sepa-mandates"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _SEPA + "/sepa-mandates/{mandate_id}/revoke")
async def test_revoking_another_tenants_mandate_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-mandates/{uuid.uuid4()}/revoke"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _SEPA + "/sepa-collections")
async def test_creating_a_collection_file_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    body = {"collection_date": "2099-01-05"}
    assert await _as_a("POST", "/sepa-collections", two_organizations, body) in REFUSED


@pytest.mark.isolation("GET", _SEPA + "/sepa-collections")
async def test_listing_another_tenants_collection_files_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", "/sepa-collections", two_organizations) in REFUSED


@pytest.mark.isolation("GET", _SEPA + "/sepa-collections/{batch_id}")
async def test_reading_another_tenants_collection_file_record_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-collections/{uuid.uuid4()}"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("GET", _SEPA + "/sepa-collections/{batch_id}/file")
async def test_downloading_another_tenants_collection_file_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-collections/{uuid.uuid4()}/file"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _SEPA + "/sepa-collections/{batch_id}/cancel")
async def test_cancelling_another_tenants_collection_file_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-collections/{uuid.uuid4()}/cancel"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _SEPA + "/sepa-collections/{batch_id}/items/{item_id}/collected")
async def test_confirming_another_tenants_collection_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-collections/{uuid.uuid4()}/items/{uuid.uuid4()}/collected"
    body = {"bank_account_id": str(uuid.uuid4())}
    assert await _as_a("POST", path, two_organizations, body) in REFUSED


@pytest.mark.isolation("POST", _SEPA + "/sepa-collections/{batch_id}/items/{item_id}/failed")
async def test_failing_another_tenants_collection_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sepa-collections/{uuid.uuid4()}/items/{uuid.uuid4()}/failed"
    assert await _as_a("POST", path, two_organizations, {"reason": "MS02"}) in REFUSED


# -- SI-16: draft - approve - send (ADR-078) -----------------------------------------------
_APPROVAL = "/v1/administrations/{administration_id}"


@pytest.mark.isolation("POST", _APPROVAL + "/sales-invoices/{invoice_id}/request-approval")
async def test_requesting_approval_for_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/request-approval"
    assert await _as_a("POST", path, two_organizations, {"note": "x"}) in REFUSED


@pytest.mark.isolation("POST", _APPROVAL + "/sales-invoices/{invoice_id}/approve")
async def test_approving_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/approve"
    assert await _as_a("POST", path, two_organizations) in REFUSED


@pytest.mark.isolation("POST", _APPROVAL + "/sales-invoices/{invoice_id}/reject")
async def test_rejecting_another_tenants_invoice_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/reject"
    assert await _as_a("POST", path, two_organizations, {"reason": "x"}) in REFUSED


@pytest.mark.isolation("GET", _APPROVAL + "/sales-invoices/{invoice_id}/approval")
async def test_reading_another_tenants_approval_status_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoices/{uuid.uuid4()}/approval"
    assert await _as_a("GET", path, two_organizations) in REFUSED


@pytest.mark.isolation("GET", _APPROVAL + "/sales-invoice-approvals")
async def test_listing_another_tenants_approvals_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", "/sales-invoice-approvals", two_organizations) in REFUSED


# -- SI-17: batch invoicing (ADR-079) ---------------------------------------------------------
_BATCHES = "/v1/administrations/{administration_id}/sales-invoice-batches"


@pytest.mark.isolation("POST", _BATCHES)
async def test_running_a_batch_in_another_tenant_is_refused(
    two_organizations: SeededTenants,
) -> None:
    body = {
        "name": "Contributie",
        "lines": [
            {
                "description": "Bijdrage",
                "quantity": "1",
                "unit_price": "250",
                "vat_treatment": "btw_21",
            }
        ],
        "entries": [{"customer_id": str(uuid.uuid4())}],
    }
    assert await _as_a("POST", "/sales-invoice-batches", two_organizations, body) in REFUSED


@pytest.mark.isolation("GET", _BATCHES)
async def test_listing_another_tenants_batches_is_refused(
    two_organizations: SeededTenants,
) -> None:
    assert await _as_a("GET", "/sales-invoice-batches", two_organizations) in REFUSED


@pytest.mark.isolation("GET", _BATCHES + "/{batch_id}")
async def test_reading_another_tenants_batch_is_refused(
    two_organizations: SeededTenants,
) -> None:
    path = f"/sales-invoice-batches/{uuid.uuid4()}"
    assert await _as_a("GET", path, two_organizations) in REFUSED

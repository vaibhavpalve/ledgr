"""The quote routes end to end - SI-08 (ADR-075).

Real HTTP, routes, services, repository, Postgres under RLS and the real
`InvoicingService` - nothing is faked. What this proves is that a converted quote is a
REAL invoice: a real draft with the quoted lines, totals the invoice computes, dated as
asked, that converting twice yields one invoice, and that the whole lifecycle holds
through the HTTP layer.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0057.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants


def _headers(tenants: SeededTenants) -> dict[str, str]:
    token = make_token(tenants.org_a, user_id=tenants.owner_a)
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


async def _call(tenants: SeededTenants, method: str, path: str, json: object = None):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            f"/v1/administrations/{tenants.admin_a}{path}",
            headers=_headers(tenants),
            json=json,
        )


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    """A customer master an invoice can be raised for (name AND a full address)."""
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, address_line1, "
        "  postal_code, city, country, delivery_channel, invoice_email) "
        "VALUES (:org, :admin, 'De Vries Holding B.V.', 'Damrak 70', '1012 LM', 'Amsterdam', "
        "  'NL', 'email', 'f@example.com') RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


def _body(customer: uuid.UUID, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "kind": "quote",
        "customer_id": str(customer),
        "subject": "Nieuwe website",
        "valid_until": (date.today() + timedelta(days=30)).isoformat(),
        "notes": "In twee termijnen",
        "lines": [
            {
                "description": "Ontwerp",
                "quantity": "10",
                "unit_price": "95",
                "vat_treatment": "btw_21",
                "discount_percent": "5",
            },
            {
                "description": "Hosting",
                "quantity": "1",
                "unit_price": "0.035",
                "vat_treatment": "btw_9",
            },
        ],
    }
    body.update(overrides)
    return body


async def _accepted(tenants: SeededTenants, customer: uuid.UUID) -> str:
    quote = (await _call(tenants, "POST", "/quotes", _body(customer))).json()
    await _call(tenants, "POST", f"/quotes/{quote['id']}/sent")
    await _call(
        tenants,
        "POST",
        f"/quotes/{quote['id']}/accept",
        {"accepted_by_name": "J. de Vries", "reference": "PO-4471"},
    )
    return str(quote["id"])


async def _invoices(tenants: SeededTenants) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT id, status, invoice_number, invoice_date, customer_id, notes "
        "  FROM sales_invoice WHERE administration_id = :admin ORDER BY created_at",
        admin=str(tenants.admin_a),
    )
    return list(result)


async def _lines(tenants: SeededTenants, invoice: uuid.UUID) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT description, quantity, unit_price, discount_percent, vat_treatment, line_net "
        "  FROM sales_invoice_line WHERE invoice_id = :i ORDER BY position",
        i=str(invoice),
    )
    return list(result)


# --- defining ---------------------------------------------------------------------------------


async def test_a_quote_is_created_numbered_and_shows_a_net_total_but_no_vat(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)

    created = await _call(two_organizations, "POST", "/quotes", _body(customer))

    assert created.status_code == 200, created.text
    quote = created.json()
    assert quote["reference"] == "OF-0001" and quote["status"] == "draft"
    # 10 x 95 less 5% = 902.50 ; 1 x 0.035 -> 0.04 : the invoice's own arithmetic.
    assert quote["net_amount"] == "902.54"
    assert quote["lines"][0]["line_net"] == "902.50"
    assert quote["lines"][1]["unit_price"] == "0.0350"  # exact, as stored
    assert "vat_amount" not in quote  # the rate is the INVOICE date's, not the quote's
    assert quote["is_expired"] is False

    fetched = await _call(two_organizations, "GET", f"/quotes/{quote['id']}")
    assert fetched.json() == quote  # create and read answer identically


async def test_an_order_confirmation_has_its_own_series(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    await _call(two_organizations, "POST", "/quotes", _body(customer))
    confirmation = await _call(
        two_organizations, "POST", "/quotes", _body(customer, kind="order_confirmation")
    )
    assert confirmation.json()["reference"] == "OB-0001"


async def test_unusable_quotes_are_refused(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    for bad in (
        _body(customer, lines=[]),
        _body(customer, valid_until="2020-01-01"),
        _body(customer, kind="invoice"),
    ):
        response = await _call(two_organizations, "POST", "/quotes", bad)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "quote_invalid"

    stranger = await _call(two_organizations, "POST", "/quotes", _body(uuid.uuid4()))
    assert stranger.status_code == 404
    assert stranger.json()["detail"]["reason"] == "customer_not_found"


async def test_a_draft_can_be_replaced_but_a_sent_quote_cannot(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = (await _call(two_organizations, "POST", "/quotes", _body(customer))).json()

    replaced = await _call(
        two_organizations,
        "PUT",
        f"/quotes/{quote['id']}",
        _body(customer, subject="Aangepast"),
    )
    assert replaced.status_code == 200 and replaced.json()["subject"] == "Aangepast"

    await _call(two_organizations, "POST", f"/quotes/{quote['id']}/sent")
    locked = await _call(two_organizations, "PUT", f"/quotes/{quote['id']}", _body(customer))
    assert locked.status_code == 409
    assert locked.json()["detail"]["reason"] == "quote_not_editable"


# --- the lifecycle -------------------------------------------------------------------------------


async def test_the_lifecycle_through_http_records_who_accepted(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote_id = await _accepted(two_organizations, customer)

    quote = (await _call(two_organizations, "GET", f"/quotes/{quote_id}")).json()

    assert quote["status"] == "accepted"
    assert quote["sent_at"] and quote["accepted_at"]
    assert quote["accepted_by_name"] == "J. de Vries"
    assert quote["acceptance_reference"] == "PO-4471"


async def test_an_illegal_move_is_a_409_naming_the_current_status(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = (await _call(two_organizations, "POST", "/quotes", _body(customer))).json()

    response = await _call(two_organizations, "POST", f"/quotes/{quote['id']}/decline")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "quote_transition_invalid"
    assert (detail["current"], detail["target"]) == ("draft", "declined")


async def test_the_list_filters_by_status(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    sent = (await _call(two_organizations, "POST", "/quotes", _body(customer))).json()
    await _call(two_organizations, "POST", f"/quotes/{sent['id']}/sent")
    await _call(two_organizations, "POST", "/quotes", _body(customer))  # stays a draft

    only_sent = await _call(two_organizations, "GET", "/quotes?status=sent")
    everything = await _call(two_organizations, "GET", "/quotes")
    bad = await _call(two_organizations, "GET", "/quotes?status=nonsense")

    assert [q["id"] for q in only_sent.json()] == [sent["id"]]
    assert len(everything.json()) == 2
    assert bad.status_code == 422


async def test_an_unknown_quote_is_a_404(two_organizations: SeededTenants) -> None:
    response = await _call(two_organizations, "GET", f"/quotes/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "quote_not_found"


# --- converting ----------------------------------------------------------------


async def test_converting_an_accepted_quote_creates_a_real_draft_with_the_quoted_lines(
    two_organizations: SeededTenants,
) -> None:
    await _world(two_organizations)  # a 2026 fiscal year
    customer = await _customer(two_organizations)
    quote_id = await _accepted(two_organizations, customer)

    response = await _call(
        two_organizations,
        "POST",
        f"/quotes/{quote_id}/convert",
        {"invoice_date": "2026-09-09"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["already_converted"] is False
    assert body["quote"]["status"] == "converted"
    assert body["quote"]["converted_invoice_id"] == body["invoice_id"]

    (invoice,) = await _invoices(two_organizations)
    assert str(invoice.id) == body["invoice_id"]  # type: ignore[attr-defined]
    # A DRAFT, not an issued invoice: no number, nothing posted, a person reviews it.
    assert invoice.status == "draft" and invoice.invoice_number is None  # type: ignore[attr-defined]
    assert invoice.invoice_date == date(2026, 9, 9)  # type: ignore[attr-defined]
    assert invoice.customer_id == customer  # type: ignore[attr-defined]
    assert invoice.notes == "In twee termijnen"  # type: ignore[attr-defined]

    lines = await _lines(two_organizations, invoice.id)  # type: ignore[attr-defined]
    assert [line.description for line in lines] == ["Ontwerp", "Hosting"]  # type: ignore[attr-defined]
    # Exactly as quoted: never re-priced, and the four decimals survive.
    assert lines[0].unit_price == Decimal("95.0000")  # type: ignore[attr-defined]
    assert lines[0].discount_percent == Decimal("5.0000")  # type: ignore[attr-defined]
    assert lines[1].unit_price == Decimal("0.0350")  # type: ignore[attr-defined]
    # And the invoice's own generated totals equal the quote's net.
    assert sum(Decimal(line.line_net) for line in lines) == Decimal("902.54")  # type: ignore[attr-defined]


async def test_converting_twice_returns_the_same_invoice_and_creates_one(
    two_organizations: SeededTenants,
) -> None:
    """A retried request must not invoice the customer twice (NFR-032)."""
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    quote_id = await _accepted(two_organizations, customer)

    first = await _call(
        two_organizations, "POST", f"/quotes/{quote_id}/convert", {"invoice_date": "2026-09-09"}
    )
    second = await _call(
        two_organizations, "POST", f"/quotes/{quote_id}/convert", {"invoice_date": "2026-09-09"}
    )

    assert second.status_code == 200
    assert second.json()["invoice_id"] == first.json()["invoice_id"]
    assert second.json()["already_converted"] is True
    assert len(await _invoices(two_organizations)) == 1  # one, not two


async def test_only_an_accepted_quote_converts(two_organizations: SeededTenants) -> None:
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    quote = (await _call(two_organizations, "POST", "/quotes", _body(customer))).json()

    response = await _call(two_organizations, "POST", f"/quotes/{quote['id']}/convert")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "quote_transition_invalid"
    assert await _invoices(two_organizations) == []


async def test_a_conversion_that_cannot_raise_the_invoice_leaves_the_quote_accepted(
    two_organizations: SeededTenants,
) -> None:
    """No `_world`, so the administration has no fiscal year: nowhere to put the
    invoice. The quote must stay accepted so it can be converted once that is fixed."""
    customer = await _customer(two_organizations)
    quote_id = await _accepted(two_organizations, customer)

    response = await _call(two_organizations, "POST", f"/quotes/{quote_id}/convert")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "quote_conversion_failed" and detail["code"] == "no_fiscal_year"
    assert "boekjaar" in detail["message"] or "fiscal year" in detail["message"]
    assert (await _call(two_organizations, "GET", f"/quotes/{quote_id}")).json()[
        "status"
    ] == "accepted"
    assert await _invoices(two_organizations) == []


async def test_a_customer_with_an_incomplete_address_blocks_the_conversion_clearly(
    two_organizations: SeededTenants,
) -> None:
    await _world(two_organizations)
    bare = await _scalar(
        two_organizations,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'Zonder adres', 'email', 'z@example.com') "
        "RETURNING id",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
    )
    quote_id = await _accepted(two_organizations, bare)

    response = await _call(two_organizations, "POST", f"/quotes/{quote_id}/convert")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "customer_details_incomplete"
    assert (await _call(two_organizations, "GET", f"/quotes/{quote_id}")).json()[
        "status"
    ] == "accepted"


async def test_an_expired_offer_cannot_be_accepted_until_its_validity_is_extended(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = (await _call(two_organizations, "POST", "/quotes", _body(customer))).json()
    # The offer lapses (a person waited too long): set its validity in the past.
    await _exec(
        two_organizations,
        "UPDATE sales_quote SET valid_until = CURRENT_DATE - 1 WHERE id = :q",
        q=quote["id"],
    )

    expired = await _call(two_organizations, "POST", f"/quotes/{quote['id']}/accept")
    assert expired.status_code == 409
    assert expired.json()["detail"]["reason"] == "quote_expired"
    assert (await _call(two_organizations, "GET", f"/quotes/{quote['id']}")).json()["is_expired"]

    extended = await _call(
        two_organizations,
        "POST",
        f"/quotes/{quote['id']}/extend",
        {"valid_until": (date.today() + timedelta(days=14)).isoformat()},
    )
    assert extended.status_code == 200 and extended.json()["is_expired"] is False
    accepted = await _call(two_organizations, "POST", f"/quotes/{quote['id']}/accept")
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"

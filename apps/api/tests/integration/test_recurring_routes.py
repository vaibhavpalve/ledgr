"""The recurring-invoice routes end to end - SI-07 (ADR-074).

Real HTTP, routes, services, repository, Postgres under RLS and the real
`InvoicingService` that a person's own invoice goes through - nothing is faked. So
what this proves is that a schedule's invoices are real invoices: real drafts with
real lines and totals, dated on their scheduled dates, numbered and posted (or not)
by the same gate.

The schedules here end on a fixed date, so the number of runs is the same whatever
day the suite runs: monthly from 2026-07-31 to 2026-08-31 is exactly two invoices.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0056.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


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
    """A customer master an invoice can be raised for: name AND a full address
    (FR-AR-003 needs one at the moment the customer is put on a document)."""
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
        "customer_id": str(customer),
        "name": "Onderhoudscontract",
        "interval_months": 1,
        "start_date": "2026-07-31",
        "end_date": "2026-08-31",  # exactly two runs, whatever day the suite runs
        "due_days": 14,
        "indexation_percent": "3.5",
        "auto_issue": False,
        "notes": "Per maand",
        "lines": [
            {
                "description": "Onderhoud",
                "quantity": "2",
                "unit_price": "49.95",
                "vat_treatment": "btw_21",
                "discount_percent": "10",
            },
            {
                "description": "Voorrijkosten",
                "quantity": "1",
                "unit_price": "0.035",
                "vat_treatment": "btw_9",
            },
        ],
    }
    body.update(overrides)
    return body


async def _invoices(tenants: SeededTenants) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT id, status, invoice_number, invoice_date, due_date, customer_id "
        "  FROM sales_invoice WHERE administration_id = :admin ORDER BY invoice_date",
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


async def test_a_schedule_is_created_and_read_back_with_what_the_next_invoice_will_charge(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)

    created = await _call(two_organizations, "POST", "/recurring-invoices", _body(customer))
    assert created.status_code == 200, created.text
    schedule = created.json()
    assert schedule["status"] == "active" and schedule["runs_generated"] == 0
    assert schedule["next_run_on"] == "2026-07-31"
    assert schedule["lines"][0]["unit_price"] == "49.9500"
    assert schedule["lines"][0]["next_unit_price"] == "49.9500"  # first year: not indexed yet

    fetched = await _call(two_organizations, "GET", f"/recurring-invoices/{schedule['id']}")
    listed = await _call(two_organizations, "GET", "/recurring-invoices")
    assert fetched.json() == schedule
    assert [s["id"] for s in listed.json()] == [schedule["id"]]


async def test_an_unrunnable_schedule_is_refused_when_saved(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)

    response = await _call(
        two_organizations, "POST", "/recurring-invoices", _body(customer, interval_months=5)
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "recurring_invalid"


async def test_a_stranger_as_the_customer_is_a_clean_404(
    two_organizations: SeededTenants,
) -> None:
    response = await _call(two_organizations, "POST", "/recurring-invoices", _body(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "customer_not_found"


# --- running ------------------------------------------------------------------------------------


async def test_a_run_generates_real_dated_drafts_with_real_lines(
    two_organizations: SeededTenants,
) -> None:
    await _world(two_organizations)  # a 2026 fiscal year
    customer = await _customer(two_organizations)
    created = (
        await _call(two_organizations, "POST", "/recurring-invoices", _body(customer))
    ).json()

    response = await _call(two_organizations, "POST", "/recurring-invoices/run")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["generated"] == 2 and body["summary"]["failed"] == 0
    assert [r["run_date"] for r in body["results"]] == ["2026-07-31", "2026-08-31"]

    first, second = await _invoices(two_organizations)
    # Each invoice is dated on ITS scheduled date - not on the day it was generated.
    assert (first.invoice_date, second.invoice_date) == (  # type: ignore[attr-defined]
        date(2026, 7, 31),
        date(2026, 8, 31),
    )
    assert first.status == "draft" and first.invoice_number is None  # type: ignore[attr-defined]
    assert first.due_date == date(2026, 8, 14)  # type: ignore[attr-defined]  # 31 Jul + 14 days
    assert first.customer_id == customer  # type: ignore[attr-defined]

    lines = await _lines(two_organizations, first.id)  # type: ignore[attr-defined]
    assert [line.description for line in lines] == ["Onderhoud", "Voorrijkosten"]  # type: ignore[attr-defined]
    # 2 x 49.95 less 10% = 89.91 (the generated line_net); and 0.035 kept its decimals.
    assert lines[0].line_net == Decimal("89.91")  # type: ignore[attr-defined]
    assert lines[1].unit_price == Decimal("0.0350")  # type: ignore[attr-defined]

    ended = (await _call(two_organizations, "GET", f"/recurring-invoices/{created['id']}")).json()
    assert ended["status"] == "ended" and ended["runs_generated"] == 2
    assert ended["next_run_on"] is None


async def test_running_again_generates_nothing_more(two_organizations: SeededTenants) -> None:
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    await _call(two_organizations, "POST", "/recurring-invoices", _body(customer))

    await _call(two_organizations, "POST", "/recurring-invoices/run")
    again = await _call(two_organizations, "POST", "/recurring-invoices/run")

    assert again.json()["results"] == []
    assert len(await _invoices(two_organizations)) == 2  # not four


async def test_a_date_in_no_fiscal_year_fails_with_a_reason_and_is_retried(
    two_organizations: SeededTenants,
) -> None:
    """No `_world`, so the administration has no fiscal year at all."""
    customer = await _customer(two_organizations)
    created = (
        await _call(two_organizations, "POST", "/recurring-invoices", _body(customer))
    ).json()

    response = await _call(two_organizations, "POST", "/recurring-invoices/run")

    (result,) = response.json()["results"]  # the first date failed, so the second was not tried
    assert result["status"] == "failed" and result["error"] == "no_fiscal_year"
    assert "boekjaar" in result["error_message"] or "fiscal year" in result["error_message"]
    assert await _invoices(two_organizations) == []
    remembered = (
        await _call(two_organizations, "GET", f"/recurring-invoices/{created['id']}")
    ).json()
    assert remembered["last_error"] == "no_fiscal_year"
    assert remembered["runs_generated"] == 0  # still due: the same date is retried


async def test_auto_issue_either_issues_or_leaves_a_draft_with_a_reason(
    two_organizations: SeededTenants,
) -> None:
    """Whichever the seed's supplier details allow - what must hold is that the
    invoice and the report AGREE, and that a refused issue burns no number."""
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    await _call(
        two_organizations,
        "POST",
        "/recurring-invoices",
        _body(customer, auto_issue=True, start_date="2026-08-31", end_date="2026-08-31"),
    )

    response = await _call(two_organizations, "POST", "/recurring-invoices/run")

    (result,) = response.json()["results"]
    assert result["status"] == "generated"
    (invoice,) = await _invoices(two_organizations)
    if result["issued"]:
        assert invoice.status == "issued" and invoice.invoice_number is not None  # type: ignore[attr-defined]
    else:
        assert result["issue_error"] is not None
        assert result["issue_error_message"]  # a sentence, never a bare code
        assert invoice.status == "draft"  # type: ignore[attr-defined]
        # The refused issue rolled its number allocation back: the series has no hole.
        assert invoice.invoice_number is None  # type: ignore[attr-defined]
        assert response.json()["summary"]["left_as_draft"] == 1


# --- editing and pausing -----------------------------------------------------
async def test_the_rhythm_locks_after_the_first_invoice_but_lines_can_change(
    two_organizations: SeededTenants,
) -> None:
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    created = (
        await _call(
            two_organizations, "POST", "/recurring-invoices", _body(customer, end_date=None)
        )
    ).json()
    await _call(two_organizations, "POST", "/recurring-invoices/run")

    locked = await _call(
        two_organizations,
        "PUT",
        f"/recurring-invoices/{created['id']}",
        _body(customer, end_date=None, interval_months=3),
    )
    assert locked.status_code == 409
    assert locked.json()["detail"]["reason"] == "recurring_locked"

    cheaper = _body(customer, end_date=None)
    cheaper["lines"] = [  # type: ignore[index]
        {"description": "Onderhoud", "quantity": "1", "unit_price": "10", "vat_treatment": "btw_21"}
    ]
    changed = await _call(two_organizations, "PUT", f"/recurring-invoices/{created['id']}", cheaper)
    assert changed.status_code == 200
    assert changed.json()["runs_generated"] > 0  # history untouched
    assert [line["description"] for line in changed.json()["lines"]] == ["Onderhoud"]
    # Invoices already generated are invoices; the edit did not touch them.
    first = (await _invoices(two_organizations))[0]
    assert len(await _lines(two_organizations, first.id)) == 2  # type: ignore[attr-defined]


async def test_pause_stops_generation_and_resume_catches_up(
    two_organizations: SeededTenants,
) -> None:
    await _world(two_organizations)
    customer = await _customer(two_organizations)
    created = (
        await _call(two_organizations, "POST", "/recurring-invoices", _body(customer))
    ).json()

    paused = await _call(two_organizations, "POST", f"/recurring-invoices/{created['id']}/pause")
    assert paused.json()["status"] == "paused"
    assert (await _call(two_organizations, "POST", "/recurring-invoices/run")).json()[
        "results"
    ] == []
    assert await _invoices(two_organizations) == []

    resumed = await _call(two_organizations, "POST", f"/recurring-invoices/{created['id']}/resume")
    assert resumed.json()["status"] == "active"
    caught_up = await _call(two_organizations, "POST", "/recurring-invoices/run")

    # A pause postpones billing; it does not waive it: both months, each dated in its own.
    assert [r["run_date"] for r in caught_up.json()["results"]] == ["2026-07-31", "2026-08-31"]


async def test_an_unknown_schedule_is_a_404(two_organizations: SeededTenants) -> None:
    response = await _call(two_organizations, "GET", f"/recurring-invoices/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "recurring_not_found"

"""The draft - approve - send workflow end to end - SI-16 (ADR-078, migration 0060).

Real HTTP, routes, real `InvoicingService` (with the real gate), real repository, real Postgres
under RLS. What this proves that fakes cannot: that the gate really sits in front of issuing,
that an approval binds to the draft's contents in the real invoice tables, that the database
holds the one-pending-request rule and the immutability of the record, and that another tenant
cannot see any of it.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0060.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from api.auth.email_verification import get_email_verification_checker
from api.db import engine as app_engine
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants, grant_role, seed_user


class _Verified:
    async def is_verified(self, user_id: uuid.UUID) -> bool:
        return True


@pytest.fixture(autouse=True)
async def _email_verified() -> AsyncIterator[None]:
    app.dependency_overrides[get_email_verification_checker] = lambda: _Verified()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_email_verification_checker, None)


async def _call(  # type: ignore[no-untyped-def]
    tenants: SeededTenants, method: str, path: str, json: object = None, *, as_user=None
):
    token = make_token(tenants.org_a, user_id=as_user or tenants.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            f"/v1/administrations/{tenants.admin_a}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


async def _patch(tenants: SeededTenants, body: dict[str, object]):  # type: ignore[no-untyped-def]
    token = make_token(tenants.org_a, user_id=tenants.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.patch(
            f"/v1/administrations/{tenants.admin_a}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
            json=body,
        )


async def _ready(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    """The shared world plus supplier details, so a draft can pass the statutory check."""
    world = await _world(tenants)
    await _exec(
        tenants,
        "UPDATE administration SET address_line1 = 'Keizersgracht 1', postal_code = '1015 CS', "
        "  city = 'Amsterdam', country = 'NL', vat_number = 'NL123456789B01', "
        "  kvk_number = '12345678' WHERE id = :id",
        id=str(tenants.admin_a),
    )
    return world


async def _draft(tenants: SeededTenants, world: dict[str, uuid.UUID]) -> str:
    """A draft the owner could issue: created through the API, so the lines are real."""
    response = await _call(
        tenants,
        "POST",
        "/sales-invoices",
        {
            "fiscal_year_id": str(world["year"]),
            "invoice_date": "2026-09-09",
            "customer_name": "De Vries Holding B.V.",
            "customer_address": "Damrak 70, 1012 LM Amsterdam",
            "customer_country": "NL",
            "lines": [
                {
                    "description": "Advies",
                    "quantity": "10",
                    "unit_price": "95",
                    "vat_treatment": "btw_21",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _bookkeeper(tenants: SeededTenants) -> uuid.UUID:
    """A firm-style drafter: a Bookkeeper on the administration, who may create and send
    invoices but does NOT hold the authority to approve them."""
    user = await seed_user(app_engine, email=f"boekhouder-{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=tenants.org_a,
        user_id=user,
        role_name="Bookkeeper",
        scope_type="administration",
        scope_id=tenants.admin_a,
        granted_by=tenants.owner_a,
    )
    return user


async def _passes_the_gate(tenants: SeededTenants, path: str, *, as_user=None) -> bool:  # type: ignore[no-untyped-def]
    """Whether an issue request got past the approval gate.

    Past it, the issue carries on into posting and archiving the PDF - which needs the
    document store's per-administration encryption key that this seed does not create, so it
    may stop there with an error. That is still 'through the gate'; what must never happen
    is the approval refusal itself, so that alone is what this reports.
    """
    try:
        response = await _call(tenants, "POST", path, as_user=as_user)
    except ValueError as exc:
        assert "no active encryption key" in str(exc), exc
        return True
    return (response.json().get("detail") or {}).get("reason") != "sales_invoice_approval_required"


async def _approvals(tenants: SeededTenants, invoice: str) -> list[str]:
    result = await _exec(
        tenants,
        "SELECT status FROM sales_invoice_approval WHERE invoice_id = :i ORDER BY requested_at",
        i=invoice,
    )
    return [row.status for row in result]


# --- the policy ----------------------------------------
async def test_the_policy_is_off_by_default_and_the_owner_can_switch_it_on(
    two_organizations: SeededTenants,
) -> None:
    off = await _patch(two_organizations, {"legal_name": "Bakker Consultancy B.V."})
    assert off.json()["invoice_approval_required"] is False

    on = await _patch(two_organizations, {"invoice_approval_required": True})
    assert on.status_code == 200 and on.json()["invoice_approval_required"] is True
    null = await _patch(two_organizations, {"invoice_approval_required": None})
    assert null.json()["invoice_approval_required"] is True  # null means "leave it"


async def test_nothing_to_request_while_the_policy_is_off(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    invoice = await _draft(two_organizations, world)

    response = await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "approval_not_required"
    status = await _call(two_organizations, "GET", f"/sales-invoices/{invoice}/approval")
    assert status.json()["required"] is False and status.json()["state"] == "none"


# --- the workflow ----------------------------------------
async def test_a_request_is_recorded_bound_to_the_draft_and_listed_for_the_owner(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)

    created = await _call(
        two_organizations,
        "POST",
        f"/sales-invoices/{invoice}/request-approval",
        {"note": "Graag vandaag nog"},
    )

    assert created.status_code == 200, created.text
    approval = created.json()["approval"]
    assert approval["status"] == "pending" and len(approval["content_hash"]) == 64
    assert approval["request_note"] == "Graag vandaag nog"

    queue = (await _call(two_organizations, "GET", "/sales-invoice-approvals")).json()
    assert [(a["invoice_id"], a["customer_name"]) for a in queue["approvals"]] == [
        (invoice, "De Vries Holding B.V.")
    ]
    status = (await _call(two_organizations, "GET", f"/sales-invoices/{invoice}/approval")).json()
    assert status["state"] == "pending"


async def test_approving_records_who_and_a_second_request_supersedes_the_first(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")

    approved = await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")

    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["status"] == "approved"
    assert approved.json()["approval"]["decided_by_user_id"] == str(two_organizations.owner_a)
    status = (await _call(two_organizations, "GET", f"/sales-invoices/{invoice}/approval")).json()
    assert status["state"] == "approved"

    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")
    assert await _approvals(two_organizations, invoice) == ["superseded", "pending"]


async def test_editing_a_draft_after_approval_makes_the_approval_stale(
    two_organizations: SeededTenants,
) -> None:
    """Bound to the real invoice tables: change a line and the approval no longer counts."""
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")

    await _exec(
        two_organizations,
        "UPDATE sales_invoice_line SET unit_price = 95000 WHERE invoice_id = :i",
        i=invoice,
    )

    status = (await _call(two_organizations, "GET", f"/sales-invoices/{invoice}/approval")).json()
    assert status["state"] == "stale"
    assert status["approval"]["status"] == "approved"  # the record is untouched
    # The owner holds the authority themselves, so the gate does not stop them.
    assert await _passes_the_gate(two_organizations, f"/sales-invoices/{invoice}/issue")


async def test_a_pending_request_that_changed_cannot_be_approved(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")
    await _exec(
        two_organizations,
        "UPDATE sales_invoice_line SET quantity = 1000 WHERE invoice_id = :i",
        i=invoice,
    )

    response = await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "approval_stale"
    assert await _approvals(two_organizations, invoice) == ["pending"]


async def test_a_rejection_keeps_its_reason_and_needs_one(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")

    blank = await _call(
        two_organizations, "POST", f"/sales-invoices/{invoice}/reject", {"reason": " "}
    )
    assert blank.status_code == 422
    assert blank.json()["detail"]["reason"] == "approval_reason_missing"

    rejected = await _call(
        two_organizations,
        "POST",
        f"/sales-invoices/{invoice}/reject",
        {"reason": "Verkeerd tarief"},
    )
    assert rejected.json()["approval"]["status"] == "rejected"
    assert rejected.json()["approval"]["decision_reason"] == "Verkeerd tarief"
    status = (await _call(two_organizations, "GET", f"/sales-invoices/{invoice}/approval")).json()
    assert status["state"] == "rejected"

    again = await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")
    assert again.status_code == 404  # nothing pending any more
    assert again.json()["detail"]["reason"] == "approval_not_found"


async def test_an_unknown_invoice_is_a_404(two_organizations: SeededTenants) -> None:
    await _patch(two_organizations, {"invoice_approval_required": True})
    response = await _call(
        two_organizations, "POST", f"/sales-invoices/{uuid.uuid4()}/request-approval"
    )
    assert response.status_code == 404


# --- the gate, as a bookkeeper meets it ----------------------------------------
async def test_a_bookkeeper_cannot_issue_until_the_owner_approves_and_cannot_approve(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    bookkeeper = await _bookkeeper(two_organizations)
    invoice = await _draft(two_organizations, world)
    issue = f"/sales-invoices/{invoice}/issue"

    blocked = await _call(two_organizations, "POST", issue, as_user=bookkeeper)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "sales_invoice_approval_required"
    assert blocked.json()["detail"]["state"] == "none"

    requested = await _call(
        two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval", as_user=bookkeeper
    )
    assert requested.status_code == 200, requested.text
    still = await _call(two_organizations, "POST", issue, as_user=bookkeeper)
    assert still.json()["detail"]["state"] == "pending"

    # The drafter cannot release: not approve, not reject, not read the owner's queue.
    for method, path, body in (
        ("POST", f"/sales-invoices/{invoice}/approve", None),
        ("POST", f"/sales-invoices/{invoice}/reject", {"reason": "x"}),
        ("GET", "/sales-invoice-approvals", None),
    ):
        refused = await _call(two_organizations, method, path, body, as_user=bookkeeper)
        assert refused.status_code == 403, path
    assert await _approvals(two_organizations, invoice) == ["pending"]


async def test_after_approval_the_drafter_gets_past_the_gate_but_not_after_an_edit(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    bookkeeper = await _bookkeeper(two_organizations)
    invoice = await _draft(two_organizations, world)
    issue = f"/sales-invoices/{invoice}/issue"
    await _call(
        two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval", as_user=bookkeeper
    )
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")

    assert await _passes_the_gate(two_organizations, issue, as_user=bookkeeper)

    await _exec(
        two_organizations,
        "UPDATE sales_invoice_line SET unit_price = 95000 WHERE invoice_id = :i",
        i=invoice,
    )
    after_edit = await _call(two_organizations, "POST", issue, as_user=bookkeeper)
    assert after_edit.status_code == 409
    assert after_edit.json()["detail"]["state"] == "stale"


async def test_with_the_policy_off_a_bookkeeper_is_not_gated(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    bookkeeper = await _bookkeeper(two_organizations)
    invoice = await _draft(two_organizations, world)

    assert await _passes_the_gate(
        two_organizations, f"/sales-invoices/{invoice}/issue", as_user=bookkeeper
    )


# --- what the database itself refuses ----------------------------------------
async def test_the_database_allows_one_pending_request_per_draft_and_freezes_the_record(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")

    insert = (
        "INSERT INTO sales_invoice_approval (organization_id, administration_id, invoice_id, "
        "  content_hash, requested_by_user_id) VALUES (:org, :admin, :i, repeat('a', 64), :user)"
    )
    with pytest.raises(Exception, match="(?i)one_pending|duplicate key"):
        await _exec(
            two_organizations,
            insert,
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            i=invoice,
            user=str(two_organizations.owner_a),
        )
    with pytest.raises(Exception, match="(?i)cannot be edited|records what was asked"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_approval SET content_hash = repeat('b', 64) "
            "WHERE invoice_id = :i",
            i=invoice,
        )
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations, "DELETE FROM sales_invoice_approval WHERE invoice_id = :i", i=invoice
        )
    # A decision is a one-way step: approved cannot go back to pending.
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/approve")
    with pytest.raises(Exception, match="(?i)cannot go from"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_approval SET status = 'pending' WHERE invoice_id = :i "
            "  AND status = 'approved'",
            i=invoice,
        )


async def test_an_issued_invoice_cannot_be_put_up_for_approval_at_the_database(
    two_organizations: SeededTenants,
) -> None:
    world = await _ready(two_organizations)
    invoice = await _draft(two_organizations, world)
    await _exec(
        two_organizations,
        "UPDATE sales_invoice SET status = 'issued' WHERE id = :id",
        id=invoice,
    )
    with pytest.raises(Exception, match="(?i)cannot be put up for approval"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_invoice_approval (organization_id, administration_id, invoice_id, "
            "  content_hash, requested_by_user_id) "
            "VALUES (:org, :admin, :i, repeat('a', 64), :user)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            i=invoice,
            user=str(two_organizations.owner_a),
        )


async def test_another_tenant_cannot_see_approvals(two_organizations: SeededTenants) -> None:
    world = await _ready(two_organizations)
    await _patch(two_organizations, {"invoice_approval_required": True})
    invoice = await _draft(two_organizations, world)
    await _call(two_organizations, "POST", f"/sales-invoices/{invoice}/request-approval")

    token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in (f"/sales-invoices/{invoice}/approval", "/sales-invoice-approvals"):
            response = await client.get(
                f"/v1/administrations/{two_organizations.admin_a}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code in {403, 404}, path

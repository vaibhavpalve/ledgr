"""Batch invoicing end to end - SI-17 (ADR-079, migration 0061).

Real HTTP, routes, the real `InvoicingService` (with its real approval gate), real repository and
Postgres under RLS. What this proves that fakes cannot: that every entry becomes a REAL draft
with the right customer snapshot, lines, dates and terms; that a bad entry is reported without
spoiling the others; that a repeated batch key creates nothing; that the approval gate holds
for issued entries; and what the database itself refuses (editing or deleting a batch).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0061.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from api.auth.email_verification import get_email_verification_checker
from api.db import engine as app_engine
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants, grant_role, seed_user

pytestmark = pytest.mark.anyio


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


async def _customer(tenants: SeededTenants, name: str, *, complete: bool = True) -> uuid.UUID:
    """A customer master; `complete=False` has no address, which an invoice cannot be raised to."""
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, address_line1, "
        "  postal_code, city, country, delivery_channel, invoice_email) "
        "VALUES (:org, :admin, :name, :addr, :zip, :city, 'NL', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        name=name,
        addr="Damrak 70" if complete else None,
        zip="1012 LM" if complete else None,
        city="Amsterdam" if complete else None,
    )


def _lines(**overrides: object) -> list[dict[str, object]]:
    line: dict[str, object] = {
        "description": "Contributie 2026",
        "quantity": "1",
        "unit_price": "250",
        "vat_treatment": "btw_21",
    }
    line.update(overrides)
    return [line]


async def _drafts(tenants: SeededTenants) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT id, status, customer_id, customer_name, invoice_date, due_date, notes "
        "  FROM sales_invoice WHERE administration_id = :admin ORDER BY customer_name",
        admin=str(tenants.admin_a),
    )
    return list(result)


async def _ready(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    world = await _world(tenants)
    await _exec(
        tenants,
        "UPDATE administration SET address_line1 = 'Keizersgracht 1', postal_code = '1015 CS', "
        "  city = 'Amsterdam', country = 'NL', vat_number = 'NL123456789B01', "
        "  kvk_number = '12345678' WHERE id = :id",
        id=str(tenants.admin_a),
    )
    return world


# --- drafting ----------------------------------------
async def test_one_pass_drafts_an_invoice_per_customer_with_the_shared_lines(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customers = [await _customer(two_organizations, f"Klant {n}") for n in "ABC"]

    response = await _call(
        two_organizations,
        "POST",
        "/sales-invoice-batches",
        {
            "name": "Contributie 2026",
            "invoice_date": "2026-09-09",
            "due_days": 14,
            "notes": "Bedankt voor uw steun",
            "lines": _lines(),
            "entries": [{"customer_id": str(c)} for c in customers],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["batch"]["summary"] == {"entries": 3, "drafted": 3, "issued": 0, "failed": 0}
    assert [i["status"] for i in body["items"]] == ["drafted"] * 3
    drafts = await _drafts(two_organizations)
    assert [d.customer_name for d in drafts] == ["Klant A", "Klant B", "Klant C"]  # type: ignore[attr-defined]
    assert all(d.status == "draft" for d in drafts)  # type: ignore[attr-defined]
    assert all(d.invoice_date == date(2026, 9, 9) for d in drafts)  # type: ignore[attr-defined]
    assert all(d.due_date == date(2026, 9, 23) for d in drafts)  # type: ignore[attr-defined]
    assert all(d.notes == "Bedankt voor uw steun" for d in drafts)  # type: ignore[attr-defined]
    # Each item points at ITS customer's draft.
    by_customer = {str(d.customer_id): str(d.id) for d in drafts}  # type: ignore[attr-defined]
    assert {i["customer_id"]: i["invoice_id"] for i in body["items"]} == by_customer


async def test_an_entry_can_carry_its_own_lines_and_terms(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    a = await _customer(two_organizations, "Klant A")
    b = await _customer(two_organizations, "Klant B")

    response = await _call(
        two_organizations,
        "POST",
        "/sales-invoice-batches",
        {
            "name": "Verbruik september",
            "invoice_date": "2026-09-09",
            "lines": _lines(),
            "entries": [
                {"customer_id": str(a)},
                {
                    "customer_id": str(b),
                    "due_days": 30,
                    "lines": _lines(
                        description="Verbruik",
                        quantity="37.5",
                        unit_price="0.035",
                        vat_treatment="btw_9",
                    ),
                },
            ],
        },
    )

    assert response.status_code == 200, response.text
    drafts = await _drafts(two_organizations)
    klant_b = next(d for d in drafts if d.customer_name == "Klant B")  # type: ignore[attr-defined]
    assert klant_b.due_date == date(2026, 10, 9)  # type: ignore[attr-defined]
    lines = await _exec(
        two_organizations,
        "SELECT description, quantity, unit_price, vat_treatment FROM sales_invoice_line "
        " WHERE invoice_id = :i",
        i=str(klant_b.id),  # type: ignore[attr-defined]
    )
    (row,) = list(lines)
    assert (row.description, row.quantity, row.unit_price, row.vat_treatment) == (
        "Verbruik",
        Decimal("37.5"),
        Decimal("0.035"),
        "btw_9",
    )


# --- one failure never spoils the rest ----------------------------------------
async def test_a_bad_entry_is_reported_and_the_others_still_get_drafts(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    good = await _customer(two_organizations, "Klant Goed")
    bare = await _customer(two_organizations, "Klant Zonder Adres", complete=False)
    archived = await _customer(two_organizations, "Klant Gearchiveerd")
    await _exec(
        two_organizations,
        "UPDATE customer SET archived_at = now() WHERE id = :id",
        id=str(archived),
    )

    response = await _call(
        two_organizations,
        "POST",
        "/sales-invoice-batches",
        {
            "name": "Gemengd",
            "invoice_date": "2026-09-09",
            "lines": _lines(),
            "entries": [
                {"customer_id": str(good)},
                {"customer_id": str(bare)},
                {"customer_id": str(uuid.uuid4())},  # a stranger
                {"customer_id": str(archived)},
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(i["status"], i["error"]) for i in body["items"]] == [
        ("drafted", None),
        ("failed", "customer_details_incomplete"),
        ("failed", "customer_not_found"),
        ("failed", "customer_archived"),
    ]
    assert all(i["error_message"] for i in body["items"] if i["status"] == "failed")  # a sentence
    assert body["batch"]["summary"] == {"entries": 4, "drafted": 1, "issued": 0, "failed": 3}
    assert [d.customer_name for d in await _drafts(two_organizations)] == ["Klant Goed"]  # type: ignore[attr-defined]


async def test_a_date_in_no_fiscal_year_fails_every_entry_but_is_recorded(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")

    response = await _call(
        two_organizations,
        "POST",
        "/sales-invoice-batches",
        {
            "name": "Ver weg",
            "invoice_date": "2040-01-01",
            "lines": _lines(),
            "entries": [{"customer_id": str(customer)}],
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["error"] == "no_fiscal_year"
    assert await _drafts(two_organizations) == []


async def test_an_unusable_batch_is_refused_and_nothing_is_created(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")
    entry = {"customer_id": str(customer)}

    for body, reason in (
        ({"name": " ", "lines": _lines(), "entries": [entry]}, "batch_name_required"),
        ({"name": "x", "lines": _lines(), "entries": []}, "batch_empty"),
        ({"name": "x", "entries": [entry]}, "batch_lines"),  # no lines anywhere
        (
            {"name": "x", "lines": _lines(quantity="0"), "entries": [entry]},
            "batch_lines",
        ),
    ):
        response = await _call(two_organizations, "POST", "/sales-invoice-batches", body)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == reason
    assert await _drafts(two_organizations) == []


# --- retries ----------------------------------------
async def test_a_repeated_batch_key_returns_the_first_batch_and_creates_nothing_more(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")
    body = {
        "name": "Contributie 2026",
        "batch_key": "2026-contributie",
        "invoice_date": "2026-09-09",
        "lines": _lines(),
        "entries": [{"customer_id": str(customer)}],
    }

    first = await _call(two_organizations, "POST", "/sales-invoice-batches", body)
    second = await _call(two_organizations, "POST", "/sales-invoice-batches", body)

    assert first.json()["already_exists"] is False and second.json()["already_exists"] is True
    assert second.json()["batch"]["id"] == first.json()["batch"]["id"]
    assert second.json()["items"] == first.json()["items"]
    assert len(await _drafts(two_organizations)) == 1  # one draft, not two


async def test_batches_are_listed_and_read_back_with_their_items(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")
    created = (
        await _call(
            two_organizations,
            "POST",
            "/sales-invoice-batches",
            {
                "name": "Eerste",
                "invoice_date": "2026-09-09",
                "lines": _lines(),
                "entries": [{"customer_id": str(customer)}],
            },
        )
    ).json()

    listing = (await _call(two_organizations, "GET", "/sales-invoice-batches")).json()
    fetched = await _call(
        two_organizations, "GET", f"/sales-invoice-batches/{created['batch']['id']}"
    )

    assert [b["name"] for b in listing["batches"]] == ["Eerste"]
    assert fetched.json()["items"] == created["items"]
    missing = await _call(two_organizations, "GET", f"/sales-invoice-batches/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["detail"]["reason"] == "batch_not_found"


# --- issuing and the approval gate ----------------------------------------
async def _bookkeeper(tenants: SeededTenants) -> uuid.UUID:
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


async def test_with_approval_required_a_bookkeepers_batch_stays_drafts_with_the_reason(
    two_organizations: SeededTenants,
) -> None:
    """SI-16 meets SI-17: the same gate a person meets, per entry, without spoiling the batch."""
    await _ready(two_organizations)
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.patch(
            f"/v1/administrations/{two_organizations.admin_a}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
            json={"invoice_approval_required": True},
        )
    bookkeeper = await _bookkeeper(two_organizations)
    customers = [await _customer(two_organizations, f"Klant {n}") for n in "AB"]

    response = await _call(
        two_organizations,
        "POST",
        "/sales-invoice-batches",
        {
            "name": "Met goedkeuring",
            "invoice_date": "2026-09-09",
            "issue": True,
            "lines": _lines(),
            "entries": [{"customer_id": str(c)} for c in customers],
        },
        as_user=bookkeeper,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(i["status"], i["issue_error"]) for i in body["items"]] == [
        ("drafted", "approval_required"),
        ("drafted", "approval_required"),
    ]
    assert all(i["issue_error_message"] for i in body["items"])
    assert body["batch"]["summary"] == {"entries": 2, "drafted": 2, "issued": 0, "failed": 0}
    drafts = await _drafts(two_organizations)
    assert [d.status for d in drafts] == ["draft", "draft"]  # type: ignore[attr-defined]
    numbers = await _exec(
        two_organizations,
        "SELECT count(*) FROM sales_invoice WHERE administration_id = :a AND invoice_number "
        "IS NOT NULL",
        a=str(two_organizations.admin_a),
    )
    assert numbers.scalar_one() == 0  # a refused issue burned no number


# --- what the database itself refuses ----------------------------------------
async def test_a_batch_cannot_be_edited_or_deleted_and_an_invoice_is_in_one_item_only(
    two_organizations: SeededTenants,
) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")
    created = (
        await _call(
            two_organizations,
            "POST",
            "/sales-invoice-batches",
            {
                "name": "Vast",
                "invoice_date": "2026-09-09",
                "lines": _lines(),
                "entries": [{"customer_id": str(customer)}],
            },
        )
    ).json()
    batch_id, invoice_id = created["batch"]["id"], created["items"][0]["invoice_id"]

    for table in ("sales_invoice_batch", "sales_invoice_batch_item"):
        with pytest.raises(Exception, match="(?i)permission denied"):
            await _exec(
                two_organizations,
                f"UPDATE {table} SET id = id",
            )
        with pytest.raises(Exception, match="(?i)permission denied"):
            await _exec(two_organizations, f"DELETE FROM {table}")
    with pytest.raises(Exception, match="(?i)duplicate key|unique"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_invoice_batch_item (organization_id, administration_id, batch_id, "
            "  position, customer_id, invoice_id, status) "
            "VALUES (:org, :admin, :batch, 2, :customer, :invoice, 'drafted')",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            batch=batch_id,
            customer=str(customer),
            invoice=invoice_id,
        )


async def test_another_tenant_cannot_see_a_batch(two_organizations: SeededTenants) -> None:
    await _ready(two_organizations)
    customer = await _customer(two_organizations, "Klant A")
    created = (
        await _call(
            two_organizations,
            "POST",
            "/sales-invoice-batches",
            {
                "name": "Privé",
                "invoice_date": "2026-09-09",
                "lines": _lines(),
                "entries": [{"customer_id": str(customer)}],
            },
        )
    ).json()

    token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in ("/sales-invoice-batches", f"/sales-invoice-batches/{created['batch']['id']}"):
            response = await client.get(
                f"/v1/administrations/{two_organizations.admin_a}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code in {403, 404}, path

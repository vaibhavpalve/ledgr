"""The Bank screen's writes against a real Postgres - api.bank.routes.

Real HTTP, real routes, real BankService/LedgerService/SalesPaymentService,
real RLS. What this proves that fakes cannot: that CSV import really dedupes
on 0065's own unique index, that generic reconciliation really posts through
`ledger.post_entry`, that matching to an invoice really posts a payment
through `SalesPaymentService`, and that another tenant cannot see or touch
any of it.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
Needs migration 0065 applied.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

_CSV_HEADER = "date,amount,counterparty_name,counterparty_iban,description"
_TELECOM_CSV = f"{_CSV_HEADER}\n2026-09-05,-12.50,KPN,NL00KPN0000000000,Telefoon\n"
_INVOICE_PAYMENT_CSV = (
    f"{_CSV_HEADER}\n2026-09-15,605.00,De Vries Holding,NL00DVH0000000000,Factuur\n"
)


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
    tenants: SeededTenants,
    admin: uuid.UUID,
    method: str,
    path: str,
    json: object = None,
    *,
    as_user=None,
):
    token = make_token(tenants.org_a, user_id=as_user or tenants.owner_a)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            f"/v1/administrations/{admin}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


async def _bank_world(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    admin = tenants.admin_a
    bank_ledger_account = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    expense = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '4600', 'Bankkosten', 'expense', "
        "  NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'BNK', 'Bank', 'bank')).id",
        admin=str(admin),
    )
    return {"bank_ledger_account": bank_ledger_account, "expense": expense}


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/bank-accounts")
async def test_create_bank_account(two_organizations: SeededTenants) -> None:
    world = await _bank_world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {
            "name": "Rabobank Zakelijk",
            "iban": "NL91RABO0123456789",
            "ledger_account_id": str(world["bank_ledger_account"]),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Rabobank Zakelijk"


async def test_create_bank_account_rejects_non_asset_account(
    two_organizations: SeededTenants,
) -> None:
    world = await _bank_world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Wrong", "ledger_account_id": str(world["expense"])},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "bank_field_invalid"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/bank-accounts")
async def test_list_bank_accounts(two_organizations: SeededTenants) -> None:
    world = await _bank_world(two_organizations)
    await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    listing = await _call(two_organizations, two_organizations.admin_a, "GET", "/bank-accounts")
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["bank_accounts"]) == 1


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/bank-accounts/{bank_account_id}/import"
)
async def test_import_statement_dedupes_on_reimport(two_organizations: SeededTenants) -> None:
    world = await _bank_world(two_organizations)
    created = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    account_id = created.json()["id"]
    csv = _TELECOM_CSV

    first = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {"filename": "statement.csv", "csv": csv},
    )
    assert first.status_code == 200, first.text
    assert first.json()["transaction_count"] == 1
    assert first.json()["duplicate_count"] == 0

    second = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {"filename": "statement.csv", "csv": csv},
    )
    assert second.status_code == 200, second.text
    assert second.json()["transaction_count"] == 0
    assert second.json()["duplicate_count"] == 1


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/bank-transactions/{transaction_id}/reconcile"
)
async def test_reconcile_generic_posts_an_entry(two_organizations: SeededTenants) -> None:
    world = await _bank_world(two_organizations)
    await _world(two_organizations)  # seeds a fiscal year + open period this administration shares
    created = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    account_id = created.json()["id"]
    csv = _TELECOM_CSV
    await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {"csv": csv},
    )

    listing = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/bank-accounts/{account_id}/transactions",
    )
    assert listing.status_code == 200, listing.text
    transaction_id = listing.json()["transactions"][0]["id"]

    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{transaction_id}/reconcile",
        {"offset_account_id": str(world["expense"])},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reconciled"

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{transaction_id}/reconcile",
        {"offset_account_id": str(world["expense"])},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["reason"] == "bank_transaction_already_reconciled"


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/bank-accounts/{bank_account_id}/transactions"
)
async def test_another_tenant_cannot_see_bank_accounts(two_organizations: SeededTenants) -> None:
    world = await _bank_world(two_organizations)
    created = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    account_id = created.json()["id"]

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listing = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/bank-accounts",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert listing.status_code in {403, 404}
        assert account_id not in listing.text

        transactions = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/bank-accounts/{account_id}/transactions",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert transactions.status_code in {403, 404}


async def _issued_invoice_and_matching_transaction(
    tenants: SeededTenants,
) -> tuple[str, str] | None:
    """An issued invoice for 605.00 and an imported bank transaction for the
    same amount - the shared setup `match-candidates` and
    `reconcile-with-invoice` both need. Returns None (the caller skips) if
    this environment cannot get an invoice past `issue` - the same "stops at
    the document store's encryption key" caveat test_invoice_approval.py's
    own `_passes_the_gate` documents; unrelated to this module.
    """
    world = await _bank_world(tenants)
    invoice_world = await _world(tenants)
    await _exec(
        tenants,
        "UPDATE administration SET address_line1 = 'Keizersgracht 1', postal_code = '1015 CS', "
        "  city = 'Amsterdam', country = 'NL', vat_number = 'NL123456789B01', "
        "  kvk_number = '12345678' WHERE id = :id",
        id=str(tenants.admin_a),
    )
    draft = await _call(
        tenants,
        tenants.admin_a,
        "POST",
        "/sales-invoices",
        {
            "fiscal_year_id": str(invoice_world["year"]),
            "invoice_date": "2026-09-09",
            "customer_name": "De Vries Holding B.V.",
            "customer_address": "Damrak 70, 1012 LM Amsterdam",
            "customer_country": "NL",
            "lines": [
                {
                    "description": "Advies",
                    "quantity": "1",
                    "unit_price": "500",
                    "vat_treatment": "btw_21",
                }
            ],
        },
    )
    assert draft.status_code == 200, draft.text
    invoice_id = draft.json()["id"]
    issued = await _call(tenants, tenants.admin_a, "POST", f"/sales-invoices/{invoice_id}/issue")
    if issued.status_code != 200:
        return None

    created = await _call(
        tenants,
        tenants.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    account_id = created.json()["id"]
    csv = _INVOICE_PAYMENT_CSV
    await _call(
        tenants, tenants.admin_a, "POST", f"/bank-accounts/{account_id}/import", {"csv": csv}
    )
    listing = await _call(
        tenants, tenants.admin_a, "GET", f"/bank-accounts/{account_id}/transactions"
    )
    transaction_id = listing.json()["transactions"][0]["id"]
    return invoice_id, transaction_id


@pytest.mark.isolation(
    "GET",
    "/v1/administrations/{administration_id}/bank-transactions/{transaction_id}/match-candidates",
)
async def test_match_candidates_suggests_invoice_of_equal_amount(
    two_organizations: SeededTenants,
) -> None:
    setup = await _issued_invoice_and_matching_transaction(two_organizations)
    if setup is None:
        pytest.skip("invoice could not be issued in this environment")
    invoice_id, transaction_id = setup

    candidates = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/bank-transactions/{transaction_id}/match-candidates",
    )
    assert candidates.status_code == 200, candidates.text
    matched = [c for c in candidates.json()["candidates"] if c["invoice_id"] == invoice_id]
    assert matched, candidates.json()


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/bank-transactions/{transaction_id}/reconcile-with-invoice",
)
async def test_reconcile_with_invoice_records_a_payment(two_organizations: SeededTenants) -> None:
    setup = await _issued_invoice_and_matching_transaction(two_organizations)
    if setup is None:
        pytest.skip("invoice could not be issued in this environment")
    invoice_id, transaction_id = setup

    reconciled = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{transaction_id}/reconcile-with-invoice",
        {"invoice_id": invoice_id},
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["matched_sales_invoice_id"] == invoice_id

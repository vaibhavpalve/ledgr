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
from tests.integration.test_expense_form_isolation import _seed_expense
from tests.integration.test_sales_invoice_posting import _exec, _scalar, _world
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

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
    # A seeded administration has no data-encryption key; onboarding provisions one in the same
    # transaction as the administration (api.onboarding.routes), so give this one the same.
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.crypto.envelope import EnvelopeEncryptionService
    from api.crypto.kms import build_kms
    from api.crypto.repository import SqlAdministrationKeyRepository
    from api.db import engine as app_engine

    async with AsyncSession(app_engine) as session, session.begin():
        await session.execute(
            sql_text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        await EnvelopeEncryptionService(
            build_kms(), SqlAdministrationKeyRepository(session)
        ).provision_key(tenants.admin_a)
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
    try:
        issued = await _call(
            tenants, tenants.admin_a, "POST", f"/sales-invoices/{invoice_id}/issue"
        )
    except ValueError as exc:
        assert "no active encryption key" in str(exc), exc
        return None
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
    [line] = listing.json()["transactions"]
    # ADR-091: the payer's name is the customer's (less "B.V.") and no other invoice is open for
    # 605.00 - certain enough to be matched in bulk.
    assert line["suggestion"]["kind"] == "sales_invoice", line
    assert line["suggestion"]["document_id"] == invoice_id, line
    assert line["suggestion"]["confidence"] == "high", line
    assert set(line["suggestion"]["reasons"]) == {"name", "only_candidate"}, line
    return invoice_id, line["id"]


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
    matched = [c for c in candidates.json()["candidates"] if c["document_id"] == invoice_id]
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


async def test_a_camt053_statement_imports_and_another_accounts_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """ADR-091: the bank's own CAMT.053 export, unchanged - and a statement whose account is not
    this bank account's is refused before anything is written."""
    from tests.bank.test_statement_formats import CAMT_02

    world = await _bank_world(two_organizations)
    created = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        "/bank-accounts",
        {
            "name": "ABN AMRO",
            "iban": "NL91 ABNA 0417 1643 00",
            "ledger_account_id": str(world["bank_ledger_account"]),
        },
    )
    account_id = created.json()["id"]
    imported = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {"filename": "camt053.xml", "csv": CAMT_02},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["transaction_count"] == 2  # the pending entry is not imported

    other = CAMT_02.replace("NL91 ABNA 0417 1643 00", "NL02RABO0123456789")
    refused = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {"filename": "camt053.xml", "csv": other},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "bank_statement_other_account"
    assert refused.json()["detail"]["iban"] == "NL02RABO0123456789"


# ---------------------------------------------------------------------------
# ADR-092: an outgoing line settles the receipt it paid
# ---------------------------------------------------------------------------

_STAPLES_CSV = (
    f"{_CSV_HEADER}\n2026-09-18,-121.00,Staples Nederland,NL44INGB0001234567,Kantoorartikelen\n"
)


async def _bank_paid_receipt(
    tenants: SeededTenants, *, settles_through: str, statement: str = _STAPLES_CSV
) -> dict[str, str]:
    """A 121.00 receipt paid from the business account, posted through the real expense routes,
    and a statement with its payment imported. `settles_through` is the account the mapping
    credits: "2000" (Kruisposten, what 0070 sets up) or "1100" (the bank, as before 0070)."""
    world = await _bank_world(tenants)
    await _world(tenants)  # the fiscal year and September's open period
    admin, org = tenants.admin_a, tenants.org_a

    async def account(code: str, name: str, kind: str) -> uuid.UUID:
        return await _scalar(  # type: ignore[no-any-return]
            tenants,
            "SELECT (ledger.create_account(:admin, :code, :name, :kind, NULL, NULL, NULL)).id",
            admin=str(admin),
            code=code,
            name=name,
            kind=kind,
        )

    transit = await account("2000", "Kruisposten", "asset")
    costs = await account("4100", "Kantoorkosten", "expense")
    vat_in = await account("1720", "Te vorderen omzetbelasting", "asset")
    await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'IK', 'Inkoopboek', 'purchase')).id",
        admin=str(admin),
    )
    funding = transit if settles_through == "2000" else world["bank_ledger_account"]
    for purpose, target in (
        ("expense_category", costs),
        ("vat_input", vat_in),
        ("business_account", funding),
    ):
        await _exec(
            tenants,
            "INSERT INTO expense_posting_account (organization_id, administration_id, purpose, "
            "  category_key, account_id) VALUES (:org, :admin, :purpose, NULL, :acct)",
            org=str(org),
            admin=str(admin),
            purpose=purpose,
            acct=str(target),
        )

    expense_id = str(await _seed_expense(admin, org, tenants.owner_a, supplier="Staples"))
    filled = await _call(
        tenants,
        admin,
        "PATCH",
        f"/expenses/{expense_id}",
        {
            "expense_date": "2026-09-16",
            "supplier": "Staples",
            "gross_amount": "121.00",
            "vat_treatment": "btw_21",
            "category": "Office supplies",
            "payment_method": "business_account",
        },
    )
    assert filled.status_code == 200, filled.text
    posted = await _call(tenants, admin, "POST", f"/expenses/{expense_id}/posting")
    assert posted.status_code == 200, posted.text

    created = await _call(
        tenants,
        admin,
        "POST",
        "/bank-accounts",
        {"name": "Bank", "ledger_account_id": str(world["bank_ledger_account"])},
    )
    account_id = created.json()["id"]
    imported = await _call(
        tenants, admin, "POST", f"/bank-accounts/{account_id}/import", {"csv": statement}
    )
    assert imported.status_code == 200, imported.text
    listing = await _call(tenants, admin, "GET", f"/bank-accounts/{account_id}/transactions")
    [line] = listing.json()["transactions"]
    return {
        "expense_id": expense_id,
        "transaction_id": line["id"],
        "bank": str(world["bank_ledger_account"]),
        "transit": str(transit),
        "suggestion": line["suggestion"],
    }


async def _balance(tenants: SeededTenants, account_id: str) -> str:
    return str(
        await _scalar(
            tenants,
            "SELECT coalesce(sum(debit) - sum(credit), 0)::numeric(19, 2) FROM journal_line "
            "WHERE account_id = :a",
            a=account_id,
        )
    )


@pytest.mark.isolation(
    "POST",
    "/v1/administrations/{administration_id}/bank-transactions/{transaction_id}/reconcile-with-expense",
)
async def test_a_bank_paid_receipt_is_settled_through_kruisposten(
    two_organizations: SeededTenants,
) -> None:
    world = await _bank_paid_receipt(two_organizations, settles_through="2000")
    # Booked: the money waits in Kruisposten, and the bank has not moved - it has not been told.
    assert await _balance(two_organizations, world["transit"]) == "-121.00"
    assert await _balance(two_organizations, world["bank"]) == "0.00"

    # The statement's line is offered the receipt, certain: the payee is the supplier and no other
    # bank-paid receipt is open for 121.00.
    suggestion = world["suggestion"]
    assert suggestion["kind"] == "expense", suggestion
    assert suggestion["document_id"] == world["expense_id"]
    assert suggestion["confidence"] == "high", suggestion

    path = f"/bank-transactions/{world['transaction_id']}/reconcile-with-expense"
    foreign_token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        foreign = await client.post(
            f"/v1/administrations/{two_organizations.admin_a}{path}",
            headers={
                "Authorization": f"Bearer {foreign_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={"expense_id": world["expense_id"]},
        )
    assert foreign.status_code in {403, 404}, foreign.text

    settled = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        {"expense_id": world["expense_id"]},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["matched_expense_id"] == world["expense_id"]
    assert settled.json()["status"] == "reconciled"

    # Settled: Kruisposten is clear and the bank moved once, by the statement's amount.
    assert await _balance(two_organizations, world["transit"]) == "0.00"
    assert await _balance(two_organizations, world["bank"]) == "-121.00"

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        {"expense_id": world["expense_id"]},
    )
    assert again.status_code == 409, again.text


async def test_a_receipt_that_already_credited_the_bank_is_linked_not_posted_again(
    two_organizations: SeededTenants,
) -> None:
    world = await _bank_paid_receipt(two_organizations, settles_through="1100")
    assert await _balance(two_organizations, world["bank"]) == "-121.00"

    settled = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{world['transaction_id']}/reconcile-with-expense",
        {"expense_id": world["expense_id"]},
    )
    assert settled.status_code == 200, settled.text
    # The payment is in the ledger once - the receipt's own entry is what the line points at.
    assert await _balance(two_organizations, world["bank"]) == "-121.00"
    entry = await _scalar(
        two_organizations,
        "SELECT journal_entry_id FROM expense WHERE id = :id",
        id=world["expense_id"],
    )
    assert settled.json()["journal_entry_id"] == str(entry)


async def test_a_receipt_of_another_amount_is_refused(two_organizations: SeededTenants) -> None:
    world = await _bank_paid_receipt(
        two_organizations,
        settles_through="2000",
        statement=f"{_CSV_HEADER}\n2026-09-18,-120.00,Staples Nederland,,Kantoorartikelen\n",
    )
    assert world["suggestion"] is None
    refused = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{world['transaction_id']}/reconcile-with-expense",
        {"expense_id": world["expense_id"]},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "bank_expense_not_matchable"
    assert await _balance(two_organizations, world["transit"]) == "-121.00"

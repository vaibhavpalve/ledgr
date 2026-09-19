"""Bad-debt write-off end to end - SI-10 (ADR-076, migration 0058).

Real HTTP, real routes, real services, real ledger, real Postgres under RLS. What this proves
that fakes cannot: that the entries balance in the real ledger and leave the debtor's
sub-ledger at zero, that a written-off invoice stops being chased and reported, that the
statement still reconciles to the sub-ledger, that the VAT reclaim lands on the VAT account
tagged with its treatment, that voiding reverses everything, and what the database itself
refuses (partial write-off, wrong account type, editing a record, deleting one).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0058.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_sales_invoice_posting import (
    _customer,
    _document,
    _exec,
    _party,
    _post,
    _scalar,
    _world,
)
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

TODAY = date.today()
LONG_OVERDUE = "2025-08-01"  # more than 12 months before any real "today" this suite runs


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


async def _issued_invoice(
    tenants: SeededTenants,
    world: dict[str, uuid.UUID],
    party: uuid.UUID,
    customer: uuid.UUID,
    *,
    due: str,
) -> uuid.UUID:
    """Like `_issued_and_posted`, but the invoice is dated two weeks before its due date, in a
    fiscal year that fits it - the shared seed pins every invoice to 2026-09-09, which would
    leave no way to ask for one that fell due more than a year ago."""
    due_date = date.fromisoformat(due)
    invoice_date = due_date - timedelta(days=14)
    if invoice_date.year == 2026:
        year = world["year"]
    else:
        year = await _scalar(
            tenants,
            "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
            "VALUES (:org, :admin, :start, :end) RETURNING id",
            org=str(tenants.org_a),
            admin=str(tenants.admin_a),
            start=date(invoice_date.year, 1, 1),
            end=date(invoice_date.year, 12, 31),
        )
    invoice = await _scalar(
        tenants,
        "INSERT INTO sales_invoice (organization_id, administration_id, fiscal_year_id, "
        "  invoice_date, due_date, customer_id, customer_name, customer_address, "
        "  customer_country, customer_language) "
        "VALUES (:org, :admin, :year, :inv, :due, :customer, 'De Vries Holding B.V.', "
        "  'Damrak 70', 'NL', 'nl') RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        year=str(year),
        inv=invoice_date,
        due=due_date,
        customer=str(customer),
    )
    entry = await _post(tenants, world, party_id=party)
    document = await _document(tenants, world)
    await _exec(
        tenants, "UPDATE sales_invoice SET status = 'issued' WHERE id = :id", id=str(invoice)
    )
    await _exec(
        tenants,
        "UPDATE sales_invoice SET journal_entry_id = :entry, document_id = :document, "
        "  posted_at = now() WHERE id = :id",
        entry=str(entry),
        document=str(document),
        id=str(invoice),
    )
    return invoice


async def _owed(tenants: SeededTenants, *, due: str = LONG_OVERDUE) -> dict[str, uuid.UUID]:
    """An issued, posted invoice of 1210.00 (1000.00 + 210.00 at 21%) whose debtor the
    services can find, plus the memorial journal and expense account a write-off needs."""
    world = await _world(tenants)
    party = await _party(tenants)
    customer = await _customer(tenants)
    await _exec(
        tenants,
        "UPDATE customer SET subledger_party_id = :party WHERE id = :id",
        party=str(party),
        id=str(customer),
    )
    invoice = await _issued_invoice(tenants, world, party, customer, due=due)
    await _exec(
        tenants,
        "INSERT INTO sales_invoice_vat_total (invoice_id, organization_id, "
        "  administration_id, vat_treatment, rate, taxable_amount, vat_amount) "
        "VALUES (:invoice, :org, :admin, 'btw_21', 21, 1000.00, 210.00)",
        invoice=str(invoice),
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )
    expense = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '4990', 'Afschrijving oninbare vorderingen', "
        "  'expense', NULL, NULL, NULL)).id",
        admin=str(tenants.admin_a),
    )
    memorial = await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'MEM', 'Memoriaal', 'memorial')).id",
        admin=str(tenants.admin_a),
    )
    bank = await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(tenants.admin_a),
    )
    await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'BNK', 'Bank', 'bank')).id",
        admin=str(tenants.admin_a),
    )
    return {
        **world,
        "party": party,
        "customer": customer,
        "invoice": invoice,
        "expense": expense,
        "memorial": memorial,
        "bank": bank,
    }


def _body(owed: dict[str, uuid.UUID], **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "expense_account_id": str(owed["expense"]),
        "reason": "Klant onbereikbaar, deurwaarder zonder resultaat",
    }
    body.update(overrides)
    return body


def _path(owed: dict[str, uuid.UUID], suffix: str = "") -> str:
    return f"/sales-invoices/{owed['invoice']}/write-offs{suffix}"


async def _debtor_balance(tenants: SeededTenants, party: uuid.UUID) -> Decimal:
    result = await _exec(
        tenants,
        "SELECT coalesce(sum(debit), 0) - coalesce(sum(credit), 0) "
        "  FROM journal_line WHERE subledger_party_id = :party",
        party=str(party),
    )
    return Decimal(result.scalar_one())


async def _account_movement(tenants: SeededTenants, account: uuid.UUID) -> tuple[Decimal, Decimal]:
    result = await _exec(
        tenants,
        "SELECT coalesce(sum(debit), 0), coalesce(sum(credit), 0) "
        "  FROM journal_line WHERE account_id = :account",
        account=str(account),
    )
    debit, credit = result.one()
    return Decimal(debit), Decimal(credit)


# --- writing off ----------------------------------------
async def test_a_write_off_clears_the_balance_and_the_debtors_sub_ledger(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")

    response = await _call(two_organizations, "POST", _path(owed), _body(owed))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["write_off"]["amount"] == "1210.00"
    assert body["write_off"]["vat_amount"] == "210.00"
    assert body["write_off"]["vat_reclaimed_on"] is None  # a separate step
    assert body["balance"]["outstanding_amount"] == "0.00"
    assert body["balance"]["written_off_amount"] == "1210.00"
    assert body["balance"]["state"] == "written_off"
    # The loss is in the expense account and the debtor owes nothing, in the real ledger.
    assert await _account_movement(two_organizations, owed["expense"]) == (
        Decimal("1210.00"),
        Decimal("0.00"),
    )
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("0.00")


async def test_a_written_off_invoice_is_no_longer_chased_or_reported(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    before = await _call(two_organizations, "GET", "/receivables/ageing")
    assert before.json()["grand_total"] == "1210.00"
    overview_before = await _call(two_organizations, "GET", "/dunning")
    assert overview_before.status_code == 200

    await _call(two_organizations, "POST", _path(owed), _body(owed))

    after = await _call(two_organizations, "GET", "/receivables/ageing")
    assert after.json()["grand_total"] == "0.00"
    overview_after = (await _call(two_organizations, "GET", "/dunning")).json()
    assert str(owed["invoice"]) not in str(overview_after)


async def test_the_statement_shows_the_write_off_and_closes_at_the_sub_ledger_balance(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    await _call(two_organizations, "POST", _path(owed), _body(owed))

    response = await _call(
        two_organizations,
        "GET",
        f"/customers/{owed['customer']}/statement?from=2020-01-01&to=2099-12-31",
    )

    statement = response.json()
    kinds = [line["kind"] for line in statement["lines"]]
    assert kinds == ["invoice", "write_off"]
    assert statement["lines"][1]["label"] == "Afgeschreven als oninbaar"
    assert Decimal(statement["closing_balance"]) == await _debtor_balance(
        two_organizations, owed["party"]
    )
    assert statement["closing_balance"] == "0.00"


async def test_nothing_is_written_off_twice(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    await _call(two_organizations, "POST", _path(owed), _body(owed))

    again = await _call(two_organizations, "POST", _path(owed), _body(owed))

    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "write_off_nothing_outstanding"
    assert await _account_movement(two_organizations, owed["expense"]) == (
        Decimal("1210.00"),
        Decimal("0.00"),
    )


async def test_a_partly_paid_invoice_writes_off_the_rest_and_its_vat_share(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    paid = await _call(
        two_organizations,
        "POST",
        f"/sales-invoices/{owed['invoice']}/payments",
        {"amount": "605.00", "paid_on": TODAY.isoformat(), "bank_account_id": str(owed["bank"])},
    )
    assert paid.status_code == 200, paid.text

    response = await _call(two_organizations, "POST", _path(owed), _body(owed))

    assert response.status_code == 200, response.text
    assert response.json()["write_off"]["amount"] == "605.00"
    assert response.json()["write_off"]["vat_amount"] == "105.00"
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("0.00")


async def test_a_reason_and_an_expense_account_are_required(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)

    blank = await _call(two_organizations, "POST", _path(owed), _body(owed, reason="  "))
    assert blank.status_code == 422
    assert blank.json()["detail"]["reason"] == "write_off_reason_missing"

    not_expense = await _call(
        two_organizations, "POST", _path(owed), _body(owed, expense_account_id=str(owed["bank"]))
    )
    assert not_expense.status_code == 422
    assert not_expense.json()["detail"]["reason"] == "write_off_account_invalid"

    future = await _call(
        two_organizations, "POST", _path(owed), _body(owed, written_off_on="2099-01-01")
    )
    assert future.status_code == 422
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")


async def test_an_unknown_invoice_is_a_404(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    response = await _call(
        two_organizations, "POST", f"/sales-invoices/{uuid.uuid4()}/write-offs", _body(owed)
    )
    assert response.status_code == 404


# --- the VAT reclaim ----------------------------------------
async def test_reclaiming_with_the_write_off_debits_the_vat_account_with_its_treatment(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)

    response = await _call(two_organizations, "POST", _path(owed), _body(owed, reclaim_vat=True))

    assert response.status_code == 200, response.text
    write_off = response.json()["write_off"]
    assert write_off["vat_reclaimed_on"] == TODAY.isoformat()
    assert write_off["vat_split"] == [{"vat_treatment": "btw_21", "vat_amount": "210.00"}]
    # The invoice's own posting credited 210.00 to the VAT account; the reclaim debits it.
    assert await _account_movement(two_organizations, owed["vat_out"]) == (
        Decimal("210.00"),
        Decimal("210.00"),
    )
    # Net loss: 1210.00 debited to the expense, 210.00 of it credited back.
    assert await _account_movement(two_organizations, owed["expense"]) == (
        Decimal("1210.00"),
        Decimal("210.00"),
    )
    tagged = await _exec(
        two_organizations,
        "SELECT vat_treatment FROM journal_line  WHERE account_id = :account AND debit = 210.00",
        account=str(owed["vat_out"]),
    )
    assert tagged.scalar_one() == "btw_21"  # the return can put it in the right box


async def test_the_vat_cannot_be_reclaimed_inside_the_waiting_period(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations, due="2026-08-01")

    refused = await _call(two_organizations, "POST", _path(owed), _body(owed, reclaim_vat=True))

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["reason"] == "write_off_vat_not_yet"
    assert detail["eligible_on_iso"] == "2027-08-01"
    # Nothing was written: the request is atomic.
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")
    listing = await _call(two_organizations, "GET", _path(owed))
    assert listing.json()["write_offs"] == []

    # The write-off ALONE is allowed - the loss is recognised now - and the reclaim waits.
    written = await _call(two_organizations, "POST", _path(owed), _body(owed))
    assert written.status_code == 200
    later = await _call(
        two_organizations, "POST", _path(owed, f"/{written.json()['write_off']['id']}/reclaim-vat")
    )
    assert later.status_code == 409
    assert later.json()["detail"]["reason"] == "write_off_vat_not_yet"


async def test_an_insolvent_customer_lets_the_vat_be_reclaimed_at_once(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations, due="2026-08-01")

    response = await _call(
        two_organizations,
        "POST",
        _path(owed),
        _body(owed, reclaim_vat=True, customer_insolvent=True),
    )

    assert response.status_code == 200, response.text
    assert response.json()["write_off"]["vat_reclaimed_on"] == TODAY.isoformat()


async def test_the_vat_can_be_reclaimed_later_once_and_only_once(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)  # long overdue: the wait has passed
    written = (await _call(two_organizations, "POST", _path(owed), _body(owed))).json()
    write_off_id = written["write_off"]["id"]

    reclaimed = await _call(two_organizations, "POST", _path(owed, f"/{write_off_id}/reclaim-vat"))
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["write_off"]["vat_reclaimed_on"] == TODAY.isoformat()

    again = await _call(two_organizations, "POST", _path(owed, f"/{write_off_id}/reclaim-vat"))
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "write_off_vat_already_reclaimed"
    assert await _account_movement(two_organizations, owed["vat_out"]) == (
        Decimal("210.00"),
        Decimal("210.00"),
    )


# --- voiding ----------------------------------------
async def test_voiding_reverses_the_write_off_and_the_reclaim_and_owes_the_invoice_again(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    written = (
        await _call(two_organizations, "POST", _path(owed), _body(owed, reclaim_vat=True))
    ).json()

    voided = await _call(
        two_organizations, "POST", _path(owed, f"/{written['write_off']['id']}/void")
    )

    assert voided.status_code == 200, voided.text
    assert voided.json()["write_off"]["voided_at"] is not None
    assert voided.json()["balance"]["outstanding_amount"] == "1210.00"
    assert voided.json()["balance"]["state"] == "open"
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")
    # The VAT account is back to what the invoice alone put there; the expense nets to nil.
    debit, credit = await _account_movement(two_organizations, owed["vat_out"])
    assert credit - debit == Decimal("210.00")
    debit, credit = await _account_movement(two_organizations, owed["expense"])
    assert debit == credit


async def test_after_a_void_the_customer_can_pay_and_the_invoice_can_be_written_off_again(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    payment = {
        "amount": "1210.00",
        "paid_on": TODAY.isoformat(),
        "bank_account_id": str(owed["bank"]),
    }
    written = (await _call(two_organizations, "POST", _path(owed), _body(owed))).json()

    # While it is written off there is nothing to pay: the guard refuses the payment.
    blocked = await _call(
        two_organizations, "POST", f"/sales-invoices/{owed['invoice']}/payments", payment
    )
    assert blocked.status_code == 409

    await _call(two_organizations, "POST", _path(owed, f"/{written['write_off']['id']}/void"))
    paid = await _call(
        two_organizations, "POST", f"/sales-invoices/{owed['invoice']}/payments", payment
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["balance"]["state"] == "paid"


async def test_a_write_off_can_be_voided_only_once(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    written = (await _call(two_organizations, "POST", _path(owed), _body(owed))).json()
    path = _path(owed, f"/{written['write_off']['id']}/void")
    await _call(two_organizations, "POST", path)

    again = await _call(two_organizations, "POST", path)

    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "write_off_already_voided"
    listing = (await _call(two_organizations, "GET", _path(owed))).json()
    assert len(listing["write_offs"]) == 1  # the voided one stays as history


async def test_an_unknown_write_off_is_a_404(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    response = await _call(two_organizations, "POST", _path(owed, f"/{uuid.uuid4()}/void"))
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "write_off_not_found"


# --- what the database itself refuses ----------------------------------------
async def _write_off_row(tenants: SeededTenants, owed: dict[str, uuid.UUID]) -> uuid.UUID:
    written = (await _call(tenants, "POST", _path(owed), _body(owed))).json()
    return uuid.UUID(written["write_off"]["id"])


async def test_the_database_refuses_a_partial_write_off(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    entry = await _scalar(
        two_organizations,
        "SELECT (ledger.post_entry("
        "  p_administration_id => :admin, p_journal_id => :journal, p_period_id => :period,"
        "  p_entry_date => DATE '2026-09-15', p_description => 'x', p_document_reference => 'x',"
        "  p_posted_by_user_id => :user, p_source_system => 'invoicing',"
        "  p_lines => cast(:lines as jsonb))).id",
        admin=str(two_organizations.admin_a),
        journal=str(owed["memorial"]),
        period=str(owed["period"]),
        user=str(two_organizations.owner_a),
        lines=json.dumps(
            [
                {
                    "account_id": str(owed["expense"]),
                    "debit": "100.00",
                    "credit": "0.00",
                    "subledger_party_id": None,
                    "cost_centre_id": None,
                    "description": "x",
                    "vat_treatment": None,
                },
                {
                    "account_id": str(owed["receivable"]),
                    "debit": "0.00",
                    "credit": "100.00",
                    "subledger_party_id": str(owed["party"]),
                    "cost_centre_id": None,
                    "description": "x",
                    "vat_treatment": None,
                },
            ]
        ),
    )

    with pytest.raises(Exception, match="(?i)whole|still outstanding|never part"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_invoice_write_off (organization_id, administration_id, "
            "  invoice_id, amount, vat_amount, written_off_on, reason, expense_account_id, "
            "  journal_entry_id, recorded_by_user_id) "
            "VALUES (:org, :admin, :invoice, 100.00, 17.36, DATE '2026-09-15', 'x', "
            "  :expense, :entry, :user)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            invoice=str(owed["invoice"]),
            expense=str(owed["expense"]),
            entry=str(entry),
            user=str(two_organizations.owner_a),
        )


async def test_a_write_off_record_cannot_be_edited_or_deleted(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    write_off = await _write_off_row(two_organizations, owed)

    with pytest.raises(Exception, match="(?i)record of a decision|only recording"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_write_off SET amount = 1.00 WHERE id = :id",
            id=str(write_off),
        )
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "DELETE FROM sales_invoice_write_off WHERE id = :id",
            id=str(write_off),
        )


async def test_a_voided_write_off_is_frozen(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    write_off = await _write_off_row(two_organizations, owed)
    await _call(two_organizations, "POST", _path(owed, f"/{write_off}/void"))

    with pytest.raises(Exception, match="(?i)voided|history"):
        await _exec(
            two_organizations,
            "UPDATE sales_invoice_write_off SET reason = 'other' WHERE id = :id",
            id=str(write_off),
        )


async def test_another_tenant_cannot_see_a_write_off(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    await _write_off_row(two_organizations, owed)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}{_path(owed)}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in {403, 404}

"""SEPA direct debit end to end - SI-09 (ADR-077, migration 0059).

Real HTTP, real routes, services, repository, ledger and Postgres under RLS. What this proves
that fakes cannot: that a file is generated, stored and served byte for byte with its hash, that
an invoice cannot be in two live collections, that confirming a collection records a REAL payment
in the real ledger (and only once), that a failure leaves the invoice owed, and what the database
itself refuses (editing a mandate, deleting one, collecting for another tenant).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0059.
"""

from __future__ import annotations

import hashlib
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

import api.invoicing.routes as routes_module
from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec
from tests.integration.test_write_offs import _call, _debtor_balance, _owed
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"}
CREDITOR_ID = "DE98ZZZ09999999999"  # the EPC's own valid example
ADMIN_IBAN = "NL91ABNA0417164300"
DEBTOR_IBAN = "NL02ABNA0123456789"

# The batch that gets CONFIRMED is generated "in the past": the API refuses a collection date
# that is not in the future, and a confirmation is refused before the collection date, so the
# only way to test a confirmation is to have generated the file on an earlier day.
PAST_TODAY = date(2026, 9, 10)  # a Thursday
PAST_COLLECTION = "2026-09-11"  # the Friday after, inside the seed's open September period


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


class _FrozenDate(date):
    @classmethod
    def today(cls) -> date:
        return PAST_TODAY


def _future_business_day(days: int = 3) -> date:
    day = date.today() + timedelta(days=days)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


async def _configured(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    """An owed invoice, and an administration with the IBAN and creditor id a file needs."""
    owed = await _owed(tenants)
    patched = await _patch_administration(
        tenants, {"iban": ADMIN_IBAN, "sepa_creditor_id": CREDITOR_ID}
    )
    assert patched.status_code == 200, patched.text
    return owed


async def _patch_administration(tenants: SeededTenants, body: dict[str, object]):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(tenants.org_a, user_id=tenants.owner_a)
        return await client.patch(
            f"/v1/administrations/{tenants.admin_a}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
            json=body,
        )


def _mandate_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "signed_on": "2026-01-10",
        "debtor_name": "De Vries Holding B.V.",
        "debtor_iban": "NL02 ABNA 0123 4567 89",
        "mandate_reference": "MNDT-0001",
    }
    body.update(overrides)
    return body


async def _mandate(tenants: SeededTenants, owed: dict[str, uuid.UUID], **overrides: object) -> str:
    response = await _call(
        tenants,
        "POST",
        f"/customers/{owed['customer']}/sepa-mandates",
        _mandate_body(**overrides),
    )
    assert response.status_code == 200, response.text
    return str(response.json()["mandate"]["id"])


async def _batch(tenants: SeededTenants, **body: object):  # type: ignore[no-untyped-def]
    payload: dict[str, object] = {"collection_date": _future_business_day().isoformat()}
    payload.update(body)
    return await _call(tenants, "POST", "/sepa-collections", payload)


# --- the creditor identifier ----------------------------------------
async def test_the_creditor_id_is_validated_normalised_and_can_be_cleared(
    two_organizations: SeededTenants,
) -> None:
    bad = await _patch_administration(two_organizations, {"sepa_creditor_id": "DE99ZZZ09999999999"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "sepa_creditor_id_invalid"

    good = await _patch_administration(
        two_organizations, {"sepa_creditor_id": "de98 zzz 0999 9999 999"}
    )
    assert good.status_code == 200
    assert good.json()["sepa_creditor_id"] == CREDITOR_ID

    cleared = await _patch_administration(two_organizations, {"sepa_creditor_id": ""})
    assert cleared.json()["sepa_creditor_id"] is None


# --- mandates ----------------------------------------
async def test_a_mandate_is_registered_listed_and_revoked_once(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)

    created = await _call(
        two_organizations,
        "POST",
        f"/customers/{owed['customer']}/sepa-mandates",
        _mandate_body(),
    )

    assert created.status_code == 200, created.text
    mandate = created.json()["mandate"]
    assert mandate["debtor_iban"] == DEBTOR_IBAN  # compact
    assert mandate["status"] == "active" and mandate["is_lapsed"] is False
    assert mandate["next_sequence"] == "FRST"  # the first / recurring flag, derived

    listing = await _call(two_organizations, "GET", f"/customers/{owed['customer']}/sepa-mandates")
    assert [m["mandate_reference"] for m in listing.json()["mandates"]] == ["MNDT-0001"]

    revoked = await _call(two_organizations, "POST", f"/sepa-mandates/{mandate['id']}/revoke")
    assert revoked.json()["mandate"]["status"] == "revoked"
    again = await _call(two_organizations, "POST", f"/sepa-mandates/{mandate['id']}/revoke")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "sepa_mandate_already_revoked"


async def test_unusable_mandates_are_refused(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)
    await _mandate(two_organizations, owed)
    path = f"/customers/{owed['customer']}/sepa-mandates"

    duplicate = await _call(two_organizations, "POST", path, _mandate_body())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason"] == "sepa_mandate_reference_taken"

    for overrides, reason in (
        ({"debtor_iban": "NL02ABNA0123456780", "mandate_reference": "X1"}, "sepa_iban_invalid"),
        ({"signed_on": "2099-01-01", "mandate_reference": "X2"}, "sepa_signed_in_future"),
        ({"mandate_reference": "no spaces"}, "sepa_reference_invalid"),
        ({"debtor_name": " ", "mandate_reference": "X3"}, "sepa_holder_name_required"),
        ({"scheme": "nope", "mandate_reference": "X4"}, "sepa_scheme_or_kind_invalid"),
    ):
        response = await _call(two_organizations, "POST", path, _mandate_body(**overrides))
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == reason

    stranger = await _call(
        two_organizations,
        "POST",
        f"/customers/{uuid.uuid4()}/sepa-mandates",
        _mandate_body(mandate_reference="X5"),
    )
    assert stranger.status_code == 404


async def test_a_mandate_record_cannot_be_edited_or_deleted(
    two_organizations: SeededTenants,
) -> None:
    owed = await _owed(two_organizations)
    mandate = await _mandate(two_organizations, owed)

    with pytest.raises(Exception, match="(?i)evidence of consent|only be revoked"):
        await _exec(
            two_organizations,
            "UPDATE sepa_mandate SET debtor_iban = 'NL91ABNA0417164300' WHERE id = :id",
            id=mandate,
        )
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(two_organizations, "DELETE FROM sepa_mandate WHERE id = :id", id=mandate)


# --- a file ----------------------------------------
async def test_a_batch_generates_a_file_that_is_stored_and_served_byte_for_byte(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)
    await _mandate(two_organizations, owed)

    response = await _batch(two_organizations)

    assert response.status_code == 200, response.text
    body = response.json()
    (item,) = body["items"]
    assert item["amount"] == "1210.00" and item["sequence_type"] == "FRST"
    assert item["status"] == "submitted" and body["skipped"] == []
    batch = body["batch"]
    assert batch["item_count"] == 1 and batch["total_amount"] == "1210.00"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        served = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/sepa-collections/{batch['id']}/file",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("application/xml")
    assert batch["message_id"] in served.headers["content-disposition"]
    # Exactly what was generated: its hash is the one the batch recorded.
    assert hashlib.sha256(served.content).hexdigest() == batch["file_sha256"]

    root = ET.fromstring(served.content)
    block = "p:CstmrDrctDbtInitn/p:PmtInf"
    assert root.findtext(f"{block}/p:CdtrSchmeId/p:Id/p:PrvtId/p:Othr/p:Id", namespaces=NS) == (
        CREDITOR_ID
    )
    assert root.findtext(f"{block}/p:CdtrAcct/p:Id/p:IBAN", namespaces=NS) == ADMIN_IBAN
    assert root.findtext(f"{block}/p:DrctDbtTxInf/p:DbtrAcct/p:Id/p:IBAN", namespaces=NS) == (
        DEBTOR_IBAN
    )
    assert root.findtext(f"{block}/p:DrctDbtTxInf/p:InstdAmt", namespaces=NS) == "1210.00"
    assert (
        root.findtext(f"{block}/p:DrctDbtTxInf/p:DrctDbtTx/p:MndtRltdInf/p:MndtId", namespaces=NS)
        == "MNDT-0001"
    )
    assert root.findtext("p:CstmrDrctDbtInitn/p:GrpHdr/p:CtrlSum", namespaces=NS) == "1210.00"

    fetched = await _call(two_organizations, "GET", f"/sepa-collections/{batch['id']}")
    assert [i["id"] for i in fetched.json()["items"]] == [item["id"]]
    listing = await _call(two_organizations, "GET", "/sepa-collections")
    assert [b["id"] for b in listing.json()["batches"]] == [batch["id"]]


async def test_a_batch_needs_a_creditor_and_a_mandate(two_organizations: SeededTenants) -> None:
    owed = await _owed(two_organizations)  # no IBAN or creditor id on the administration yet
    await _mandate(two_organizations, owed)

    refused = await _batch(two_organizations)
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "sepa_creditor_not_configured"
    assert refused.json()["detail"]["missing"] == "iban"

    await _patch_administration(two_organizations, {"iban": ADMIN_IBAN})
    refused = await _batch(two_organizations)
    assert refused.json()["detail"]["missing"] == "sepa_creditor_id"


async def test_no_mandate_means_nothing_to_collect_and_names_the_invoice(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)

    response = await _batch(two_organizations, invoice_ids=[str(owed["invoice"])])

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "sepa_nothing_to_collect"
    assert detail["skipped"] == [{"invoice_id": str(owed["invoice"]), "reason": "no_mandate"}]


async def test_an_invoice_is_in_one_live_collection_only_and_a_cancelled_file_frees_it(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)
    await _mandate(two_organizations, owed)
    first = (await _batch(two_organizations)).json()

    second = await _batch(two_organizations, invoice_ids=[str(owed["invoice"])])
    assert second.status_code == 409
    assert second.json()["detail"]["skipped"][0]["reason"] == "already_in_collection"

    cancelled = await _call(
        two_organizations, "POST", f"/sepa-collections/{first['batch']['id']}/cancel"
    )
    assert cancelled.status_code == 200 and cancelled.json()["batch"]["cancelled_at"]
    fetched = (
        await _call(two_organizations, "GET", f"/sepa-collections/{first['batch']['id']}")
    ).json()
    assert [i["status"] for i in fetched["items"]] == ["cancelled"]

    again = await _batch(two_organizations)
    assert again.status_code == 200, again.text
    assert again.json()["items"][0]["sequence_type"] == "FRST"  # nothing was ever submitted

    twice = await _call(
        two_organizations, "POST", f"/sepa-collections/{first['batch']['id']}/cancel"
    )
    assert twice.status_code == 409
    assert twice.json()["detail"]["reason"] == "sepa_batch_not_cancellable"


async def test_the_collection_date_must_be_a_future_business_day(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)
    await _mandate(two_organizations, owed)
    for bad in (date.today(), date.today() - timedelta(days=1), date.today() + timedelta(days=90)):
        response = await _batch(two_organizations, collection_date=bad.isoformat())
        assert response.status_code == 422, bad
        assert response.json()["detail"]["reason"] == "sepa_collection_date_invalid"


# --- outcomes ----------------------------------------
async def _past_batch(
    tenants: SeededTenants, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, uuid.UUID], dict[str, object]]:
    """A configured world and a batch generated on 2026-09-10 for 2026-09-11."""
    owed = await _configured(tenants)
    await _mandate(tenants, owed)
    monkeypatch.setattr(routes_module, "date", _FrozenDate)
    try:
        response = await _batch(tenants, collection_date=PAST_COLLECTION)
    finally:
        monkeypatch.undo()
    assert response.status_code == 200, response.text
    return owed, response.json()


async def test_confirming_a_collection_records_a_real_payment_once(
    two_organizations: SeededTenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    owed, batch = await _past_batch(two_organizations, monkeypatch)
    item = batch["items"][0]  # type: ignore[index]
    path = f"/sepa-collections/{batch['batch']['id']}/items/{item['id']}"  # type: ignore[index]
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")

    collected = await _call(
        two_organizations, "POST", f"{path}/collected", {"bank_account_id": str(owed["bank"])}
    )

    assert collected.status_code == 200, collected.text
    settled = collected.json()["item"]
    assert settled["status"] == "collected" and settled["payment_id"]
    # A real payment: the debtor's sub-ledger fell, the invoice is paid, and it says how.
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("0.00")
    payments = (
        await _call(two_organizations, "GET", f"/sales-invoices/{owed['invoice']}/payments")
    ).json()
    assert payments["balance"]["state"] == "paid"
    (payment,) = payments["payments"]
    assert payment["paid_on"] == PAST_COLLECTION
    assert payment["method"] == "other" and payment["id"] == settled["payment_id"]

    # Once: a second confirmation neither posts again nor changes anything.
    again = await _call(
        two_organizations, "POST", f"{path}/collected", {"bank_account_id": str(owed["bank"])}
    )
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "sepa_item_already_decided"
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("0.00")

    # The mandate remembers, so the next collection on it is recurring.
    mandates = (
        await _call(two_organizations, "GET", f"/customers/{owed['customer']}/sepa-mandates")
    ).json()["mandates"]
    assert mandates[0]["last_collected_on"] == PAST_COLLECTION
    assert mandates[0]["next_sequence"] == "RCUR"


async def test_a_collection_cannot_be_confirmed_before_its_date(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)
    await _mandate(two_organizations, owed)
    batch = (await _batch(two_organizations)).json()
    item = batch["items"][0]

    response = await _call(
        two_organizations,
        "POST",
        f"/sepa-collections/{batch['batch']['id']}/items/{item['id']}/collected",
        {"bank_account_id": str(owed["bank"])},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "sepa_collection_not_due"
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")


async def test_an_invoice_paid_another_way_meanwhile_leaves_the_item_undecided(
    two_organizations: SeededTenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    owed, batch = await _past_batch(two_organizations, monkeypatch)
    item = batch["items"][0]  # type: ignore[index]
    paid = await _call(
        two_organizations,
        "POST",
        f"/sales-invoices/{owed['invoice']}/payments",
        {
            "amount": "1210.00",
            "paid_on": PAST_COLLECTION,
            "bank_account_id": str(owed["bank"]),
        },
    )
    assert paid.status_code == 200, paid.text

    response = await _call(
        two_organizations,
        "POST",
        f"/sepa-collections/{batch['batch']['id']}/items/{item['id']}/collected",  # type: ignore[index]
        {"bank_account_id": str(owed["bank"])},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "payment_exceeds_outstanding"
    fetched = (
        await _call(two_organizations, "GET", f"/sepa-collections/{batch['batch']['id']}")  # type: ignore[index]
    ).json()
    assert [i["status"] for i in fetched["items"]] == ["submitted"]  # nothing half-recorded
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("0.00")  # one payment


async def test_a_failed_collection_leaves_the_invoice_owed_and_free_to_collect_again(
    two_organizations: SeededTenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    owed, batch = await _past_batch(two_organizations, monkeypatch)
    item = batch["items"][0]  # type: ignore[index]
    path = f"/sepa-collections/{batch['batch']['id']}/items/{item['id']}/failed"  # type: ignore[index]

    blank = await _call(two_organizations, "POST", path, {"reason": "  "})
    assert blank.status_code == 422
    assert blank.json()["detail"]["reason"] == "sepa_failure_reason_required"

    failed = await _call(two_organizations, "POST", path, {"reason": "AM04 insufficient funds"})
    assert failed.status_code == 200, failed.text
    assert failed.json()["item"]["status"] == "failed"
    assert failed.json()["item"]["failure_reason"] == "AM04 insufficient funds"
    assert await _debtor_balance(two_organizations, owed["party"]) == Decimal("1210.00")

    again = await _batch(two_organizations)
    assert again.status_code == 200, again.text
    assert again.json()["items"][0]["sequence_type"] == "FRST"  # a failed first is still first

    # A batch that has an outcome recorded can no longer be withdrawn.
    cancel = await _call(
        two_organizations,
        "POST",
        f"/sepa-collections/{batch['batch']['id']}/cancel",  # type: ignore[index]
    )
    assert cancel.status_code == 409


async def test_unknown_batches_and_items_are_404(two_organizations: SeededTenants) -> None:
    owed = await _configured(two_organizations)
    assert (
        await _call(two_organizations, "GET", f"/sepa-collections/{uuid.uuid4()}")
    ).status_code == 404
    response = await _call(
        two_organizations,
        "POST",
        f"/sepa-collections/{uuid.uuid4()}/items/{uuid.uuid4()}/failed",
        {"reason": "x"},
    )
    assert response.status_code == 404
    assert owed  # the world exists; the ids do not


# --- what the database itself refuses ----------------------------------------
async def test_the_database_refuses_editing_or_deleting_a_batch_and_its_items(
    two_organizations: SeededTenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, batch = await _past_batch(two_organizations, monkeypatch)
    batch_id, item_id = batch["batch"]["id"], batch["items"][0]["id"]  # type: ignore[index]

    with pytest.raises(Exception, match="(?i)file the bank was given"):
        await _exec(
            two_organizations,
            "UPDATE sepa_collection_batch SET file_xml = '<x/>' WHERE id = :id",
            id=batch_id,
        )
    with pytest.raises(Exception, match="(?i)what the bank was asked"):
        await _exec(
            two_organizations,
            "UPDATE sepa_collection SET amount = 1.00 WHERE id = :id",
            id=item_id,
        )
    for table, key in (("sepa_collection_batch", batch_id), ("sepa_collection", item_id)):
        with pytest.raises(Exception, match="(?i)permission denied"):
            await _exec(two_organizations, f"DELETE FROM {table} WHERE id = :id", id=key)


async def test_another_tenant_cannot_see_a_batch_or_a_mandate(
    two_organizations: SeededTenants,
) -> None:
    owed = await _configured(two_organizations)
    await _mandate(two_organizations, owed)
    batch = (await _batch(two_organizations)).json()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        for path in (
            f"/sepa-collections/{batch['batch']['id']}",
            f"/sepa-collections/{batch['batch']['id']}/file",
            f"/customers/{owed['customer']}/sepa-mandates",
        ):
            response = await client.get(
                f"/v1/administrations/{two_organizations.admin_a}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code in {403, 404}, path

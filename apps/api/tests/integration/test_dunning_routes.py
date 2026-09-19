"""The dunning routes end to end - SI-04 and SI-11 (ADR-071, ADR-073).

Real HTTP, real routes, real services, real repository, real Postgres under RLS, real
audit log. Only the e-mail hand-over is faked (the delivery service is overridden),
because nothing here should send mail. So what this proves is what unit tests with
fakes cannot: that the SQL, the transaction and the routes agree.

Two of these guard defects found while building SI-11:

* a provider refusal used to raise, rolling the request's transaction back and taking
  the failed dispatch's audit entry with it;
* nothing stopped step 2 following step 1 at once, so a retried bulk chase would
  have escalated every customer.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0055.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from api.i18n.language import Language
from api.invoicing.delivery import DeliveryChannel, DeliveryStatus, UnreachableCustomer
from api.invoicing.delivery_service import DeliveryRecord
from api.invoicing.routes import get_delivery_service
from api.main import app
from tests.integration.test_dunning import _overdue_invoice
from tests.integration.test_sales_invoice_posting import _exec, _issued_and_posted, _scalar
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

# Invoices are dated 2026-09-09 (the shared seed); a due date of 2026-09-10 makes them
# overdue for every real "today" from 2026-09-17 on, and step 1 is the next step for
# any invoice nobody has chased.
DUE = "2026-09-10"


@dataclass
class FakeDeliveryService:
    """Stands in for `InvoiceDeliveryService`: answers, never sends."""

    status_for: dict[uuid.UUID, DeliveryStatus] = field(default_factory=dict)
    raises_for: dict[uuid.UUID, Exception] = field(default_factory=dict)
    sent_to: list[uuid.UUID] = field(default_factory=list)

    async def dispatch(self, **kwargs: object) -> DeliveryRecord:
        invoice_id = kwargs["invoice_id"]
        assert isinstance(invoice_id, uuid.UUID)
        if invoice_id in self.raises_for:
            raise self.raises_for[invoice_id]
        status = self.status_for.get(invoice_id, DeliveryStatus.SENT)
        if status is DeliveryStatus.SENT:
            self.sent_to.append(invoice_id)
        return DeliveryRecord(
            id=await _delivery_row(invoice_id, status),
            invoice_id=invoice_id,
            channel=DeliveryChannel.EMAIL,
            status=status,
            recipient="klant@example.com",
            language=Language.NL,
            attempts=1,
        )


#: The tenants under test, so the fake can write the `invoice_delivery` row the real
#: service would (`dunning_reminder.delivery_id` is a real foreign key).
_TENANTS: SeededTenants | None = None


async def _delivery_row(invoice_id: uuid.UUID, status: DeliveryStatus) -> uuid.UUID:
    assert _TENANTS is not None
    sent = status is DeliveryStatus.SENT
    return await _scalar(
        _TENANTS,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at, next_attempt_at) "
        "VALUES (:org, :admin, :invoice, 'email', :status, 'klant@example.com', 'nl', "
        "  CASE WHEN :sent THEN now() END, "
        "  CASE WHEN :status = 'queued' THEN now() + interval '5 minutes' END) "
        "RETURNING id",
        org=str(_TENANTS.org_a),
        admin=str(_TENANTS.admin_a),
        invoice=str(invoice_id),
        status=status.value,
        sent=sent,
    )


@pytest.fixture
async def delivery(two_organizations: SeededTenants) -> AsyncIterator[FakeDeliveryService]:
    global _TENANTS  # noqa: PLW0603
    _TENANTS = two_organizations
    fake = FakeDeliveryService()
    app.dependency_overrides[get_delivery_service] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_delivery_service, None)
        _TENANTS = None


def _headers(tenants: SeededTenants) -> dict[str, str]:
    token = make_token(tenants.org_a, user_id=tenants.owner_a)
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _url(tenants: SeededTenants, path: str) -> str:
    return f"/v1/administrations/{tenants.admin_a}{path}"


async def _post(tenants: SeededTenants, path: str, json: object = None):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(_url(tenants, path), headers=_headers(tenants), json=json)


async def _get(tenants: SeededTenants, path: str):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(_url(tenants, path), headers=_headers(tenants))


async def _request(tenants: SeededTenants, method: str, path: str, json: object = None):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method, _url(tenants, path), headers=_headers(tenants), json=json
        )


async def _second_invoice(tenants: SeededTenants, owed: dict[str, uuid.UUID]) -> uuid.UUID:
    """Another issued invoice in the same administration, owing 1210.00, due DUE."""
    invoice, _ = await _issued_and_posted(tenants, owed, owed["party"], due_date=DUE)
    await _exec(
        tenants,
        "INSERT INTO sales_invoice_vat_total (invoice_id, organization_id, "
        "  administration_id, vat_treatment, rate, taxable_amount, vat_amount) "
        "VALUES (:invoice, :org, :admin, 'btw_21', 21, 1000.00, 210.00)",
        invoice=str(invoice),
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )
    return invoice


async def _ladder(tenants: SeededTenants, *steps: tuple[int, int, str, bool]) -> None:
    """(position, days_after_due, kind, charge_collection_cost). No interest, so no
    interest rate has to be loaded."""
    await _exec(
        tenants,
        "INSERT INTO dunning_ladder_configured (administration_id, organization_id) "
        "VALUES (:admin, :org)",
        admin=str(tenants.admin_a),
        org=str(tenants.org_a),
    )
    for position, days, kind, cost in steps:
        await _exec(
            tenants,
            "INSERT INTO dunning_step (organization_id, administration_id, position, "
            "  days_after_due, kind, charge_collection_cost) "
            "VALUES (:org, :admin, :position, :days, :kind, :cost)",
            org=str(tenants.org_a),
            admin=str(tenants.admin_a),
            position=position,
            days=days,
            kind=kind,
            cost=cost,
        )


async def _earlier_reminder(
    tenants: SeededTenants, invoice: uuid.UUID, *, position: int, days_ago: int, kind: str
) -> None:
    """A reminder already sent `days_ago` days back, as the database would hold it."""
    delivery = await _scalar(
        tenants,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at) "
        "VALUES (:org, :admin, :invoice, 'email', 'sent', 'k@example.com', 'nl', now()) "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(invoice),
    )
    await _exec(
        tenants,
        "INSERT INTO dunning_reminder (organization_id, administration_id, invoice_id, "
        "  step_position, kind, delivery_id, outstanding_amount, days_overdue, "
        "  sent_by_user_id, sent_at) "
        "VALUES (:org, :admin, :invoice, :position, :kind, :delivery, 1210.00, 5, :user, :at)",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(invoice),
        position=position,
        kind=kind,
        delivery=str(delivery),
        user=str(tenants.owner_a),
        at=datetime.now().astimezone() - timedelta(days=days_ago),
    )


async def _reminders(tenants: SeededTenants) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT invoice_id, step_position, kind, collection_cost_amount, pay_by "
        "  FROM dunning_reminder WHERE administration_id = :admin ORDER BY step_position",
        admin=str(tenants.admin_a),
    )
    return list(result)


async def _audit(tenants: SeededTenants, action: str) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT outcome, detail FROM audit_log "
        " WHERE administration_id = :admin AND action = :action",
        admin=str(tenants.admin_a),
        action=action,
    )
    return list(result)


# --- chase everything -----------------------------------------------------------
async def test_chase_everything_sends_the_first_reminder_to_each_overdue_invoice(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    first = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]
    owed = await _overdue_invoice_world(two_organizations, first)
    second = await _second_invoice(two_organizations, owed)

    response = await _post(two_organizations, "/dunning/chase")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {"sent": 2, "skipped": 0, "failed": 0, "deferred": 0}
    assert {r["invoice_id"] for r in body["results"]} == {str(first), str(second)}
    assert {r["step"]["kind"] for r in body["results"]} == {"friendly"}
    assert set(delivery.sent_to) == {first, second}
    assert len(await _reminders(two_organizations)) == 2
    (summary,) = await _audit(two_organizations, "chase_overdue")
    assert summary.detail["sent"] == 2  # type: ignore[attr-defined]


async def _overdue_invoice_world(
    tenants: SeededTenants, first_invoice: uuid.UUID
) -> dict[str, uuid.UUID]:
    """Rebuild the seed world dict for the invoice `_overdue_invoice` made, so a
    second invoice can be issued in it."""
    world = await _exec(
        tenants,
        "SELECT fiscal_year_id FROM sales_invoice WHERE id = :id",
        id=str(first_invoice),
    )
    year = world.scalar_one()
    ids = {}
    for name, sql in {
        "period": "SELECT id FROM period WHERE administration_id = :admin",
        "journal": "SELECT id FROM ledger_journal WHERE administration_id = :admin "
        "AND journal_type = 'sales'",
        "receivable": "SELECT id FROM ledger_account WHERE administration_id = :admin "
        "AND control_kind = 'accounts_receivable'",
        "revenue": "SELECT id FROM ledger_account WHERE administration_id = :admin "
        "AND account_type = 'revenue'",
        "vat_out": "SELECT id FROM ledger_account WHERE administration_id = :admin "
        "AND account_type = 'liability' AND control_kind IS NULL",
        "party": "SELECT id FROM subledger_party WHERE administration_id = :admin",
    }.items():
        ids[name] = (await _exec(tenants, sql, admin=str(tenants.admin_a))).scalars().first()
    return {"year": year, **ids}  # type: ignore[dict-item]


async def test_repeating_a_chase_sends_nothing_more(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    """The defect the spacing rule closes, through the real database: after step 1 the
    ladder's step 2 is already due (day 5 <= 9), but the last reminder was today."""
    await _ladder(two_organizations, (1, 2, "friendly", False), (2, 5, "reminder", False))
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]

    first = await _post(two_organizations, "/dunning/chase")
    second = await _post(two_organizations, "/dunning/chase")

    assert first.json()["summary"]["sent"] == 1
    assert second.json()["summary"]["sent"] == 0
    (result,) = second.json()["results"]
    assert result["blocker"] == "too_soon"
    assert result["step"]["position"] == 2  # named, so a screen can say what is next
    assert delivery.sent_to == [invoice]  # once, ever
    assert len(await _reminders(two_organizations)) == 1


async def test_a_formal_notice_needs_a_person_to_choose_it(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    await _ladder(two_organizations, (1, 2, "friendly", False), (2, 5, "formal_notice", True))
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]
    await _earlier_reminder(two_organizations, invoice, position=1, days_ago=10, kind="friendly")

    everything = await _post(two_organizations, "/dunning/chase")
    assert everything.json()["results"][0]["skip"] == "needs_confirmation"
    assert delivery.sent_to == []

    chosen = await _post(
        two_organizations,
        "/dunning/chase",
        {"items": [{"invoice_id": str(invoice), "step_position": 2}]},
    )

    assert chosen.json()["summary"]["sent"] == 1
    notice = (await _reminders(two_organizations))[-1]
    assert notice.kind == "formal_notice"  # type: ignore[attr-defined]
    # 15% of 1210.00: costs are on the principal.
    assert notice.collection_cost_amount == Decimal("181.50")  # type: ignore[attr-defined]
    assert notice.pay_by == date.today() + timedelta(days=15)  # type: ignore[attr-defined]


async def test_a_step_that_changed_since_the_preview_is_skipped(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]

    response = await _post(
        two_organizations,
        "/dunning/chase",
        {"items": [{"invoice_id": str(invoice), "step_position": 2}]},  # next is step 1
    )

    (result,) = response.json()["results"]
    assert result["skip"] == "step_changed"
    assert delivery.sent_to == []


async def test_one_customer_with_no_address_does_not_stop_the_others(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    first = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]
    owed = await _overdue_invoice_world(two_organizations, first)
    second = await _second_invoice(two_organizations, owed)
    delivery.raises_for[first] = UnreachableCustomer(DeliveryChannel.EMAIL, "no address")

    response = await _post(two_organizations, "/dunning/chase")

    body = response.json()
    assert body["summary"] == {"sent": 1, "skipped": 0, "failed": 1, "deferred": 0}
    by_id = {r["invoice_id"]: r for r in body["results"]}
    assert by_id[str(first)]["failure"] == "unreachable"
    assert by_id[str(second)]["status"] == "sent"
    # The savepoint rolled back the failed one only: the other reminder is committed.
    (recorded,) = await _reminders(two_organizations)
    assert recorded.invoice_id == second  # type: ignore[attr-defined]
    failures = [
        a
        for a in await _audit(two_organizations, "send_dunning_reminder")
        if a.outcome == "failure"
    ]  # type: ignore[attr-defined]
    assert len(failures) == 1


# --- one reminder, and the transaction ----------------------------------------
async def test_sending_one_reminder_records_it(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]

    response = await _post(two_organizations, f"/sales-invoices/{invoice}/dunning/send")

    assert response.status_code == 200, response.text
    assert response.json()["step"]["kind"] == "friendly"
    (reminder,) = await _reminders(two_organizations)
    assert reminder.step_position == 1  # type: ignore[attr-defined]


async def test_a_provider_refusal_answers_502_and_KEEPS_the_record_of_the_attempt(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    """The defect: this used to raise, the request's transaction rolled back, and the
    failed dispatch and its FAILURE audit entry vanished. Nothing was e-mailed, so the
    502 is honest - but somebody investigating needs to see that it was tried."""
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]
    delivery.status_for[invoice] = DeliveryStatus.QUEUED

    response = await _post(two_organizations, f"/sales-invoices/{invoice}/dunning/send")

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "dunning_reminder_not_delivered"
    assert await _reminders(two_organizations) == []  # not a reminder sent: still due
    failures = [
        a
        for a in await _audit(two_organizations, "send_dunning_reminder")
        if a.outcome == "failure"  # type: ignore[attr-defined]
    ]
    assert len(failures) == 1  # the audit entry SURVIVED the 502
    deliveries = await _exec(
        two_organizations,
        "SELECT status FROM invoice_delivery WHERE invoice_id = :i",
        i=str(invoice),
    )
    assert [r.status for r in deliveries] == ["queued"]  # and so did the dispatch row


# --- pause, end to end ---------------------------------------------------------
async def test_pausing_a_customer_stops_the_chase_and_resuming_restarts_it(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    customer = await _scalar(
        two_organizations,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
    )
    invoice = (await _overdue_invoice(two_organizations, due=DUE, customer_id=customer))["invoice"]

    paused = await _request(
        two_organizations, "PUT", f"/customers/{customer}/dunning-pause", {"reason": "dispute"}
    )
    assert paused.status_code == 200 and paused.json()["is_paused"] is True

    overview = (await _get(two_organizations, "/dunning")).json()
    assert overview["overdue"][0]["is_paused"] is True
    skipped = (await _post(two_organizations, "/dunning/chase")).json()
    assert skipped["results"][0]["skip"] == "blocked"
    assert skipped["results"][0]["blocker"] == "paused"
    assert delivery.sent_to == []

    resumed = await _request(two_organizations, "DELETE", f"/customers/{customer}/dunning-pause")
    assert resumed.json()["is_paused"] is False
    sent = (await _post(two_organizations, "/dunning/chase")).json()
    assert sent["summary"]["sent"] == 1
    assert delivery.sent_to == [invoice]


async def test_the_overview_reports_the_ladder_and_each_overdue_invoice(
    two_organizations: SeededTenants, delivery: FakeDeliveryService
) -> None:
    invoice = (await _overdue_invoice(two_organizations, due=DUE))["invoice"]

    response = await _get(two_organizations, "/dunning")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ladder"]["is_default"] is True
    (row,) = body["overdue"]
    assert row["invoice_id"] == str(invoice)
    assert row["can_send"] is True
    assert row["outstanding_amount"] == "1210.00"
    assert row["next_step"]["kind"] == "friendly"
    # Looking sent nothing.
    assert delivery.sent_to == []

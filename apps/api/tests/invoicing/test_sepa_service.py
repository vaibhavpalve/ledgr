"""api.invoicing.sepa_service - what it decides, refuses and records (ADR-077).

Fakes for the repository and the payment recorder: under test are the rules the service applies
(which mandate, first/recurring, what is skipped and why), who may do what, and that an outcome
and its payment are recorded together. `tests/integration/test_sepa_routes.py` runs the same
against Postgres, where the unique indexes and triggers live.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.invoicing.model import NotAuthorizedToInvoice
from api.invoicing.payments import InvoicePayment, PaymentMethod
from api.invoicing.sepa import MandateScheme, SequenceKind, SequenceType, SkipReason
from api.invoicing.sepa_service import (
    BatchNotCancellable,
    BatchNotFound,
    BatchRaced,
    Candidate,
    CollectionBatch,
    CollectionInvalid,
    CollectionItem,
    CollectionNotDue,
    CreditorDetails,
    CreditorNotConfigured,
    ItemAlreadyDecided,
    Mandate,
    MandateAlreadyRevoked,
    MandateNotFound,
    NothingToCollect,
    ReservationLost,
    SepaCustomerNotFound,
    SepaDirectDebitService,
)
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

TODAY = date(2026, 9, 21)  # a Monday
COLLECT_ON = date(2026, 9, 24)
NOW = datetime(2026, 9, 21, 10, 0, 0).astimezone()
CUSTOMER = uuid.uuid4()
BANK = uuid.uuid4()
IBAN_OK = "NL91ABNA0417164300"


class FailingPayments:
    """Stands in for `SalesPaymentService`: records what it was asked, or refuses."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def record(self, **kwargs: object) -> InvoicePayment:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return InvoicePayment(
            id=uuid.uuid4(),
            administration_id=kwargs["administration_id"],  # type: ignore[arg-type]
            invoice_id=kwargs["invoice_id"],  # type: ignore[arg-type]
            amount=kwargs["amount"],  # type: ignore[arg-type]
            paid_on=kwargs["paid_on"],  # type: ignore[arg-type]
            method=kwargs["method"],  # type: ignore[arg-type]
            reference=kwargs["reference"],  # type: ignore[arg-type]
            bank_account_id=kwargs["bank_account_id"],  # type: ignore[arg-type]
            journal_entry_id=uuid.uuid4(),
            recorded_by_user_id=kwargs["actor_user_id"],  # type: ignore[arg-type]
            recorded_at=NOW,
        )


@dataclass
class FakeRepository:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    creditor_details: CreditorDetails | None = field(
        default_factory=lambda: CreditorDetails("Bakker B.V.", IBAN_OK, "DE98ZZZ09999999999")
    )
    customer_known: bool = True
    mandates: list[Mandate] = field(default_factory=list)
    invoices: list[Candidate] = field(default_factory=list)
    batches: dict[uuid.UUID, CollectionBatch] = field(default_factory=dict)
    files: dict[uuid.UUID, str] = field(default_factory=dict)
    items: list[CollectionItem] = field(default_factory=list)
    reservation_lost: bool = False
    touched: list[tuple[uuid.UUID, date]] = field(default_factory=list)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        # Behaves like a SAVEPOINT for what these tests check: an error inside leaves the
        # state as it was before entering.
        before = (list(self.items), dict(self.batches), list(self.touched))
        try:
            yield
        except Exception:
            self.items, self.batches, self.touched = before[0], before[1], before[2]
            raise

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def creditor(self, *, administration_id: uuid.UUID) -> CreditorDetails | None:
        return self.creditor_details

    async def customer_exists(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.customer_known

    async def insert_mandate(self, mandate: Mandate) -> Mandate:
        self.mandates.append(mandate)
        return mandate

    async def get_mandate(self, *, administration_id: uuid.UUID, mandate_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return next((m for m in self.mandates if m.id == mandate_id), None)

    async def mandates_for_customer(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return [
            replace(
                m,
                used=any(
                    i.mandate_id == m.id and i.status in ("submitted", "collected")
                    for i in self.items
                ),
            )
            for m in self.mandates
            if m.customer_id == customer_id
        ]

    async def revoke_mandate(self, *, administration_id, mandate_id, user_id, reason):  # type: ignore[no-untyped-def]
        existing = next(m for m in self.mandates if m.id == mandate_id)
        if not existing.is_active:
            return None
        revoked = replace(existing, status="revoked", revoked_at=NOW, revoked_reason=reason)
        self.mandates[self.mandates.index(existing)] = revoked
        return revoked

    async def candidates(self, *, administration_id: uuid.UUID, invoice_ids):  # type: ignore[no-untyped-def]
        live = {i.invoice_id for i in self.items if i.status == "submitted"}
        rows = [
            replace(c, in_live_collection=c.in_live_collection or c.invoice_id in live)
            for c in self.invoices
        ]
        if invoice_ids is not None:
            return [c for c in rows if c.invoice_id in invoice_ids]
        return [c for c in rows if c.outstanding > 0]

    async def insert_batch(self, batch: CollectionBatch, file_xml: str) -> CollectionBatch:
        self.batches[batch.id] = batch
        self.files[batch.id] = file_xml
        return batch

    async def insert_item(self, item: CollectionItem) -> CollectionItem:
        if self.reservation_lost:
            raise ReservationLost(str(item.invoice_id))
        self.items.append(item)
        return item

    async def get_batch(self, *, administration_id: uuid.UUID, batch_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.batches.get(batch_id)

    async def batch_file(self, *, administration_id: uuid.UUID, batch_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.files.get(batch_id)

    async def list_batches(self, *, administration_id: uuid.UUID, limit: int):  # type: ignore[no-untyped-def]
        return list(self.batches.values())

    async def items_for_batch(self, *, administration_id: uuid.UUID, batch_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return [i for i in self.items if i.batch_id == batch_id]

    async def get_item(self, *, administration_id: uuid.UUID, item_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return next((i for i in self.items if i.id == item_id), None)

    async def decide_item(  # type: ignore[no-untyped-def]
        self, *, administration_id, item_id, status, user_id, failure_reason, payment_id
    ):
        existing = next(i for i in self.items if i.id == item_id)
        if existing.status != "submitted":
            return None
        decided = replace(
            existing,
            status=status,
            decided_at=NOW,
            failure_reason=failure_reason,
            payment_id=payment_id,
        )
        self.items[self.items.index(existing)] = decided
        return decided

    async def cancel_batch(self, *, administration_id, batch_id, user_id):  # type: ignore[no-untyped-def]
        self.items = [
            replace(i, status="cancelled") if i.batch_id == batch_id else i for i in self.items
        ]
        cancelled = replace(self.batches[batch_id], cancelled_at=NOW)
        self.batches[batch_id] = cancelled
        return cancelled

    async def touch_mandate(self, *, administration_id, mandate_id, collected_on):  # type: ignore[no-untyped-def]
        self.touched.append((mandate_id, collected_on))


@dataclass
class Setup:
    service: SepaDirectDebitService
    repository: FakeRepository
    payments: FailingPayments
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def mandate(self, **overrides: object) -> Mandate:
        params: dict[str, object] = dict(
            administration_id=self.administration,
            actor_user_id=self.user,
            customer_id=CUSTOMER,
            signed_on=date(2026, 1, 10),
            today=TODAY,
            debtor_name="De Vries Holding B.V.",
            debtor_iban="NL02 ABNA 0123 4567 89",
        )
        params.update(overrides)
        return await self.service.create_mandate(**params)  # type: ignore[arg-type]

    def invoice(self, reference: str = "2026-1", **overrides: object) -> Candidate:
        candidate = Candidate(
            invoice_id=uuid.uuid4(),
            invoice_reference=reference,
            customer_id=CUSTOMER,
            customer_name="De Vries Holding B.V.",
            due_date=date(2026, 9, 10),
            outstanding=Decimal("121.00"),
            in_live_collection=False,
        )
        candidate = replace(candidate, **overrides)  # type: ignore[arg-type]
        self.repository.invoices.append(candidate)
        return candidate

    async def batch(self, **overrides: object):  # type: ignore[no-untyped-def]
        params: dict[str, object] = dict(
            administration_id=self.administration,
            actor_user_id=self.user,
            collection_date=COLLECT_ON,
            today=TODAY,
            now=NOW,
        )
        params.update(overrides)
        return await self.service.create_batch(**params)  # type: ignore[arg-type]


def _setup(*, role: str = "Accountant", **repository_overrides: object) -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(administration_id=world.acme_books, organization_id=world.acme)
    for name, value in repository_overrides.items():
        setattr(repository, name, value)
    payments = FailingPayments()
    audit = InMemoryAuditRepository()
    service = SepaDirectDebitService(
        repository=repository,  # type: ignore[arg-type]
        payments=payments,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, payments, audit, world.user, world.acme_books)


def _sequence_types(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [e.text or "" for e in root.iter() if e.tag.endswith("}SeqTp")]


# --- mandates ----------------------------------------
async def test_a_mandate_is_stored_normalised_and_audited_without_personal_data() -> None:
    setup = _setup()

    mandate = await setup.mandate(debtor_bic="abna nl2a")

    assert mandate.debtor_iban == "NL02ABNA0123456789"  # compact, upper case
    assert mandate.debtor_bic == "ABNANL2A"
    assert mandate.mandate_reference.startswith("LEDGR-")  # generated when omitted
    assert mandate.is_active and mandate.scheme is MandateScheme.CORE
    entry = setup.audit._entries[-1]
    assert entry.action == "create_sepa_mandate"
    assert "NL02" not in str(entry.detail) and "Vries" not in str(entry.detail)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"signed_on": date(2026, 9, 22)}, "signed_in_future"),
        ({"debtor_name": "  "}, "holder_name_required"),
        ({"debtor_iban": "NL02ABNA0123456780"}, "iban_invalid"),
        ({"debtor_bic": "ABC"}, "bic_invalid"),
        ({"mandate_reference": "has space"}, "reference_invalid"),
    ],
)
async def test_an_unusable_mandate_is_refused_with_a_code_and_nothing_is_stored(
    overrides: dict[str, object], code: str
) -> None:
    setup = _setup()
    with pytest.raises(CollectionInvalid) as refused:
        await setup.mandate(**overrides)
    assert refused.value.code == code
    assert setup.repository.mandates == []


async def test_a_mandate_for_an_unknown_customer_is_refused() -> None:
    setup = _setup(customer_known=False)
    with pytest.raises(SepaCustomerNotFound):
        await setup.mandate()


async def test_a_mandate_is_revoked_once() -> None:
    setup = _setup()
    mandate = await setup.mandate()
    revoked = await setup.service.revoke_mandate(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        mandate_id=mandate.id,
        reason="Klant heeft opgezegd",
    )
    assert not revoked.is_active
    with pytest.raises(MandateAlreadyRevoked):
        await setup.service.revoke_mandate(
            administration_id=setup.administration, actor_user_id=setup.user, mandate_id=mandate.id
        )
    with pytest.raises(MandateNotFound):
        await setup.service.revoke_mandate(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            mandate_id=uuid.uuid4(),
        )


async def test_an_invoicer_may_register_a_mandate_but_not_collect_on_it() -> None:
    setup = _setup(role="Invoicer")
    await setup.mandate()  # `create sales_invoice`
    setup.invoice()
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.batch()
    assert setup.repository.batches == {}
    assert any(e.outcome.value == "denied" for e in setup.audit._entries)


# --- a batch ----------------------------------------
async def test_a_batch_collects_the_outstanding_amount_on_the_mandate_and_reserves_it() -> None:
    setup = _setup()
    mandate = await setup.mandate(mandate_reference="M-1")
    invoice = setup.invoice()

    result = await setup.batch()

    (item,) = result.items
    assert item.invoice_id == invoice.invoice_id and item.mandate_id == mandate.id
    assert item.amount == Decimal("121.00") and item.status == "submitted"
    assert item.sequence_type is SequenceType.FIRST
    assert item.end_to_end_id == "2026-1"
    assert result.batch.total_amount == Decimal("121.00") and result.batch.item_count == 1
    xml = setup.repository.files[result.batch.id]
    assert "<MndtId>M-1</MndtId>" in xml and '<InstdAmt Ccy="EUR">121.00</InstdAmt>' in xml
    assert len(result.batch.file_sha256) == 64


async def test_the_second_invoice_on_a_new_mandate_in_the_same_file_is_recurring() -> None:
    setup = _setup()
    await setup.mandate()
    setup.invoice("2026-1")
    setup.invoice("2026-2")

    result = await setup.batch()

    assert sorted(i.sequence_type.value for i in result.items) == ["FRST", "RCUR"]
    assert sorted(_sequence_types(setup.repository.files[result.batch.id])) == ["FRST", "RCUR"]


async def test_after_a_collection_the_next_one_on_that_mandate_is_recurring() -> None:
    setup = _setup()
    await setup.mandate()
    setup.invoice("2026-1")
    first = await setup.batch()
    await setup.service.record_collected(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=first.batch.id,
        item_id=first.items[0].id,
        bank_account_id=BANK,
        today=date(2026, 9, 25),
    )
    setup.repository.invoices.clear()
    setup.invoice("2026-2")

    second = await setup.batch(collection_date=date(2026, 10, 1))

    assert second.items[0].sequence_type is SequenceType.RECURRING


async def test_a_one_off_mandate_is_ooff_and_used_once() -> None:
    setup = _setup()
    await setup.mandate(kind=SequenceKind.ONE_OFF)
    setup.invoice("2026-1")
    result = await setup.batch()
    assert result.items[0].sequence_type is SequenceType.ONE_OFF
    setup.invoice("2026-2")
    with pytest.raises(NothingToCollect) as refused:
        await setup.batch(invoice_ids=[setup.repository.invoices[1].invoice_id])
    assert refused.value.skipped[0].reason is SkipReason.MANDATE_LAPSED


async def test_invoices_asked_for_by_name_are_reported_with_the_reason_they_were_left_out() -> None:
    setup = _setup()
    await setup.mandate()
    good = setup.invoice("2026-1")
    paid = setup.invoice("2026-2", outstanding=Decimal("0.00"))
    not_due = setup.invoice("2026-3", due_date=date(2026, 12, 1))
    reserved = setup.invoice("2026-4", in_live_collection=True)
    stranger = setup.invoice("2026-5", customer_id=uuid.uuid4())
    unknown = uuid.uuid4()

    result = await setup.batch(
        invoice_ids=[
            good.invoice_id,
            paid.invoice_id,
            not_due.invoice_id,
            reserved.invoice_id,
            stranger.invoice_id,
            unknown,
        ]
    )

    assert [i.invoice_id for i in result.items] == [good.invoice_id]
    assert {s.invoice_id: s.reason for s in result.skipped} == {
        unknown: SkipReason.NOT_FOUND,
        paid.invoice_id: SkipReason.NOTHING_OUTSTANDING,
        not_due.invoice_id: SkipReason.NOT_YET_DUE,
        reserved.invoice_id: SkipReason.ALREADY_IN_COLLECTION,
        stranger.invoice_id: SkipReason.NO_MANDATE,
    }


async def test_selecting_everything_reports_only_what_somebody_must_act_on() -> None:
    setup = _setup()
    await setup.mandate(signed_on=date(2020, 1, 1))  # lapsed
    lapsed = setup.invoice("2026-1")
    setup.invoice("2026-2", customer_id=uuid.uuid4())  # no mandate: just not collected

    with pytest.raises(NothingToCollect) as refused:
        await setup.batch()

    assert [(s.invoice_id, s.reason) for s in refused.value.skipped] == [
        (lapsed.invoice_id, SkipReason.MANDATE_LAPSED)
    ]
    assert setup.repository.batches == {}  # nothing written


async def test_a_batch_needs_a_configured_creditor() -> None:
    for details, missing in (
        (None, "name"),
        (CreditorDetails("Bakker", None, "DE98ZZZ09999999999"), "iban"),
        (CreditorDetails("Bakker", IBAN_OK, None), "sepa_creditor_id"),
    ):
        setup = _setup(creditor_details=details)
        await setup.mandate()
        setup.invoice()
        with pytest.raises(CreditorNotConfigured) as refused:
            await setup.batch()
        assert refused.value.missing == missing
        assert setup.repository.batches == {}


@pytest.mark.parametrize("bad", [TODAY, date(2026, 9, 20), date(2026, 9, 26), date(2027, 1, 4)])
async def test_the_collection_date_is_checked(bad: date) -> None:
    setup = _setup()
    await setup.mandate()
    setup.invoice()
    with pytest.raises(CollectionInvalid) as refused:
        await setup.batch(collection_date=bad)
    assert refused.value.code == "collection_date_invalid"


async def test_losing_the_reservation_race_writes_nothing() -> None:
    setup = _setup(reservation_lost=True)
    await setup.mandate()
    setup.invoice()
    with pytest.raises(BatchRaced):
        await setup.batch()
    assert setup.repository.batches == {} and setup.repository.items == []


async def test_the_batch_is_audited_and_downloading_the_file_is_too() -> None:
    setup = _setup()
    await setup.mandate()
    setup.invoice()
    result = await setup.batch()
    batch, xml = await setup.service.file(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
    )
    assert xml == setup.repository.files[batch.id]
    actions = [e.action for e in setup.audit._entries]
    assert "create_sepa_collection_batch" in actions
    assert "download_sepa_collection_file" in actions
    with pytest.raises(BatchNotFound):
        await setup.service.file(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            batch_id=uuid.uuid4(),
        )


# --- outcomes ----------------------------------------
async def _submitted(setup: Setup):  # type: ignore[no-untyped-def]
    await setup.mandate()
    setup.invoice()
    return await setup.batch()


async def test_a_collected_item_records_a_payment_dated_on_the_collection_date() -> None:
    setup = _setup()
    result = await _submitted(setup)
    item = result.items[0]

    decided = await setup.service.record_collected(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
        item_id=item.id,
        bank_account_id=BANK,
        today=date(2026, 9, 25),
    )

    assert decided.status == "collected" and decided.payment_id is not None
    (call,) = setup.payments.calls
    assert call["invoice_id"] == item.invoice_id and call["amount"] == Decimal("121.00")
    assert call["paid_on"] == COLLECT_ON and call["bank_account_id"] == BANK
    assert call["method"] is PaymentMethod.OTHER and call["reference"] == item.end_to_end_id
    assert setup.repository.touched == [(item.mandate_id, COLLECT_ON)]


async def test_a_collection_cannot_be_confirmed_before_its_date() -> None:
    setup = _setup()
    result = await _submitted(setup)
    with pytest.raises(CollectionNotDue) as refused:
        await setup.service.record_collected(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            batch_id=result.batch.id,
            item_id=result.items[0].id,
            bank_account_id=BANK,
            today=date(2026, 9, 23),
        )
    assert refused.value.collection_date == COLLECT_ON
    assert setup.payments.calls == []


async def test_a_refused_payment_leaves_the_item_undecided() -> None:
    """The invoice was paid another way meanwhile: no half-recorded collection."""
    setup = _setup()
    result = await _submitted(setup)
    setup.payments.error = RuntimeError("exceeds outstanding")
    with pytest.raises(RuntimeError):
        await setup.service.record_collected(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            batch_id=result.batch.id,
            item_id=result.items[0].id,
            bank_account_id=BANK,
            today=date(2026, 9, 25),
        )
    assert setup.repository.items[0].status == "submitted"
    assert setup.repository.touched == []


async def test_an_outcome_is_recorded_once() -> None:
    setup = _setup()
    result = await _submitted(setup)
    kwargs = dict(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
        item_id=result.items[0].id,
    )
    await setup.service.record_failed(reason="AM04 insufficient funds", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ItemAlreadyDecided):
        await setup.service.record_failed(reason="again", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ItemAlreadyDecided):
        await setup.service.record_collected(
            bank_account_id=BANK,
            today=date(2026, 9, 25),
            **kwargs,  # type: ignore[arg-type]
        )
    assert setup.payments.calls == []  # a failure records no money


async def test_a_failure_needs_a_reason_and_frees_the_invoice_for_another_batch() -> None:
    setup = _setup()
    result = await _submitted(setup)
    kwargs = dict(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
        item_id=result.items[0].id,
    )
    with pytest.raises(CollectionInvalid) as refused:
        await setup.service.record_failed(reason="  ", **kwargs)  # type: ignore[arg-type]
    assert refused.value.code == "failure_reason_required"

    failed = await setup.service.record_failed(reason="MS02", **kwargs)  # type: ignore[arg-type]

    assert failed.failure_reason == "MS02"
    again = await setup.batch(collection_date=date(2026, 10, 1))  # the invoice is free again
    assert again.items[0].sequence_type is SequenceType.FIRST  # a failed FRST is still FRST


# --- cancelling ----------------------------------------
async def test_an_unsent_batch_can_be_cancelled_and_its_invoices_collected_again() -> None:
    setup = _setup()
    result = await _submitted(setup)

    cancelled = await setup.service.cancel_batch(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
    )

    assert cancelled.is_cancelled
    assert [i.status for i in setup.repository.items] == ["cancelled"]
    again = await setup.batch(collection_date=date(2026, 10, 1))
    assert len(again.items) == 1


async def test_a_batch_with_an_outcome_cannot_be_cancelled() -> None:
    setup = _setup()
    result = await _submitted(setup)
    await setup.service.record_failed(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
        item_id=result.items[0].id,
        reason="MS02",
    )
    with pytest.raises(BatchNotCancellable):
        await setup.service.cancel_batch(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            batch_id=result.batch.id,
        )

"""SEPA direct debit - mandates, collection batches, outcomes - SI-09 (ADR-077).

--- Who may do what ---

Managing a mandate is `create sales_invoice`: whoever raises the invoice may register the
authorisation the customer signed. Generating the file, downloading it and recording how each
collection turned out are `post journal_entry`: the file is an instruction to debit customers'
accounts and recording an outcome posts a receipt, and the person who raises invoices must not
also be the one who takes the money (the separation ADR-070 makes for payments). Otherwise one
person could register a mandate nobody signed and collect on it.

--- Atomicity ---

A batch is written in a savepoint: the file, its items, and the reservation of each invoice
(one live collection per invoice, a unique index) all succeed or none does. Recording that a
collection succeeded records the payment - through `SalesPaymentService`, so it posts like any
other - and flips the item in the same savepoint, so a payment without its item, or the
reverse, cannot exist.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.iban import parse as parse_iban
from api.invoicing.model import InvoicingError, NotAuthorizedToInvoice
from api.invoicing.payments import InvoicePayment, PaymentMethod
from api.invoicing.sepa import (
    CollectionLine,
    Creditor,
    MandateFacts,
    MandateScheme,
    SepaInvalid,
    SequenceKind,
    SequenceType,
    SkipReason,
    build_pain008,
    choose_mandate,
    mandate_lapsed,
    sanitise,
    sequence_type_for,
    validate_collection_date,
    validate_mandate_reference,
)

MANAGE_MANDATES = ("create", "sales_invoice")
COLLECT = ("post", "journal_entry")

#: A file is meant to be checked by a person before it is uploaded; a runaway select-all is not.
MAX_BATCH_ITEMS = 500


# --- values ----------------------------------------
@dataclass(frozen=True, slots=True)
class Mandate:
    id: uuid.UUID
    administration_id: uuid.UUID
    customer_id: uuid.UUID
    mandate_reference: str
    scheme: MandateScheme
    kind: SequenceKind
    signed_on: date
    debtor_name: str
    debtor_iban: str
    debtor_bic: str | None
    status: str
    created_at: datetime
    created_by_user_id: uuid.UUID
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    last_collected_on: date | None = None
    #: A submitted or collected collection exists: the next one is not the first.
    used: bool = False

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def facts(self) -> MandateFacts:
        return MandateFacts(
            id=self.id,
            is_active=self.is_active,
            scheme=self.scheme,
            kind=self.kind,
            signed_on=self.signed_on,
            last_collected_on=self.last_collected_on,
            used=self.used,
        )

    def is_lapsed(self, today: date) -> bool:
        return mandate_lapsed(self.signed_on, self.last_collected_on, today)


@dataclass(frozen=True, slots=True)
class CreditorDetails:
    name: str
    iban: str | None
    creditor_id: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """An invoice that might be collected, with what deciding needs."""

    invoice_id: uuid.UUID
    invoice_reference: str | None
    customer_id: uuid.UUID | None
    customer_name: str
    due_date: date | None
    outstanding: Decimal
    in_live_collection: bool


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    id: uuid.UUID
    administration_id: uuid.UUID
    message_id: str
    collection_date: date
    item_count: int
    total_amount: Decimal
    file_sha256: str
    created_at: datetime
    created_by_user_id: uuid.UUID
    cancelled_at: datetime | None = None

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


@dataclass(frozen=True, slots=True)
class CollectionItem:
    id: uuid.UUID
    administration_id: uuid.UUID
    batch_id: uuid.UUID
    invoice_id: uuid.UUID
    mandate_id: uuid.UUID
    amount: Decimal
    sequence_type: SequenceType
    end_to_end_id: str
    status: str
    decided_at: datetime | None = None
    failure_reason: str | None = None
    payment_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Skipped:
    invoice_id: uuid.UUID
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch: CollectionBatch
    items: tuple[CollectionItem, ...]
    skipped: tuple[Skipped, ...] = field(default_factory=tuple)


# --- errors ----------------------------------------
class CollectionInvalid(InvoicingError):
    """A request the rules refuse. `code` names which, for the reader's-language message."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class SepaCustomerNotFound(InvoicingError):
    pass


class MandateNotFound(InvoicingError):
    pass


class MandateReferenceTaken(InvoicingError):
    pass


class MandateAlreadyRevoked(InvoicingError):
    pass


class CreditorNotConfigured(InvoicingError):
    def __init__(self, missing: str) -> None:
        self.missing = missing
        super().__init__(f"the administration has no {missing} on file")


class NothingToCollect(InvoicingError):
    def __init__(self, skipped: Sequence[Skipped]) -> None:
        self.skipped = tuple(skipped)
        super().__init__("no invoice can be collected")


class TooManyInvoices(InvoicingError):
    pass


class BatchNotFound(InvoicingError):
    pass


class BatchNotCancellable(InvoicingError):
    pass


class BatchRaced(InvoicingError):
    """Another request reserved one of these invoices first; nothing was written."""


class ItemNotFound(InvoicingError):
    pass


class ItemAlreadyDecided(InvoicingError):
    pass


class CollectionNotDue(InvoicingError):
    def __init__(self, collection_date: date) -> None:
        self.collection_date = collection_date
        super().__init__(f"the collection date {collection_date} has not been reached")


# --- ports ----------------------------------------
class PaymentRecorder(Protocol):
    async def record(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        amount: Decimal,
        paid_on: date,
        method: PaymentMethod,
        bank_account_id: uuid.UUID,
        reference: str | None = None,
        correlation_id: str | None = None,
    ) -> InvoicePayment: ...


class SepaRepository(Protocol):
    def savepoint(self) -> AbstractAsyncContextManager[Any]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def creditor(self, *, administration_id: uuid.UUID) -> CreditorDetails | None: ...

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool: ...

    async def insert_mandate(self, mandate: Mandate) -> Mandate: ...

    async def get_mandate(
        self, *, administration_id: uuid.UUID, mandate_id: uuid.UUID
    ) -> Mandate | None: ...

    async def mandates_for_customer(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Sequence[Mandate]: ...

    async def revoke_mandate(
        self,
        *,
        administration_id: uuid.UUID,
        mandate_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> Mandate | None: ...

    async def candidates(
        self, *, administration_id: uuid.UUID, invoice_ids: Sequence[uuid.UUID] | None
    ) -> Sequence[Candidate]: ...

    async def insert_batch(self, batch: CollectionBatch, file_xml: str) -> CollectionBatch: ...

    async def insert_item(self, item: CollectionItem) -> CollectionItem: ...

    async def get_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> CollectionBatch | None: ...

    async def batch_file(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> str | None: ...

    async def list_batches(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[CollectionBatch]: ...

    async def items_for_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> Sequence[CollectionItem]: ...

    async def get_item(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID
    ) -> CollectionItem | None: ...

    async def decide_item(
        self,
        *,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        status: str,
        user_id: uuid.UUID,
        failure_reason: str | None,
        payment_id: uuid.UUID | None,
    ) -> CollectionItem | None:
        """Conditional on the item still being `submitted`; None when it no longer is."""
        ...

    async def cancel_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID, user_id: uuid.UUID
    ) -> CollectionBatch | None: ...

    async def touch_mandate(
        self, *, administration_id: uuid.UUID, mandate_id: uuid.UUID, collected_on: date
    ) -> None: ...


# --- the service ----------------------------------------
class SepaDirectDebitService:
    def __init__(
        self,
        repository: SepaRepository,
        payments: PaymentRecorder,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._payments = payments
        self._authorization = authorization
        self._audit = audit_log

    # -- mandates ----------------------------------------
    async def create_mandate(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        customer_id: uuid.UUID,
        signed_on: date,
        today: date,
        debtor_name: str,
        debtor_iban: str,
        debtor_bic: str | None = None,
        mandate_reference: str | None = None,
        scheme: MandateScheme = MandateScheme.CORE,
        kind: SequenceKind = SequenceKind.RECURRING,
        correlation_id: str | None = None,
    ) -> Mandate:
        await self._require(MANAGE_MANDATES, actor_user_id, administration_id)

        if signed_on > today:
            raise CollectionInvalid("signed_in_future")
        name = (debtor_name or "").strip()
        if not name:
            raise CollectionInvalid("holder_name_required")
        iban = parse_iban(debtor_iban)
        if iban is None:
            raise CollectionInvalid("iban_invalid")
        bic = (debtor_bic or "").replace(" ", "").upper() or None
        if bic is not None and len(bic) not in (8, 11):
            raise CollectionInvalid("bic_invalid")
        try:
            reference = validate_mandate_reference(
                mandate_reference or f"LEDGR-{secrets.token_hex(6).upper()}"
            )
        except SepaInvalid as exc:
            raise CollectionInvalid("reference_invalid", str(exc)) from exc

        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise SepaCustomerNotFound(f"customer {customer_id} not found")

        mandate = await self._repository.insert_mandate(
            Mandate(
                id=uuid.uuid4(),
                administration_id=administration_id,
                customer_id=customer_id,
                mandate_reference=reference,
                scheme=scheme,
                kind=kind,
                signed_on=signed_on,
                debtor_name=name,
                debtor_iban=iban.value,
                debtor_bic=bic,
                status="active",
                created_at=datetime.now().astimezone(),
                created_by_user_id=actor_user_id,
            )
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_sepa_mandate",
            resource_type="sepa_mandate",
            resource_id=mandate.id,
            correlation_id=correlation_id,
            # The reference and the scheme, not the IBAN or the holder: personal data stays in
            # the mandate row, not the audit trail.
            detail={
                "customer_id": str(customer_id),
                "mandate_reference": reference,
                "scheme": scheme.value,
                "kind": kind.value,
            },
        )
        return mandate

    async def revoke_mandate(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        mandate_id: uuid.UUID,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Mandate:
        await self._require(MANAGE_MANDATES, actor_user_id, administration_id)
        existing = await self._repository.get_mandate(
            administration_id=administration_id, mandate_id=mandate_id
        )
        if existing is None:
            raise MandateNotFound(f"mandate {mandate_id} not found")
        if not existing.is_active:
            raise MandateAlreadyRevoked(f"mandate {mandate_id} is already revoked")
        revoked = await self._repository.revoke_mandate(
            administration_id=administration_id,
            mandate_id=mandate_id,
            user_id=actor_user_id,
            reason=(reason or "").strip() or None,
        )
        if revoked is None:  # revoked by a concurrent request
            raise MandateAlreadyRevoked(f"mandate {mandate_id} is already revoked")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="revoke_sepa_mandate",
            resource_type="sepa_mandate",
            resource_id=mandate_id,
            correlation_id=correlation_id,
            detail={"customer_id": str(existing.customer_id)},
        )
        return revoked

    async def mandates(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Sequence[Mandate]:
        await self._require(MANAGE_MANDATES, actor_user_id, administration_id)
        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise SepaCustomerNotFound(f"customer {customer_id} not found")
        return await self._repository.mandates_for_customer(
            administration_id=administration_id, customer_id=customer_id
        )

    # -- a batch ----------------------------------------
    async def create_batch(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        collection_date: date,
        today: date,
        now: datetime,
        invoice_ids: Sequence[uuid.UUID] | None = None,
        correlation_id: str | None = None,
    ) -> BatchResult:
        """Generate a pain.008 file for the given invoices - or, with none given, for every
        invoice of a customer with a usable mandate - and reserve each of them.

        Invoices that cannot be collected are reported in `skipped`, never silently dropped; if
        none can, `NothingToCollect` carries the reasons and nothing is written.
        """
        await self._require(COLLECT, actor_user_id, administration_id)
        try:
            validate_collection_date(collection_date, today)
        except SepaInvalid as exc:
            raise CollectionInvalid("collection_date_invalid", str(exc)) from exc
        if invoice_ids is not None and len(set(invoice_ids)) > MAX_BATCH_ITEMS:
            raise TooManyInvoices(f"at most {MAX_BATCH_ITEMS} invoices per file")

        creditor = await self._repository.creditor(administration_id=administration_id)
        if creditor is None or not creditor.name.strip():
            raise CreditorNotConfigured("name")
        if not creditor.iban:
            raise CreditorNotConfigured("iban")
        if not creditor.creditor_id:
            raise CreditorNotConfigured("sepa_creditor_id")

        wanted = list(dict.fromkeys(invoice_ids)) if invoice_ids is not None else None
        found = await self._repository.candidates(
            administration_id=administration_id, invoice_ids=wanted
        )
        skipped: list[Skipped] = []
        if wanted is not None:
            present = {c.invoice_id for c in found}
            skipped.extend(Skipped(i, SkipReason.NOT_FOUND) for i in wanted if i not in present)

        lines: list[CollectionLine] = []
        picked: list[tuple[Candidate, Mandate, SequenceType, str]] = []
        first_taken: set[uuid.UUID] = set()
        used_ids: set[str] = set()
        mandates_by_customer: dict[uuid.UUID, Sequence[Mandate]] = {}

        for candidate in found[:MAX_BATCH_ITEMS]:
            reason = self._skip_reason(candidate, collection_date)
            mandate: Mandate | None = None
            if reason is None:
                assert candidate.customer_id is not None
                if candidate.customer_id not in mandates_by_customer:
                    mandates_by_customer[
                        candidate.customer_id
                    ] = await self._repository.mandates_for_customer(
                        administration_id=administration_id, customer_id=candidate.customer_id
                    )
                pool = mandates_by_customer[candidate.customer_id]
                chosen = choose_mandate([m.facts() for m in pool], today)
                if isinstance(chosen, SkipReason):
                    reason = chosen
                else:
                    mandate = next(m for m in pool if m.id == chosen.id)
            if reason is not None or mandate is None:
                # Asked for by name, every refusal is reported. Selecting everything, only the
                # one somebody must act on (a lapsed mandate) is: most invoices simply have no
                # mandate, and listing all of them would drown it.
                if wanted is not None or reason is SkipReason.MANDATE_LAPSED:
                    skipped.append(Skipped(candidate.invoice_id, reason or SkipReason.NO_MANDATE))
                continue

            used = mandate.used or mandate.id in first_taken
            sequence = sequence_type_for(mandate.kind, used)
            first_taken.add(mandate.id)
            e2e = _end_to_end_id(candidate, used_ids)
            picked.append((candidate, mandate, sequence, e2e))
            lines.append(
                CollectionLine(
                    end_to_end_id=e2e,
                    amount=candidate.outstanding,
                    sequence_type=sequence,
                    scheme=mandate.scheme,
                    mandate_reference=mandate.mandate_reference,
                    signed_on=mandate.signed_on,
                    debtor_name=mandate.debtor_name,
                    debtor_iban=mandate.debtor_iban,
                    debtor_bic=mandate.debtor_bic,
                    remittance=f"Factuur {candidate.invoice_reference or ''}".strip(),
                )
            )

        if not lines:
            raise NothingToCollect(skipped)

        message_id = f"LEDGR-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3).upper()}"
        try:
            xml = build_pain008(
                message_id=message_id,
                created_at=now,
                creditor=Creditor(creditor.name, creditor.iban, creditor.creditor_id),
                collection_date=collection_date,
                lines=lines,
            )
        except SepaInvalid as exc:
            raise CollectionInvalid("file_invalid", str(exc)) from exc

        total = sum((line.amount for line in lines), Decimal(0))
        batch_id = uuid.uuid4()
        try:
            async with self._repository.savepoint():
                batch = await self._repository.insert_batch(
                    CollectionBatch(
                        id=batch_id,
                        administration_id=administration_id,
                        message_id=message_id,
                        collection_date=collection_date,
                        item_count=len(lines),
                        total_amount=total,
                        file_sha256=hashlib.sha256(xml.encode("utf-8")).hexdigest(),
                        created_at=now,
                        created_by_user_id=actor_user_id,
                    ),
                    xml,
                )
                items = [
                    await self._repository.insert_item(
                        CollectionItem(
                            id=uuid.uuid4(),
                            administration_id=administration_id,
                            batch_id=batch_id,
                            invoice_id=candidate.invoice_id,
                            mandate_id=mandate.id,
                            amount=candidate.outstanding,
                            sequence_type=sequence,
                            end_to_end_id=e2e,
                            status="submitted",
                        )
                    )
                    for candidate, mandate, sequence, e2e in picked
                ]
        except ReservationLost as exc:
            raise BatchRaced("an invoice was reserved by another request") from exc

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_sepa_collection_batch",
            resource_type="sepa_collection_batch",
            resource_id=batch.id,
            correlation_id=correlation_id,
            detail={
                "message_id": message_id,
                "collection_date": collection_date.isoformat(),
                "items": len(items),
                "total": str(total),
                "skipped": len(skipped),
            },
        )
        return BatchResult(batch=batch, items=tuple(items), skipped=tuple(skipped))

    async def batch(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, batch_id: uuid.UUID
    ) -> tuple[CollectionBatch, Sequence[CollectionItem]]:
        await self._require(COLLECT, actor_user_id, administration_id)
        batch = await self._batch_or_refuse(administration_id, batch_id)
        items = await self._repository.items_for_batch(
            administration_id=administration_id, batch_id=batch_id
        )
        return batch, items

    async def batches(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, limit: int = 50
    ) -> Sequence[CollectionBatch]:
        await self._require(COLLECT, actor_user_id, administration_id)
        return await self._repository.list_batches(administration_id=administration_id, limit=limit)

    async def file(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        batch_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> tuple[CollectionBatch, str]:
        await self._require(COLLECT, actor_user_id, administration_id)
        batch = await self._batch_or_refuse(administration_id, batch_id)
        xml = await self._repository.batch_file(
            administration_id=administration_id, batch_id=batch_id
        )
        if xml is None:  # pragma: no cover - the batch row was just read
            raise BatchNotFound(f"batch {batch_id} not found")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="download_sepa_collection_file",
            resource_type="sepa_collection_batch",
            resource_id=batch_id,
            correlation_id=correlation_id,
            detail={"message_id": batch.message_id},
        )
        return batch, xml

    async def cancel_batch(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        batch_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> CollectionBatch:
        """Withdraw a file that was never uploaded (or was refused whole): every item must
        still be undecided, and the invoices are free to be collected again."""
        await self._require(COLLECT, actor_user_id, administration_id)
        batch = await self._batch_or_refuse(administration_id, batch_id)
        items = await self._repository.items_for_batch(
            administration_id=administration_id, batch_id=batch_id
        )
        if batch.is_cancelled or any(item.status != "submitted" for item in items):
            raise BatchNotCancellable(
                f"batch {batch_id} is cancelled or already has recorded outcomes"
            )
        async with self._repository.savepoint():
            cancelled = await self._repository.cancel_batch(
                administration_id=administration_id, batch_id=batch_id, user_id=actor_user_id
            )
            if cancelled is None:
                # An outcome was recorded meanwhile: leaving the savepoint undoes the items
                # already marked cancelled, so the batch is never half-cancelled.
                raise BatchNotCancellable(f"batch {batch_id} can no longer be cancelled")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="cancel_sepa_collection_batch",
            resource_type="sepa_collection_batch",
            resource_id=batch_id,
            correlation_id=correlation_id,
            detail={"message_id": batch.message_id, "items": len(items)},
        )
        return cancelled

    # -- outcomes ----------------------------------------
    async def record_collected(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        batch_id: uuid.UUID,
        item_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> CollectionItem:
        """The bank confirms the money arrived: record the payment and settle the item.

        Both happen in one savepoint. If the invoice was paid another way in the meantime the
        payment is refused (it would exceed what is owed), the item stays undecided and the
        error says so - the person then marks it failed or cancels the batch."""
        await self._require(COLLECT, actor_user_id, administration_id)
        batch = await self._batch_or_refuse(administration_id, batch_id)
        item = await self._pending_item(administration_id, batch_id, item_id)
        if batch.collection_date > today:
            raise CollectionNotDue(batch.collection_date)

        async with self._repository.savepoint():
            payment = await self._payments.record(
                administration_id=administration_id,
                invoice_id=item.invoice_id,
                actor_user_id=actor_user_id,
                amount=item.amount,
                paid_on=batch.collection_date,
                method=PaymentMethod.OTHER,
                bank_account_id=bank_account_id,
                reference=item.end_to_end_id,
                correlation_id=correlation_id,
            )
            decided = await self._repository.decide_item(
                administration_id=administration_id,
                item_id=item_id,
                status="collected",
                user_id=actor_user_id,
                failure_reason=None,
                payment_id=payment.id,
            )
            if decided is None:
                # Decided by a concurrent request: leaving the savepoint undoes the payment.
                raise ItemAlreadyDecided(f"collection {item_id} was decided by another request")
            await self._repository.touch_mandate(
                administration_id=administration_id,
                mandate_id=item.mandate_id,
                collected_on=batch.collection_date,
            )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="record_sepa_collection",
            resource_type="sepa_collection",
            resource_id=item_id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(item.invoice_id),
                "amount": str(item.amount),
                "payment_id": str(payment.id),
            },
        )
        return decided

    async def record_failed(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        batch_id: uuid.UUID,
        item_id: uuid.UUID,
        reason: str,
        correlation_id: str | None = None,
    ) -> CollectionItem:
        """The bank returned or rejected the collection. The invoice stays owed and is free to
        be collected again, or chased."""
        await self._require(COLLECT, actor_user_id, administration_id)
        reason = sanitise(reason or "", 140)
        if not reason:
            raise CollectionInvalid("failure_reason_required")
        await self._batch_or_refuse(administration_id, batch_id)
        item = await self._pending_item(administration_id, batch_id, item_id)
        decided = await self._repository.decide_item(
            administration_id=administration_id,
            item_id=item_id,
            status="failed",
            user_id=actor_user_id,
            failure_reason=reason,
            payment_id=None,
        )
        if decided is None:
            raise ItemAlreadyDecided(f"collection {item_id} was already decided")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="fail_sepa_collection",
            resource_type="sepa_collection",
            resource_id=item_id,
            correlation_id=correlation_id,
            detail={"invoice_id": str(item.invoice_id), "reason": reason},
        )
        return decided

    # -- internals ----------------------------------------
    def _skip_reason(self, candidate: Candidate, collection_date: date) -> SkipReason | None:
        if candidate.customer_id is None:
            return SkipReason.NO_MANDATE
        if candidate.outstanding <= 0:
            return SkipReason.NOTHING_OUTSTANDING
        if candidate.in_live_collection:
            return SkipReason.ALREADY_IN_COLLECTION
        if candidate.due_date is not None and candidate.due_date > collection_date:
            return SkipReason.NOT_YET_DUE
        return None

    async def _batch_or_refuse(
        self, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> CollectionBatch:
        batch = await self._repository.get_batch(
            administration_id=administration_id, batch_id=batch_id
        )
        if batch is None:
            raise BatchNotFound(f"batch {batch_id} not found")
        return batch

    async def _pending_item(
        self, administration_id: uuid.UUID, batch_id: uuid.UUID, item_id: uuid.UUID
    ) -> CollectionItem:
        item = await self._repository.get_item(administration_id=administration_id, item_id=item_id)
        if item is None or item.batch_id != batch_id:
            raise ItemNotFound(f"collection {item_id} not found in batch {batch_id}")
        if item.status != "submitted":
            raise ItemAlreadyDecided(f"collection {item_id} is already {item.status}")
        return item

    async def _require(
        self, permission: tuple[str, str], user_id: uuid.UUID, administration_id: uuid.UUID
    ) -> None:
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_type="sepa",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        detail: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise SepaCustomerNotFound(f"administration {administration_id} does not exist")
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


class ReservationLost(Exception):
    """Raised by the repository when an invoice's live-collection reservation is taken."""


def _end_to_end_id(candidate: Candidate, taken: set[str]) -> str:
    base = sanitise(candidate.invoice_reference or "", 35) or candidate.invoice_id.hex[:32]
    e2e, n = base, 1
    while e2e in taken:
        n += 1
        suffix = f"-{n}"
        e2e = base[: 35 - len(suffix)] + suffix
    taken.add(e2e)
    return e2e


__all__ = [
    "BatchNotCancellable",
    "BatchNotFound",
    "BatchRaced",
    "BatchResult",
    "Candidate",
    "CollectionBatch",
    "CollectionInvalid",
    "CollectionItem",
    "CollectionNotDue",
    "CreditorDetails",
    "CreditorNotConfigured",
    "ItemAlreadyDecided",
    "ItemNotFound",
    "Mandate",
    "MandateAlreadyRevoked",
    "MandateNotFound",
    "MandateReferenceTaken",
    "NothingToCollect",
    "ReservationLost",
    "SepaCustomerNotFound",
    "SepaDirectDebitService",
    "SepaRepository",
    "Skipped",
    "TooManyInvoices",
]

"""Batch invoicing - SI-17 (ADR-079).

One pass turns a list of entries into ordinary DRAFT invoices (and, if asked, issues them),
each in its own savepoint: an entry that cannot be raised (an archived customer, an incomplete
address, a date in no fiscal year) is reported with a code and never spoils the rest.

--- It is the same invoice ---

Every entry goes through `InvoicingService.create_draft`, and issuing through
`attempt_issue` (which is `InvoicingService.issue` in a savepoint), so the statutory gate, the
gapless number, the posting and the approval gate (SI-16) are exactly the ones a person meets.
A refused issue leaves a draft behind and burns no number. Nothing here sends.

--- Who may ---

`create sales_invoice` to run a batch. Issuing additionally needs `send sales_invoice` per
invoice - checked by `issue` itself - and an entry the caller may not issue is left as a draft
(`not_authorized_to_issue`), not a refusal of the batch.

--- Retries ---

`batch_key` names the pass. A repeat with the same key returns the first batch and creates
nothing (`already_exists`); the unique index holds it when two arrive at once, and the loser's
whole request - drafts included - rolls back.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.customers.model import (
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerNotFound,
)
from api.invoicing.batches import BatchEntry, BatchInvalid, resolve_entries
from api.invoicing.issue_attempt import attempt_issue
from api.invoicing.model import (
    CustomerDetailsConflict,
    CustomerDetailsMissing,
    InvoiceNotFound,
    InvoicingError,
    NotAuthorizedToInvoice,
)
from api.invoicing.quotes import QuoteLine
from api.invoicing.service import InvoicingService, NewLine

CREATE = ("create", "sales_invoice")

__all__ = [
    "BatchInvoicingService",
    "BatchItem",
    "InvoiceBatchNotFound",
    "BatchRecord",
    "BatchRejected",
    "BatchRepository",
    "BatchKeyTaken",
    "BatchResult",
]


@dataclass(frozen=True, slots=True)
class BatchRecord:
    id: uuid.UUID
    administration_id: uuid.UUID
    name: str
    batch_key: str | None
    invoice_date: date
    issue_requested: bool
    item_count: int
    drafted_count: int
    issued_count: int
    failed_count: int
    created_by_user_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BatchItem:
    id: uuid.UUID
    batch_id: uuid.UUID
    position: int
    customer_id: uuid.UUID
    status: str  # drafted | issued | failed
    invoice_id: uuid.UUID | None = None
    error_code: str | None = None
    issue_error: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch: BatchRecord
    items: tuple[BatchItem, ...]
    #: True when `batch_key` matched an earlier batch: nothing was created this time.
    already_exists: bool = False


class BatchRejected(InvoicingError):
    """The batch as a whole cannot run. `code` and `position` say why and where."""

    def __init__(self, code: str, position: int | None = None) -> None:
        self.code = code
        self.position = position
        super().__init__(code)


class InvoiceBatchNotFound(InvoicingError):
    pass


class BatchKeyTaken(InvoicingError):
    """Another request created a batch with this key at the same moment."""


class BatchRepository(Protocol):
    def savepoint(self) -> AbstractAsyncContextManager[Any]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def fiscal_year_for(
        self, *, administration_id: uuid.UUID, on: date
    ) -> uuid.UUID | None: ...

    async def batch_by_key(
        self, *, administration_id: uuid.UUID, batch_key: str
    ) -> BatchRecord | None: ...

    async def insert_batch(self, batch: BatchRecord, items: Sequence[BatchItem]) -> None:
        """Raises `BatchKeyTaken` when the key's unique index is already taken."""
        ...

    async def get_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> BatchRecord | None: ...

    async def items_for_batch(
        self, *, administration_id: uuid.UUID, batch_id: uuid.UUID
    ) -> Sequence[BatchItem]: ...

    async def list_batches(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[BatchRecord]: ...


@dataclass
class _Tally:
    drafted: int = 0
    issued: int = 0
    failed: int = 0
    items: list[BatchItem] = field(default_factory=list)


class BatchInvoicingService:
    def __init__(
        self,
        repository: BatchRepository,
        invoicing: InvoicingService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._invoicing = invoicing
        self._authorization = authorization
        self._audit = audit_log

    async def run(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        name: str,
        entries: Sequence[BatchEntry],
        today: date,
        now: datetime,
        invoice_date: date | None = None,
        default_lines: Sequence[QuoteLine] | None = None,
        default_notes: str | None = None,
        default_due_days: int | None = None,
        issue: bool = False,
        batch_key: str | None = None,
        correlation_id: str | None = None,
    ) -> BatchResult:
        await self._require(actor_user_id, administration_id)
        try:
            resolved = resolve_entries(
                name=name,
                entries=entries,
                default_lines=default_lines,
                default_notes=default_notes,
                default_due_days=default_due_days,
                today=today,
            )
        except BatchInvalid as exc:
            raise BatchRejected(exc.code, exc.position) from exc

        key = (batch_key or "").strip() or None
        if key is not None:
            existing = await self._repository.batch_by_key(
                administration_id=administration_id, batch_key=key
            )
            if existing is not None:
                items = await self._repository.items_for_batch(
                    administration_id=administration_id, batch_id=existing.id
                )
                return BatchResult(existing, tuple(items), already_exists=True)

        on = invoice_date or today
        fiscal_year = await self._repository.fiscal_year_for(
            administration_id=administration_id, on=on
        )
        batch_id = uuid.uuid4()
        tally = _Tally()

        for entry in resolved:
            status, invoice_id, error, issue_error = await self._one(
                administration_id=administration_id,
                actor_user_id=actor_user_id,
                fiscal_year=fiscal_year,
                on=on,
                issue=issue,
                customer_id=entry.customer_id,
                lines=entry.lines,
                notes=entry.notes,
                due_days=entry.due_days,
                correlation_id=correlation_id,
            )
            if status == "failed":
                tally.failed += 1
            elif status == "issued":
                tally.issued += 1
            else:
                tally.drafted += 1
            tally.items.append(
                BatchItem(
                    id=uuid.uuid4(),
                    batch_id=batch_id,
                    position=entry.position,
                    customer_id=entry.customer_id,
                    status=status,
                    invoice_id=invoice_id,
                    error_code=error,
                    issue_error=issue_error,
                )
            )

        record = BatchRecord(
            id=batch_id,
            administration_id=administration_id,
            name=name.strip(),
            batch_key=key,
            invoice_date=on,
            issue_requested=issue,
            item_count=len(resolved),
            drafted_count=tally.drafted,
            issued_count=tally.issued,
            failed_count=tally.failed,
            created_by_user_id=actor_user_id,
            created_at=now,
        )
        await self._repository.insert_batch(record, tally.items)
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="run_sales_invoice_batch",
            resource_id=batch_id,
            correlation_id=correlation_id,
            detail={
                "name": record.name,
                "entries": record.item_count,
                "drafted": tally.drafted,
                "issued": tally.issued,
                "failed": tally.failed,
                "issue_requested": issue,
            },
        )
        return BatchResult(record, tuple(tally.items))

    async def _one(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        fiscal_year: uuid.UUID | None,
        on: date,
        issue: bool,
        customer_id: uuid.UUID,
        lines: Sequence[QuoteLine],
        notes: str | None,
        due_days: int | None,
        correlation_id: str | None,
    ) -> tuple[str, uuid.UUID | None, str | None, str | None]:
        """`(status, invoice_id, error_code, issue_error)` for one entry."""
        if fiscal_year is None:
            return "failed", None, "no_fiscal_year", None

        new_lines = [
            NewLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_treatment=line.vat_treatment,
                discount_percent=line.discount_percent,
            )
            for line in lines
        ]
        try:
            async with self._repository.savepoint():
                view = await self._invoicing.create_draft(
                    administration_id=administration_id,
                    fiscal_year_id=fiscal_year,
                    actor_user_id=actor_user_id,
                    invoice_date=on,
                    customer_id=customer_id,
                    due_date=on + timedelta(days=due_days) if due_days is not None else None,
                    notes=notes,
                    lines=new_lines,
                    correlation_id=correlation_id,
                )
        except CustomerNotFound:
            return "failed", None, "customer_not_found", None
        except CustomerIsArchived:
            return "failed", None, "customer_archived", None
        except (CustomerAddressIncomplete, CustomerDetailsMissing, CustomerDetailsConflict):
            return "failed", None, "customer_details_incomplete", None
        except NotAuthorizedToInvoice:
            raise
        except InvoicingError:
            return "failed", None, "invoice_invalid", None

        invoice_id = view.invoice.id
        if not issue:
            return "drafted", invoice_id, None, None
        issued, issue_error = await attempt_issue(
            invoicing=self._invoicing,
            savepoint=self._repository.savepoint,
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            invoice_id=invoice_id,
        )
        if issued:
            return "issued", invoice_id, None, None
        return "drafted", invoice_id, None, issue_error

    async def batch(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, batch_id: uuid.UUID
    ) -> BatchResult:
        await self._require(actor_user_id, administration_id)
        record = await self._repository.get_batch(
            administration_id=administration_id, batch_id=batch_id
        )
        if record is None:
            raise InvoiceBatchNotFound(f"batch {batch_id} not found")
        items = await self._repository.items_for_batch(
            administration_id=administration_id, batch_id=batch_id
        )
        return BatchResult(record, tuple(items))

    async def batches(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, limit: int = 50
    ) -> Sequence[BatchRecord]:
        await self._require(actor_user_id, administration_id)
        return await self._repository.list_batches(administration_id=administration_id, limit=limit)

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = CREATE
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
        resource_id: uuid.UUID,
        detail: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise InvoiceNotFound(f"administration {administration_id} does not exist")
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="sales_invoice_batch",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )

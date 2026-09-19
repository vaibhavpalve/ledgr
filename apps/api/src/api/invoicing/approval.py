"""Draft - approve - send: SI-16 (ADR-078).

    A bookkeeper drafts on a client's behalf; the business owner approves before it sends.

--- The gate is at ISSUE ---

Issuing is the point of no return: it allocates the gapless number, freezes the VAT, posts
to the ledger and stores the PDF; sending can only follow. So the gate sits in
`InvoicingService.issue`, before the number is allocated, and every route to issuing meets it -
the invoice route, a recurring schedule with `auto_issue`, whatever comes next.

With `administration.invoice_approval_required` off (the default) there is no gate. With it on,
an issue is allowed when either

  * the actor holds `approve sales_invoice` - the owner's own authority, so an owner drafting
    their own invoice is not stuck waiting for somebody else to approve it, or
  * an approver has approved a request for exactly THIS draft as it is now.

--- Approval is of a version ---

`content_fingerprint` hashes what the customer would receive. An approval records the hash it
was given; if the draft changes afterwards the hashes differ, the approval stops counting
(`ApprovalState.STALE`), and a new request is needed. Without this a small invoice could be
approved and then edited into a large one.

--- Who holds what ---

`create sales_invoice` requests approval (the drafter). `approve sales_invoice` - an extension
capability granted to the Owner alone, see `api.authz.matrix` - approves or rejects. The
Bookkeeper and Accountant, which a firm holds on a client, deliberately do not have it: the
person who drafts must not be able to release.

Credit notes are outside this gate (`InvoicingService.credit` is a separate path) - flagged in
ADR-078.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.invoicing.model import (
    InvoiceAlreadyIssued,
    InvoiceNotFound,
    InvoicingError,
    NotAuthorizedToInvoice,
    SalesInvoice,
)
from api.invoicing.statutory import StatutoryFailure

__all__ = [
    "APPROVE_INVOICE",
    "ApprovalAlreadyDecided",
    "ApprovalNotFound",
    "ApprovalNotReady",
    "ApprovalNotRequired",
    "ApprovalReasonMissing",
    "ApprovalRepository",
    "ApprovalRequired",
    "ApprovalService",
    "ApprovalStale",
    "ApprovalState",
    "ApprovalStatus",
    "InvoiceApproval",
    "QueueEntry",
    "SalesInvoiceApprovalGate",
    "content_fingerprint",
]

APPROVE_INVOICE = ("approve", "sales_invoice")
REQUEST_APPROVAL = ("create", "sales_invoice")


class ApprovalState(enum.Enum):
    """Where an invoice stands for a reader asking "can this go out?"."""

    NONE = "none"  # never requested, or the request was replaced
    PENDING = "pending"
    APPROVED = "approved"  # and current
    REJECTED = "rejected"
    STALE = "stale"  # approved, but the draft has changed since


def content_fingerprint(invoice: SalesInvoice) -> str:
    """SHA-256 of what the customer would receive.

    The customer snapshot, the dates, the notes and every line, in a canonical form (sorted
    keys, decimals as plain strings so 95 and 95.0000 agree, dates as ISO). Nothing that
    changes without the document changing - ids, timestamps, status - is in it.
    """

    def money(value: Decimal) -> str:
        return format(value.normalize(), "f") if value != 0 else "0"

    def day(value: date | None) -> str | None:
        return value.isoformat() if value else None

    payload: dict[str, Any] = {
        "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
        "customer_name": invoice.customer_name,
        "customer_address": invoice.customer_address,
        "customer_country": invoice.customer_country,
        "customer_vat_number": invoice.customer_vat_number,
        "customer_language": invoice.customer_language.value,
        "invoice_date": day(invoice.invoice_date),
        "supply_date": day(invoice.supply_date),
        "due_date": day(invoice.due_date),
        "notes": invoice.notes,
        "lines": [
            {
                "position": line.position,
                "description": line.description,
                "quantity": money(line.quantity),
                "unit_price": money(line.unit_price),
                "discount_percent": money(line.discount_percent),
                "vat_treatment": line.vat_treatment,
            }
            for line in sorted(invoice.lines, key=lambda line: line.position)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InvoiceApproval:
    id: uuid.UUID
    administration_id: uuid.UUID
    invoice_id: uuid.UUID
    content_hash: str
    status: str  # pending | approved | rejected | superseded
    requested_by_user_id: uuid.UUID
    requested_at: datetime
    request_note: str | None = None
    decided_by_user_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """Everything a screen needs about one draft's approval."""

    required: bool
    state: ApprovalState
    approval: InvoiceApproval | None
    #: Whether THIS actor could issue right now (an approver, or a current approval).
    can_issue: bool


@dataclass(frozen=True, slots=True)
class QueueEntry:
    approval: InvoiceApproval
    customer_name: str
    invoice_date: date
    invoice_reference: str | None


# --- errors ----------------------------------------
class ApprovalRequired(InvoicingError):
    """The administration requires approval, and this draft has none that counts."""

    def __init__(self, state: ApprovalState) -> None:
        self.state = state
        super().__init__(f"this invoice needs approval before it can be issued ({state.value})")


class ApprovalNotRequired(InvoicingError):
    """Approval is switched off for this administration: there is nothing to request."""


class ApprovalNotFound(InvoicingError):
    """No pending request for this invoice."""


class ApprovalStale(InvoicingError):
    """The draft changed after it was put up for approval: what was asked is not what is
    there. Approving would approve something nobody looked at."""


class ApprovalAlreadyDecided(InvoicingError):
    pass


class ApprovalReasonMissing(InvoicingError):
    pass


class ApprovalNotReady(InvoicingError):
    """The draft cannot be issued as it stands, so asking for approval would be pointless."""

    def __init__(self, failures: Sequence[StatutoryFailure]) -> None:
        self.failures = tuple(failures)
        super().__init__("the invoice is not complete enough to be issued")


# --- ports ----------------------------------------
class ApprovalRepository(Protocol):
    def savepoint(self) -> AbstractAsyncContextManager[Any]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def approval_required(self, *, administration_id: uuid.UUID) -> bool: ...

    async def create_request(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        content_hash: str,
        user_id: uuid.UUID,
        note: str | None,
    ) -> InvoiceApproval:
        """Supersede any pending or approved request for the invoice, then insert a new
        pending one - in one step."""
        ...

    async def pending_for_invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceApproval | None: ...

    async def latest_for_invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceApproval | None: ...

    async def decide(
        self,
        *,
        administration_id: uuid.UUID,
        approval_id: uuid.UUID,
        status: str,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> InvoiceApproval | None:
        """Conditional on the request still being pending; None when it no longer is."""
        ...

    async def queue(
        self, *, administration_id: uuid.UUID, status: str, limit: int
    ) -> Sequence[QueueEntry]: ...


class InvoiceReader(Protocol):
    """What the service needs of `InvoicingService`, and nothing more."""

    async def view(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Any: ...


# --- the gate ----------------------------------------
async def _holds(
    authorization: AuthorizationService,
    permission: tuple[str, str],
    user_id: uuid.UUID,
    administration_id: uuid.UUID,
) -> bool:
    action, resource_type = permission
    decision = await authorization.authorize(
        AuthorizationRequest(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            target=AdministrationScope(administration_id),
            attributes=ResourceAttributes(),
        )
    )
    return bool(decision.allowed)


def state_of(approval: InvoiceApproval | None, fingerprint: str) -> ApprovalState:
    if approval is None or approval.status == "superseded":
        return ApprovalState.NONE
    if approval.status == "pending":
        return ApprovalState.PENDING
    if approval.status == "rejected":
        return ApprovalState.REJECTED
    return ApprovalState.APPROVED if approval.content_hash == fingerprint else ApprovalState.STALE


class SalesInvoiceApprovalGate:
    """Consulted by `InvoicingService.issue` before a number is allocated."""

    def __init__(self, repository: ApprovalRepository, authorization: AuthorizationService) -> None:
        self._repository = repository
        self._authorization = authorization

    async def check(self, invoice: SalesInvoice, *, actor_user_id: uuid.UUID) -> None:
        administration_id = invoice.administration_id
        if not await self._repository.approval_required(administration_id=administration_id):
            return
        if await _holds(self._authorization, APPROVE_INVOICE, actor_user_id, administration_id):
            return
        latest = await self._repository.latest_for_invoice(
            administration_id=administration_id, invoice_id=invoice.id
        )
        state = state_of(latest, content_fingerprint(invoice))
        if state is not ApprovalState.APPROVED:
            raise ApprovalRequired(state)


# --- the service ----------------------------------------
class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        invoices: InvoiceReader,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._invoices = invoices
        self._authorization = authorization
        self._audit = audit_log

    async def request(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        note: str | None = None,
        correlation_id: str | None = None,
    ) -> InvoiceApproval:
        """Put a draft up for approval. A new request replaces an earlier one (pending or
        approved), which is how a draft that changed gets re-approved."""
        await self._require(REQUEST_APPROVAL, actor_user_id, administration_id)
        if not await self._repository.approval_required(administration_id=administration_id):
            raise ApprovalNotRequired("invoice approval is not switched on for this administration")

        view = await self._invoices.view(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=actor_user_id,
        )
        invoice: SalesInvoice = view.invoice
        if not invoice.is_draft:
            raise InvoiceAlreadyIssued(f"invoice {invoice_id} is already issued")
        if view.statutory_failures:
            raise ApprovalNotReady(view.statutory_failures)

        async with self._repository.savepoint():
            approval = await self._repository.create_request(
                administration_id=administration_id,
                invoice_id=invoice_id,
                content_hash=content_fingerprint(invoice),
                user_id=actor_user_id,
                note=(note or "").strip() or None,
            )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="request_sales_invoice_approval",
            resource_id=approval.id,
            correlation_id=correlation_id,
            detail={"invoice_id": str(invoice_id), "content_hash": approval.content_hash},
        )
        return approval

    async def approve(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> InvoiceApproval:
        """Approve the pending request - if it is still about the draft as it is now."""
        await self._require(APPROVE_INVOICE, actor_user_id, administration_id)
        pending = await self._pending_or_refuse(administration_id, invoice_id)
        view = await self._invoices.view(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=actor_user_id,
        )
        if pending.content_hash != content_fingerprint(view.invoice):
            raise ApprovalStale("the draft changed after approval was requested")
        decided = await self._repository.decide(
            administration_id=administration_id,
            approval_id=pending.id,
            status="approved",
            user_id=actor_user_id,
            reason=None,
        )
        if decided is None:
            raise ApprovalAlreadyDecided("the request was decided by another request")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="approve_sales_invoice",
            resource_id=decided.id,
            correlation_id=correlation_id,
            detail={"invoice_id": str(invoice_id), "content_hash": decided.content_hash},
        )
        return decided

    async def reject(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str,
        correlation_id: str | None = None,
    ) -> InvoiceApproval:
        """Send the draft back, with the reason the drafter needs to fix it."""
        await self._require(APPROVE_INVOICE, actor_user_id, administration_id)
        reason = (reason or "").strip()
        if not reason:
            raise ApprovalReasonMissing("a rejection needs a reason")
        pending = await self._pending_or_refuse(administration_id, invoice_id)
        decided = await self._repository.decide(
            administration_id=administration_id,
            approval_id=pending.id,
            status="rejected",
            user_id=actor_user_id,
            reason=reason,
        )
        if decided is None:
            raise ApprovalAlreadyDecided("the request was decided by another request")
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="reject_sales_invoice",
            resource_id=decided.id,
            correlation_id=correlation_id,
            # That it was rejected, not the reason: free text stays in the record.
            detail={"invoice_id": str(invoice_id)},
        )
        return decided

    async def status(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> ApprovalStatus:
        await self._require(REQUEST_APPROVAL, actor_user_id, administration_id)
        view = await self._invoices.view(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=actor_user_id,
        )
        required = await self._repository.approval_required(administration_id=administration_id)
        latest = await self._repository.latest_for_invoice(
            administration_id=administration_id, invoice_id=invoice_id
        )
        state = state_of(latest, content_fingerprint(view.invoice))
        is_approver = await _holds(
            self._authorization, APPROVE_INVOICE, actor_user_id, administration_id
        )
        can_issue = view.can_be_issued and (
            not required or is_approver or state is ApprovalState.APPROVED
        )
        return ApprovalStatus(required=required, state=state, approval=latest, can_issue=can_issue)

    async def queue(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        status: str = "pending",
        limit: int = 50,
    ) -> Sequence[QueueEntry]:
        """What is waiting for the owner. Approvers only: it names other people's drafts."""
        await self._require(APPROVE_INVOICE, actor_user_id, administration_id)
        return await self._repository.queue(
            administration_id=administration_id, status=status, limit=limit
        )

    # -- internals ----------------------------------------
    async def _pending_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> InvoiceApproval:
        pending = await self._repository.pending_for_invoice(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if pending is None:
            raise ApprovalNotFound(f"no pending approval request for invoice {invoice_id}")
        return pending

    async def _require(
        self, permission: tuple[str, str], user_id: uuid.UUID, administration_id: uuid.UUID
    ) -> None:
        if not await _holds(self._authorization, permission, user_id, administration_id):
            action, resource_type = permission
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={},
            )
            raise NotAuthorizedToInvoice(action, resource_type, "not permitted")

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
                resource_type="sales_invoice_approval",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )

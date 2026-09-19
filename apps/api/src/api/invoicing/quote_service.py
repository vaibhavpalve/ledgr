"""SI-08's orchestration: a quote's lifecycle, and its conversion into an invoice.

--- Converting is exactly what a person would have done ---

A conversion is `InvoicingService.create_draft` with the quote's customer and lines.
So the statutory gate, the gapless number (at ISSUE, not here), the posting and the
permission checks are the ones a person meets. The result is a DRAFT: a quote turning
into a numbered, posted invoice with no human looking at it is the bulk-click problem
again, and converting is not the moment to skip the review.

--- One quote, one invoice, even under a race ---

The draft and the `converted` mark are written in ONE savepoint, and the mark is a
conditional UPDATE (`WHERE status = 'accepted'`). Two conversions racing both create a
draft; the second finds the quote no longer `accepted`, its savepoint rolls its draft
back, and it answers with the FIRST invoice. So a retried request returns the same
invoice - idempotent (NFR-032) - and the customer is never invoiced twice.

--- An accepted quote is what it was quoted ---

Lines are copied exactly: quantity, unit price, discount, VAT treatment. The price is
NOT re-quoted or indexed. The VAT RATE is the one in force on the invoice date
(CMP-014), which may differ from the quote's date; that is why a quote shows a net
total and never a VAT figure.

--- Who may ---

`create sales_invoice` throughout (ADR-012): a quote is a pre-invoice commercial
document and the same people who draft invoices draft quotes. Nothing here issues,
sends or posts anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.customers.model import (
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerNotFound,
)
from api.invoicing.model import (
    CustomerDetailsConflict,
    CustomerDetailsMissing,
    InvoiceNotFound,
    InvoicingError,
    NotAuthorizedToInvoice,
)
from api.invoicing.quotes import (
    QuoteInvalid,
    QuoteKind,
    QuoteLine,
    QuoteStatus,
    can_transition,
    is_expired,
    validate_quote,
)
from api.invoicing.service import InvoicingService, NewLine

__all__ = [
    "ConversionResult",
    "Quote",
    "QuoteConversionFailed",
    "QuoteDraft",
    "QuoteExpired",
    "QuoteNotEditable",
    "QuoteNotFound",
    "QuoteRepository",
    "QuoteService",
    "QuoteTransitionInvalid",
]

CREATE = ("create", "sales_invoice")


class QuoteNotFound(InvoicingError):
    """No such quote in this administration - indistinguishable from another tenant's."""


class QuoteTransitionInvalid(InvoicingError):
    def __init__(self, current: QuoteStatus, target: QuoteStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"a quote cannot go from {current.value} to {target.value}")


class QuoteExpired(InvoicingError):
    """The offer no longer stands; extend its validity to revive it."""


class QuoteNotEditable(InvoicingError):
    """Once a quote has gone to a customer it is what they were told."""

    def __init__(self, status: QuoteStatus) -> None:
        self.status = status
        super().__init__(f"a {status.value} quote cannot be edited")


class QuoteConversionFailed(InvoicingError):
    """The invoice could not be raised; `code` says why (the sentence is the catalogue's)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"conversion failed: {code}")


class _AlreadyConverted(Exception):
    """Raised INSIDE the conversion savepoint when another request got there first, to
    roll the losing draft back."""


@dataclass(frozen=True, slots=True)
class QuoteDraft:
    """What a person supplies to create or replace a quote."""

    kind: QuoteKind
    customer_id: uuid.UUID
    subject: str | None
    valid_until: date | None
    notes: str | None
    lines: tuple[QuoteLine, ...]


@dataclass(frozen=True, slots=True)
class Quote:
    id: uuid.UUID
    administration_id: uuid.UUID
    kind: QuoteKind
    quote_number: int
    reference: str
    customer_id: uuid.UUID
    subject: str | None
    valid_until: date | None
    notes: str | None
    status: QuoteStatus
    lines: tuple[QuoteLine, ...]
    sent_at: datetime | None = None
    accepted_at: datetime | None = None
    accepted_by_name: str | None = None
    acceptance_reference: str | None = None
    declined_at: datetime | None = None
    decline_reason: str | None = None
    cancelled_at: datetime | None = None
    converted_at: datetime | None = None
    converted_invoice_id: uuid.UUID | None = None

    def is_expired(self, today: date) -> bool:
        """Derived, never stored. Only a quote still OUT can expire: an accepted one
        was accepted in time, and a converted one is done."""
        return self.status in (QuoteStatus.DRAFT, QuoteStatus.SENT) and is_expired(
            self.valid_until, today
        )


@dataclass(frozen=True, slots=True)
class ConversionResult:
    quote: Quote
    invoice_id: uuid.UUID
    #: True when this request found the quote already converted and returned its
    #: invoice, rather than creating one.
    already_converted: bool


class QuoteRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    def savepoint(self) -> AbstractAsyncContextManager[None]: ...

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool: ...

    async def create(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, draft: QuoteDraft
    ) -> Quote: ...

    async def replace(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, draft: QuoteDraft
    ) -> Quote: ...

    async def get(self, *, administration_id: uuid.UUID, quote_id: uuid.UUID) -> Quote | None: ...

    async def list(
        self, *, administration_id: uuid.UUID, status: QuoteStatus | None
    ) -> Sequence[Quote]: ...

    async def transition(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        expected: frozenset[QuoteStatus],
        to: QuoteStatus,
        user_id: uuid.UUID,
        accepted_by_name: str | None = None,
        acceptance_reference: str | None = None,
        decline_reason: str | None = None,
    ) -> bool:
        """Move to `to` only if the quote is currently one of `expected`. False when it
        was not - somebody else moved it first."""
        ...

    async def extend_validity(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, valid_until: date
    ) -> None: ...

    async def fiscal_year_for(
        self, *, administration_id: uuid.UUID, on: date
    ) -> uuid.UUID | None: ...

    async def mark_converted(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> bool:
        """`converted` and the invoice link in ONE conditional statement
        (`WHERE status = 'accepted'`); False when the quote was not accepted."""
        ...


class QuoteService:
    def __init__(
        self,
        repository: QuoteRepository,
        invoicing: InvoicingService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._invoicing = invoicing
        self._authorization = authorization
        self._audit = audit_log

    # -- defining -----------------------------------------------------------------

    async def create(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        draft: QuoteDraft,
        today: date,
        correlation_id: str | None = None,
    ) -> Quote:
        await self._require(actor_user_id, administration_id)
        validate_quote(lines=draft.lines, valid_until=draft.valid_until, today=today)
        await self._customer_or_refuse(administration_id, draft.customer_id)
        created = await self._repository.create(
            administration_id=administration_id, user_id=actor_user_id, draft=draft
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_quote",
            resource_id=created.id,
            correlation_id=correlation_id,
            detail={"reference": created.reference, "kind": created.kind.value},
        )
        return created

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        draft: QuoteDraft,
        today: date,
        correlation_id: str | None = None,
    ) -> Quote:
        """Replace a DRAFT. Once a quote has gone to the customer it is what they were
        told; the way to change it is to cancel it and create a new one."""
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, quote_id)
        if current.status is not QuoteStatus.DRAFT:
            raise QuoteNotEditable(current.status)
        validate_quote(lines=draft.lines, valid_until=draft.valid_until, today=today)
        await self._customer_or_refuse(administration_id, draft.customer_id)
        # The KIND is the document's identity ('OF-' or 'OB-'); it does not change.
        replaced = await self._repository.replace(
            administration_id=administration_id,
            quote_id=quote_id,
            draft=QuoteDraft(
                kind=current.kind,
                customer_id=draft.customer_id,
                subject=draft.subject,
                valid_until=draft.valid_until,
                notes=draft.notes,
                lines=draft.lines,
            ),
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="edit_quote",
            resource_id=quote_id,
            correlation_id=correlation_id,
            detail={"reference": current.reference},
        )
        return replaced

    async def get(
        self, *, administration_id: uuid.UUID, quote_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Quote:
        await self._require(actor_user_id, administration_id)
        return await self._get_or_refuse(administration_id, quote_id)

    async def list(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        status: QuoteStatus | None = None,
    ) -> Sequence[Quote]:
        await self._require(actor_user_id, administration_id)
        return await self._repository.list(administration_id=administration_id, status=status)

    async def extend_validity(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        valid_until: date,
        today: date,
        correlation_id: str | None = None,
    ) -> Quote:
        """Push out the validity of a quote that is still OUT. Only ever later than
        the current date - it can only help the customer - and it revives an expired
        one."""
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, quote_id)
        if current.status not in (QuoteStatus.DRAFT, QuoteStatus.SENT):
            raise QuoteNotEditable(current.status)
        if valid_until < today or (
            current.valid_until is not None and valid_until <= current.valid_until
        ):
            raise QuoteInvalid("the new validity date must be later than the current one")
        await self._repository.extend_validity(
            administration_id=administration_id, quote_id=quote_id, valid_until=valid_until
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="extend_quote",
            resource_id=quote_id,
            correlation_id=correlation_id,
            detail={"valid_until": valid_until.isoformat()},
        )
        return await self._get_or_refuse(administration_id, quote_id)

    # -- lifecycle ------------------------------------------------------------------

    async def mark_sent(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> Quote:
        """Record that the quote has gone to the customer. This does not send it -
        there is no quote PDF or e-mail yet (ADR-075) - it says a person did."""
        return await self._move(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=actor_user_id,
            to=QuoteStatus.SENT,
            today=today,
            action="send_quote",
            check_expiry=True,
            correlation_id=correlation_id,
        )

    async def accept(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        accepted_by_name: str | None = None,
        acceptance_reference: str | None = None,
        correlation_id: str | None = None,
    ) -> Quote:
        """Record that the customer accepted. Refused once the offer has expired: an
        acceptance after `valid_until` is a new agreement, so extend the validity first
        if the business is honouring it."""
        return await self._move(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=actor_user_id,
            to=QuoteStatus.ACCEPTED,
            today=today,
            action="accept_quote",
            check_expiry=True,
            accepted_by_name=_clean(accepted_by_name),
            acceptance_reference=_clean(acceptance_reference),
            correlation_id=correlation_id,
        )

    async def decline(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Quote:
        return await self._move(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=actor_user_id,
            to=QuoteStatus.DECLINED,
            today=today,
            action="decline_quote",
            check_expiry=False,
            decline_reason=_clean(reason),
            correlation_id=correlation_id,
        )

    async def cancel(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> Quote:
        return await self._move(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=actor_user_id,
            to=QuoteStatus.CANCELLED,
            today=today,
            action="cancel_quote",
            check_expiry=False,
            correlation_id=correlation_id,
        )

    async def _move(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        to: QuoteStatus,
        today: date,
        action: str,
        check_expiry: bool,
        accepted_by_name: str | None = None,
        acceptance_reference: str | None = None,
        decline_reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Quote:
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, quote_id)
        if not can_transition(current.status, to):
            raise QuoteTransitionInvalid(current.status, to)
        if check_expiry and current.is_expired(today):
            raise QuoteExpired(f"{current.reference} was valid until {current.valid_until}")

        moved = await self._repository.transition(
            administration_id=administration_id,
            quote_id=quote_id,
            expected=frozenset({current.status}),
            to=to,
            user_id=actor_user_id,
            accepted_by_name=accepted_by_name,
            acceptance_reference=acceptance_reference,
            decline_reason=decline_reason,
        )
        if not moved:
            # Somebody else moved it between our read and our write.
            latest = await self._get_or_refuse(administration_id, quote_id)
            raise QuoteTransitionInvalid(latest.status, to)

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action=action,
            resource_id=quote_id,
            correlation_id=correlation_id,
            detail={
                "reference": current.reference,
                "from": current.status.value,
                # That they were given, not what they said: a customer's own words and
                # PO number are not something to accumulate under the audit retention.
                "acceptance_details_given": bool(accepted_by_name or acceptance_reference),
                "reason_given": bool(decline_reason),
            },
        )
        return await self._get_or_refuse(administration_id, quote_id)

    # -- converting -----------------------------------------------------------------

    async def convert(
        self,
        *,
        administration_id: uuid.UUID,
        quote_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        invoice_date: date | None = None,
        correlation_id: str | None = None,
    ) -> ConversionResult:
        """Turn an ACCEPTED quote into a DRAFT invoice with exactly its lines.

        Idempotent: converting a converted quote returns its invoice. `invoice_date`
        defaults to today.
        """
        await self._require(actor_user_id, administration_id)
        quote = await self._get_or_refuse(administration_id, quote_id)

        if quote.status is QuoteStatus.CONVERTED and quote.converted_invoice_id is not None:
            return ConversionResult(quote, quote.converted_invoice_id, already_converted=True)
        if quote.status is not QuoteStatus.ACCEPTED:
            raise QuoteTransitionInvalid(quote.status, QuoteStatus.CONVERTED)

        on = invoice_date or today
        fiscal_year = await self._repository.fiscal_year_for(
            administration_id=administration_id, on=on
        )
        if fiscal_year is None:
            raise QuoteConversionFailed("no_fiscal_year")

        lines = [
            NewLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,  # exactly as quoted: never re-priced
                vat_treatment=line.vat_treatment,
                discount_percent=line.discount_percent,
            )
            for line in quote.lines
        ]
        try:
            async with self._repository.savepoint():
                view = await self._invoicing.create_draft(
                    administration_id=administration_id,
                    fiscal_year_id=fiscal_year,
                    actor_user_id=actor_user_id,
                    invoice_date=on,
                    customer_id=quote.customer_id,
                    notes=quote.notes,
                    lines=lines,
                    correlation_id=correlation_id,
                )
                if not await self._repository.mark_converted(
                    administration_id=administration_id,
                    quote_id=quote_id,
                    invoice_id=view.invoice.id,
                ):
                    raise _AlreadyConverted
        except _AlreadyConverted:
            # Another request converted it first; the savepoint took our draft with it.
            latest = await self._get_or_refuse(administration_id, quote_id)
            if latest.converted_invoice_id is None:
                raise QuoteTransitionInvalid(latest.status, QuoteStatus.CONVERTED) from None
            return ConversionResult(latest, latest.converted_invoice_id, already_converted=True)
        except CustomerNotFound:
            raise QuoteConversionFailed("customer_not_found") from None
        except CustomerIsArchived:
            raise QuoteConversionFailed("customer_archived") from None
        except (CustomerAddressIncomplete, CustomerDetailsMissing, CustomerDetailsConflict):
            raise QuoteConversionFailed("customer_details_incomplete") from None

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="convert_quote",
            resource_id=quote_id,
            correlation_id=correlation_id,
            detail={
                "reference": quote.reference,
                "invoice_id": str(view.invoice.id),
                "lines": len(lines),
            },
        )
        converted = await self._get_or_refuse(administration_id, quote_id)
        return ConversionResult(converted, view.invoice.id, already_converted=False)

    # -- internals ------------------------------------------------------------------

    async def _get_or_refuse(self, administration_id: uuid.UUID, quote_id: uuid.UUID) -> Quote:
        found = await self._repository.get(administration_id=administration_id, quote_id=quote_id)
        if found is None:
            raise QuoteNotFound(f"quote {quote_id} not found")
        return found

    async def _customer_or_refuse(
        self, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise CustomerNotFound(f"customer {customer_id} not found")

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
                resource_type="sales_quote",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def _clean(value: str | None) -> str | None:
    """A customer-supplied free-text field: trimmed, and None when blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

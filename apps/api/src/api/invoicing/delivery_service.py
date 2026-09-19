"""Dispatching an invoice - FR-AR-005.

Read `api.invoicing.delivery` first: this module is the orchestration, and that
one is the seam the orchestration is written against.

--- What this file deliberately does not contain ---

There is no `if channel is DeliveryChannel.EMAIL` in it, and there is no
mention of an e-mail address. Every channel-specific decision is a call on the
adapter:

    which address       adapter.address_of(recipient)
    which artifacts     adapter.required_artifacts
    how to hand over    adapter.deliver(request)

That is the property FR-AR-005's "Peppol added in P2" needs, and
`tests/invoicing/test_delivery.py` proves it by dispatching over a channel this
package has never heard of.

--- The order, and what each step protects ---

    1. authorize                    `send sales_invoice`
    2. load the invoice; refuse a draft
    3. choose the channel           requested, else the customer's preference
    4. resolve the address          refuse if the customer is unreachable
    5. fetch the artifacts          refuse if one cannot be produced
    6. RECORD the dispatch          `queued`
    7. hand over
    8. record the outcome           `sent`, or `queued` with a retry time

Steps 1-5 all refuse before step 6 writes anything, so a refusal leaves no
half-dispatch behind. Step 6 precedes step 7 for the opposite reason: the row
has to exist BEFORE the irreversible act, or a crash between handing over and
recording would leave a customer holding an invoice this system has no record
of sending - and the next person to press send would send it again.

That ordering is the whole reason `queued` is a state rather than an
implementation detail.

--- A failed hand-over is not a failed request ---

NFR-026: an outage at the mail provider is not the caller's mistake and does
not fail their operation. `dispatch` returns the delivery record whatever
happened, and the caller reads `status`. The HTTP layer answers 200 with a
status rather than 502 - see `api.invoicing.routes.send_invoice`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.documents.service import DocumentService
from api.i18n.language import Language
from api.invoicing.delivery import (
    ArtifactKind,
    ArtifactNotAvailable,
    ChannelRegistry,
    DeliverableInvoice,
    DeliveryArtifact,
    DeliveryChannel,
    DeliveryRequest,
    DeliveryStatus,
    Recipient,
    ReminderNotice,
    UnreachableCustomer,
)
from api.invoicing.model import (
    InvoiceNotFound,
    InvoiceNotIssued,
    NotAuthorizedToInvoice,
    SalesInvoice,
)

__all__ = [
    "DeliveryRecord",
    "InvoiceDeliveryService",
    "DeliveryRepository",
    "InvoiceNotRendered",
    "RETRY_BACKOFF",
]

#: Appendix A's "Send sales invoices" - the same permission issuing takes.
#: Reused rather than invented (ADR-012), and it is the right one: sending is
#: the act FR-AR-005 names, and a role trusted to make a claim to somebody
#: outside the business is trusted to put it in the post.
SEND_INVOICE = ("send", "sales_invoice")

#: How long to wait before each transport retry. Explicit rather than
#: exponential-by-formula so the schedule is readable: a mail provider is
#: usually back within minutes, and an invoice that has not gone out within a
#: working day is something a person should look at rather than a queue should
#: keep quietly retrying.
RETRY_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=8),
)


class InvoiceNotRendered(Exception):
    """The invoice has no stored PDF.

    Only reachable for an invoice issued before migration 0040, which posted
    and rendered in the same transaction as issuing. Refused rather than
    rendered now: FR-TPL-017 says the stored rendering is what was issued, and
    producing one today for a document sent last year would be a different
    document wearing the same number.
    """


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """One row of `invoice_delivery` (migration 0041)."""

    id: uuid.UUID
    invoice_id: uuid.UUID
    channel: DeliveryChannel
    status: DeliveryStatus
    recipient: str
    language: Language
    attempts: int
    document_id: uuid.UUID | None = None
    provider: str | None = None
    provider_reference: str | None = None
    last_error: str | None = None
    requested_at: datetime | None = None
    sent_at: datetime | None = None
    settled_at: datetime | None = None
    next_attempt_at: datetime | None = None


class DeliveryRepository(Protocol):
    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice | None: ...

    async def recipient_for(
        self, *, administration_id: uuid.UUID, invoice: SalesInvoice
    ) -> Recipient:
        """Every way this invoice's customer can be addressed.

        Assembled from the invoice's frozen snapshot and, where the invoice
        does not carry the field, the customer master. A one-off customer
        (`customer_id is null`) yields a recipient with no addresses at all,
        which is a real state - the caller supplies one explicitly or gets an
        `UnreachableCustomer` naming the channel.
        """
        ...

    async def supplier_contact(self, *, administration_id: uuid.UUID) -> tuple[str, str | None]:
        """The administration's legal name and the address replies go to."""
        ...

    async def preferred_channel(
        self, *, administration_id: uuid.UUID, invoice: SalesInvoice
    ) -> DeliveryChannel | None: ...

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        channel: DeliveryChannel,
        recipient: str,
        language: Language,
        document_id: uuid.UUID | None,
        user_id: uuid.UUID,
    ) -> DeliveryRecord: ...

    async def record_outcome(
        self,
        *,
        administration_id: uuid.UUID,
        delivery_id: uuid.UUID,
        status: DeliveryStatus,
        provider: str | None,
        provider_reference: str | None,
        last_error: str | None,
        next_attempt_at: datetime | None,
    ) -> DeliveryRecord: ...

    async def state_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[DeliveryRecord]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class InvoiceDeliveryService:
    def __init__(
        self,
        repository: DeliveryRepository,
        registry: ChannelRegistry,
        documents: DocumentService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._documents = documents
        self._authorization = authorization
        self._audit = audit_log

    # -- FR-AR-005 ----------------------------------------------------------

    async def dispatch(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        channel: DeliveryChannel | None = None,
        recipient_override: str | None = None,
        correlation_id: str | None = None,
        custom_message: str | None = None,
        reminder: ReminderNotice | None = None,
    ) -> DeliveryRecord:
        """Send one invoice over one channel, and record what happened.

        `channel` defaults to the customer's stated preference (FR-AR-006), and
        falls back to e-mail when there is no customer master - which is the
        P0 channel and the only one a one-off customer can be reached on.

        `recipient_override` is the address to use instead of the customer's,
        interpreted by the channel: an e-mail address for e-mail, a participant
        id for Peppol. It exists for two ordinary needs - a one-off customer
        who has no master record to hold an address, and "send this copy to
        their bookkeeper" - and it is recorded on the dispatch, so where a
        document actually went is never inferred.

        `custom_message` (SI-01) is not persisted anywhere - it exists only
        for the one adapter call below - so a resend never silently reuses
        whatever somebody typed last time.

        Returns the record whatever happened. A provider outage leaves it
        `queued` with a retry time; it does not raise (NFR-026).
        """
        await self._require(actor_user_id, administration_id)

        invoice = await self._issued_or_refuse(administration_id, invoice_id)
        chosen = channel or await self._channel_for(administration_id, invoice)
        adapter = self._registry.adapter_for(chosen)

        recipient = await self._repository.recipient_for(
            administration_id=administration_id, invoice=invoice
        )
        # The override goes to the ADAPTER rather than being substituted here:
        # validating an address is channel-specific, and a service that swapped
        # the string blindly would hand a typo to the transport and report it
        # as a failed send rather than as a bad address.
        address = adapter.address_of(
            recipient, override=recipient_override.strip() if recipient_override else None
        )
        if address is None:
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="send_sales_invoice",
                resource_id=invoice_id,
                outcome=AuditOutcome.FAILURE,
                correlation_id=correlation_id,
                detail={"channel": chosen.value, "reason": "unreachable"},
            )
            raise UnreachableCustomer(
                chosen,
                f"this customer has no {chosen.value} address on file, and none was "
                f"supplied with the request (FR-AR-006)",
            )

        artifacts = await self._artifacts(
            invoice=invoice,
            kinds=adapter.required_artifacts,
            actor_user_id=actor_user_id,
        )

        organization_id = await self._organization_of(administration_id)
        # BEFORE the hand-over. A crash between sending and recording would
        # otherwise leave a customer holding an invoice with no record of it,
        # and the next person to press send would send it twice.
        record = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            invoice_id=invoice.id,
            channel=chosen,
            recipient=address,
            language=recipient.language,
            document_id=_document_id(artifacts),
            user_id=actor_user_id,
        )

        supplier_name, supplier_email = await self._repository.supplier_contact(
            administration_id=administration_id
        )
        outcome = await adapter.deliver(
            DeliveryRequest(
                invoice=_deliverable(invoice, supplier_name, supplier_email),
                recipient=recipient,
                address=address,
                artifacts=artifacts,
                custom_message=custom_message,
                reminder=reminder,
            )
        )

        settled = await self._repository.record_outcome(
            administration_id=administration_id,
            delivery_id=record.id,
            status=(
                DeliveryStatus.SENT
                if outcome.accepted
                else DeliveryStatus.QUEUED
                if outcome.retryable
                else DeliveryStatus.FAILED
            ),
            provider=outcome.provider,
            provider_reference=outcome.reference,
            last_error=None if outcome.accepted else outcome.detail,
            next_attempt_at=(
                _next_attempt(record.attempts)
                if not outcome.accepted and outcome.retryable
                else None
            ),
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="send_sales_invoice",
            resource_id=invoice.id,
            correlation_id=correlation_id,
            outcome=AuditOutcome.SUCCESS if outcome.accepted else AuditOutcome.FAILURE,
            detail={
                "delivery_id": str(settled.id),
                "channel": chosen.value,
                "status": settled.status.value,
                # WHERE it went. IAM-090 asks who did what to which record, and
                # for a dispatch the address is the "what": an invoice sent to
                # the wrong address is the incident, and the audit log is where
                # it is reconstructed.
                "recipient": address,
                "language": recipient.language.value,
                "provider": outcome.provider,
                "provider_reference": outcome.reference,
                "document_id": (str(settled.document_id) if settled.document_id else None),
                # Operator-facing; never rendered to a user (FR-UX-007).
                "detail": outcome.detail,
                # SI-01: whether a sender's own note went out with this
                # dispatch. The CONTENT is never audited - it is not
                # persisted anywhere at all (see dispatch's own docstring) -
                # only the fact that one was included.
                # SI-04: which reminder step this dispatch was, if it was one.
                "reminder_kind": reminder.kind if reminder else None,
                "custom_message_included": custom_message is not None
                and custom_message.strip() != "",
            },
        )
        return settled

    async def state_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[DeliveryRecord]:
        """FR-AR-005's "tracked per channel": the latest dispatch per channel."""
        await self._require(actor_user_id, administration_id)
        return await self._repository.state_of(
            administration_id=administration_id, invoice_id=invoice_id
        )

    @property
    def channels(self) -> frozenset[DeliveryChannel]:
        """What this deployment can send over, so a screen can grey out the
        rest rather than offering Peppol in P0 and failing.
        """
        return self._registry.channels

    # -- internals ----------------------------------------------------------

    async def _artifacts(
        self,
        *,
        invoice: SalesInvoice,
        kinds: frozenset[ArtifactKind],
        actor_user_id: uuid.UUID,
    ) -> dict[ArtifactKind, DeliveryArtifact]:
        """Exactly what the channel asked for, and nothing else.

        The loop is the extension point: a channel needing UBL costs this
        method one branch and the service nothing, because the SET comes from
        the adapter rather than from here.
        """
        artifacts: dict[ArtifactKind, DeliveryArtifact] = {}

        for kind in sorted(kinds, key=lambda k: k.value):
            if kind is ArtifactKind.PDF:
                artifacts[kind] = await self._stored_pdf(invoice, actor_user_id)
            else:
                # P2's UBL. A clear refusal rather than an empty attachment:
                # an e-invoice with no payload is worse than one never sent.
                raise ArtifactNotAvailable(kind)

        return artifacts

    async def _stored_pdf(
        self, invoice: SalesInvoice, actor_user_id: uuid.UUID
    ) -> DeliveryArtifact:
        """FR-TPL-017's stored rendering, read back from the archive.

        Read, never re-rendered. `DocumentService.original` verifies the hash on
        the way out, so what is attached is provably the bytes stored when the
        invoice was issued - which is the whole content of that requirement,
        and the reason sending does not go near the renderer.
        """
        if invoice.document_id is None:
            raise InvoiceNotRendered(
                f"invoice {invoice.invoice_reference} has no stored PDF. Invoices "
                f"issued before migration 0040 were not rendered at issue, and "
                f"rendering one now would be a different document under the same "
                f"number (FR-TPL-017)."
            )

        document, content = await self._documents.original(
            administration_id=invoice.administration_id,
            document_id=invoice.document_id,
            actor_user_id=actor_user_id,
        )
        return DeliveryArtifact(
            kind=ArtifactKind.PDF,
            filename=f"{invoice.invoice_reference or invoice.id}.pdf",
            content=content,
            content_type=document.content_type.value,
            document_id=document.id,
        )

    async def _channel_for(
        self, administration_id: uuid.UUID, invoice: SalesInvoice
    ) -> DeliveryChannel:
        """The customer's stated preference, or e-mail.

        A preference this deployment cannot honour falls back rather than
        refusing: a customer who asked for Peppol before P2 ships should still
        get their invoice, and FR-AR-005 makes e-mail the P0 channel. The
        dispatch records what actually happened, so the fallback is visible
        rather than silent.
        """
        preferred = await self._repository.preferred_channel(
            administration_id=administration_id, invoice=invoice
        )
        if preferred is not None and self._registry.supports(preferred):
            return preferred
        return DeliveryChannel.EMAIL

    async def _issued_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice:
        invoice = await self._repository.invoice(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if invoice is None:
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        if invoice.is_draft:
            raise InvoiceNotIssued(
                f"invoice {invoice_id} is a draft and carries no number; issue it "
                f"before sending it (FR-AR-004, FR-AR-005)"
            )
        return invoice

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise InvoiceNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = SEND_INVOICE
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
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                # IAM-090. Sending an invoice is the moment a claim leaves the
                # business, and CMP-009's "we never sent that" is exactly the
                # question these entries answer.
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="sales_invoice",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def _document_id(artifacts: dict[ArtifactKind, DeliveryArtifact]) -> uuid.UUID | None:
    for artifact in artifacts.values():
        if artifact.document_id is not None:
            return artifact.document_id
    return None


def _deliverable(
    invoice: SalesInvoice, supplier_name: str, supplier_email: str | None
) -> DeliverableInvoice:
    return DeliverableInvoice(
        id=invoice.id,
        reference=invoice.invoice_reference or str(invoice.id),
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        gross=abs(_gross_of(invoice)),
        is_credit_note=invoice.is_credit_note,
        supplier_name=supplier_name,
        supplier_email=supplier_email,
    )


def _gross_of(invoice: SalesInvoice) -> Any:
    """The invoice total, from its own lines.

    Summed from `line_net` rather than recomputed through the VAT engine: the
    covering e-mail states an amount as a courtesy, and the authoritative
    figure is on the PDF beside it. Pulling `InvoiceView` in here would make
    sending depend on the VAT ruleset being resolvable, which is a reason to
    fail a send that has nothing to do with sending.
    """
    from decimal import Decimal

    return sum((line.line_net for line in invoice.lines), Decimal("0.00"))


def _next_attempt(attempts: int) -> datetime | None:
    """When to try again, or None once the schedule is exhausted.

    None means the queue stops offering it - `RETRY_BACKOFF` runs out at about
    a working day, past which an invoice that has not gone out is something a
    person should look at rather than a queue should keep retrying quietly.
    """
    if attempts >= len(RETRY_BACKOFF):
        return None
    return datetime.now(UTC) + RETRY_BACKOFF[attempts]

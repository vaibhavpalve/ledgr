"""SI-04's orchestration: facts in, one decision, one email, one record.

The RULES are `api.invoicing.dunning` and are pure. This module fetches the facts
they need (what is outstanding, what has been sent, which rates are loaded), asks
them what to do, and - only when they say a reminder may go - sends it through the
existing delivery path and records it.

--- A reminder is a dispatch, not a second email system ---

It goes through `InvoiceDeliveryService.dispatch` with a `ReminderNotice`, so it
inherits everything FR-AR-005 already guarantees: the stored PDF is attached (never
re-rendered), the address is the customer's, the recipient's language is used
(FR-LOC-003), the dispatch is a row with a status and an audit entry, and the
channel seam (non-negotiable #4) is respected - there is no `if channel is EMAIL`
here.

--- Only an ACCEPTED dispatch is a reminder sent ---

`dispatch` returns a record whatever happened: a provider outage leaves it `queued`
with a retry time. Nothing drains that queue (0041's worker is not built), so a
queued reminder would never go out, and recording it as sent would stop the step
ever being retried. So a `dunning_reminder` row is written only for a dispatch the
provider ACCEPTED; anything else is reported and leaves the step due.

--- Who may do what ---

Reading the overview and assessments needs `create sales_invoice`, as viewing an
invoice does. Sending a reminder, pausing a customer and changing the ladder all
need `send sales_invoice` (ADR-012): each is a communication to, or a decision about
how hard to press, somebody outside the business - the act that permission names.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.invoicing.delivery import (
    ArtifactNotAvailable,
    ChannelNotAvailable,
    DeliveryStatus,
    ReminderNotice,
    UnreachableCustomer,
)
from api.invoicing.delivery_service import (
    DeliveryRecord,
    InvoiceDeliveryService,
    InvoiceNotRendered,
)
from api.invoicing.dunning import (
    DEFAULT_LADDER,
    DunningAssessment,
    InterestRate,
    InterestRateKind,
    LadderStep,
    StepKind,
    assess,
    validate_ladder,
)
from api.invoicing.model import InvoiceNotFound, InvoicingError, NotAuthorizedToInvoice

__all__ = [
    "MAX_CHASE_PER_CALL",
    "ChaseFailure",
    "ChaseItem",
    "ChaseReport",
    "ChaseResult",
    "ChaseSkip",
    "ChaseStatus",
    "DunningInvoice",
    "DunningRepository",
    "DunningService",
    "NothingToSend",
    "ReminderNotDelivered",
    "ReminderAlreadySent",
    "CustomerNotFoundForPause",
    "SentReminder",
]

VIEW = ("create", "sales_invoice")
SEND = ("send", "sales_invoice")


class NothingToSend(InvoicingError):
    """The rules say no reminder is due, and `assessment.blocker` says why."""

    def __init__(self, assessment: DunningAssessment) -> None:
        self.assessment = assessment
        super().__init__(f"nothing to send: {assessment.blocker}")


class ReminderNotDelivered(InvoicingError):
    """The provider did not accept it. The step stays due; nothing was recorded."""

    def __init__(self, delivery: DeliveryRecord) -> None:
        self.delivery = delivery
        super().__init__(f"the reminder was not accepted: {delivery.status.value}")


class ReminderAlreadySent(InvoicingError):
    """Two requests raced to send the same step; the database let one record it."""


class CustomerNotFoundForPause(InvoicingError):
    pass


@dataclass(frozen=True, slots=True)
class DunningInvoice:
    """One overdue-or-not invoice with the facts the rules need."""

    invoice_id: uuid.UUID
    invoice_reference: str | None
    invoice_date: date
    due_date: date | None
    customer_id: uuid.UUID | None
    customer_name: str
    outstanding: Decimal
    is_business: bool
    is_paused: bool
    sent_positions: frozenset[int]
    #: The day the most recent reminder about this invoice went out, if any.
    last_sent_on: date | None = None


@dataclass(frozen=True, slots=True)
class SentReminder:
    assessment: DunningAssessment
    delivery: DeliveryRecord


#: The most reminders one chase call sends. Sending is sequential (one SMTP hand-over
#: each), so an unbounded call would run past any request timeout with half the
#: customers mailed and no report. The rest are `deferred`; call again - anyone
#: already reminded is `too_soon`, so it is safe.
MAX_CHASE_PER_CALL = 50


class ChaseStatus(enum.Enum):
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class ChaseSkip(enum.Enum):
    """Why an invoice was not attempted."""

    #: The rules say nothing is due (see the assessment's blocker).
    BLOCKED = "blocked"
    #: A formal notice, in a "chase everything" call: it claims money and needs a
    #: person to have chosen it.
    NEEDS_CONFIRMATION = "needs_confirmation"
    #: The step the person reviewed is no longer the next one.
    STEP_CHANGED = "step_changed"
    #: Named, but not overdue in this administration (or not there at all).
    NOT_OVERDUE_OR_UNKNOWN = "not_overdue_or_unknown"
    #: Over `MAX_CHASE_PER_CALL`; call again.
    DEFERRED = "deferred"


class ChaseFailure(enum.Enum):
    """Why an attempted reminder did not go."""

    UNREACHABLE = "unreachable"
    NOT_RENDERED = "not_rendered"
    NOT_DELIVERED = "not_delivered"
    ALREADY_SENT = "already_sent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ChaseItem:
    """One invoice a person chose to chase, and the step they SAW."""

    invoice_id: uuid.UUID
    step_position: int


@dataclass(frozen=True, slots=True)
class ChaseResult:
    invoice_id: uuid.UUID
    status: ChaseStatus
    invoice: DunningInvoice | None = None
    assessment: DunningAssessment | None = None
    skip: ChaseSkip | None = None
    failure: ChaseFailure | None = None
    delivery: DeliveryRecord | None = None


@dataclass(frozen=True, slots=True)
class ChaseReport:
    results: tuple[ChaseResult, ...]

    @property
    def sent(self) -> int:
        return sum(1 for r in self.results if r.status is ChaseStatus.SENT)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status is ChaseStatus.FAILED)

    @property
    def deferred(self) -> int:
        return sum(1 for r in self.results if r.skip is ChaseSkip.DEFERRED)

    @property
    def skipped(self) -> int:
        """Skipped for any reason EXCEPT being deferred, which is its own count."""
        return sum(
            1
            for r in self.results
            if r.status is ChaseStatus.SKIPPED and r.skip is not ChaseSkip.DEFERRED
        )


def _skipped(invoice: DunningInvoice, assessment: DunningAssessment, why: ChaseSkip) -> ChaseResult:
    return ChaseResult(
        invoice_id=invoice.invoice_id,
        status=ChaseStatus.SKIPPED,
        invoice=invoice,
        assessment=assessment,
        skip=why,
    )


class DunningRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    def savepoint(self) -> AbstractAsyncContextManager[None]:
        """A SAVEPOINT around one unit of work: an error inside rolls back that
        unit only, not the request's whole transaction."""
        ...

    async def ladder(self, *, administration_id: uuid.UUID) -> tuple[LadderStep, ...] | None:
        """The configured ladder, or None when the administration never chose one
        (which means the default) - distinct from an empty one (chase nobody)."""
        ...

    async def replace_ladder(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, steps: Sequence[LadderStep]
    ) -> None: ...

    async def rates(self, *, kind: InterestRateKind) -> Sequence[InterestRate]: ...

    async def overdue(
        self, *, administration_id: uuid.UUID, today: date
    ) -> Sequence[DunningInvoice]: ...

    async def facts_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> DunningInvoice | None: ...

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool: ...

    async def pause(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> None: ...

    async def resume(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> bool: ...

    async def record_reminder(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        assessment: DunningAssessment,
        delivery_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None: ...


class DunningService:
    def __init__(
        self,
        repository: DunningRepository,
        delivery: InvoiceDeliveryService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._delivery = delivery
        self._authorization = authorization
        self._audit = audit_log

    # -- the ladder -------------------------------------------------------------

    async def ladder(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> tuple[tuple[LadderStep, ...], bool]:
        """The ladder in force and whether it is the built-in default."""
        await self._require(VIEW, actor_user_id, administration_id)
        return await self._ladder(administration_id)

    async def set_ladder(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        steps: Sequence[LadderStep],
        correlation_id: str | None = None,
    ) -> tuple[LadderStep, ...]:
        """Replace the whole ladder. Validated first (`LadderInvalid`), so a ladder
        that would claim what the law does not allow is refused when configured."""
        await self._require(SEND, actor_user_id, administration_id)
        ordered = validate_ladder(steps)
        await self._repository.replace_ladder(
            administration_id=administration_id, user_id=actor_user_id, steps=ordered
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="configure_dunning_ladder",
            resource_type="dunning_ladder",
            resource_id=administration_id,
            correlation_id=correlation_id,
            detail={
                "steps": [
                    {
                        "position": s.position,
                        "days_after_due": s.days_after_due,
                        "kind": s.kind.value,
                        "charge_interest": s.charge_interest,
                        "charge_collection_cost": s.charge_collection_cost,
                    }
                    for s in ordered
                ]
            },
        )
        return ordered

    # -- reading ------------------------------------------------------------------

    async def overview(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, today: date
    ) -> Sequence[tuple[DunningInvoice, DunningAssessment]]:
        """Every overdue invoice with what should happen to it today."""
        await self._require(VIEW, actor_user_id, administration_id)
        steps, _ = await self._ladder(administration_id)
        invoices = await self._repository.overdue(administration_id=administration_id, today=today)
        rates = await self._rates_for(invoices, steps)
        return [(inv, self._assess(inv, steps, rates, today)) for inv in invoices]

    async def assessment(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
    ) -> tuple[DunningInvoice, DunningAssessment]:
        await self._require(VIEW, actor_user_id, administration_id)
        return await self._assessed(administration_id, invoice_id, today)

    # -- sending ------------------------------------------------------------------

    async def send_next(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> SentReminder:
        """Send the next reminder step for one invoice, if the rules allow it.

        Raises `NothingToSend` (with the reason) when they do not, and
        `ReminderNotDelivered` when the provider did not accept the message - in
        which case nothing is recorded and the step is still due. The failed
        dispatch and its audit entry ARE written, so a route that catches this
        (rather than letting it roll the transaction back) keeps them.
        """
        await self._require(SEND, actor_user_id, administration_id)
        _, assessment = await self._assessed(administration_id, invoice_id, today)
        if not assessment.can_send or assessment.step is None:
            raise NothingToSend(assessment)

        delivery, sent = await self._dispatch_and_record(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=actor_user_id,
            assessment=assessment,
            correlation_id=correlation_id,
        )
        if not sent:
            raise ReminderNotDelivered(delivery)
        return SentReminder(assessment=assessment, delivery=delivery)

    async def _dispatch_and_record(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        assessment: DunningAssessment,
        correlation_id: str | None,
    ) -> tuple[DeliveryRecord, bool]:
        """Dispatch one assessed reminder and record what happened.

        Returns the delivery and whether the provider ACCEPTED it. Only an accepted
        one is recorded as a reminder sent (see the module docstring). A refusal is
        RETURNED, not raised: a bulk chase must carry on to the next customer, and
        a single send must be able to commit the failed dispatch's record.
        """
        step = assessment.step
        assert step is not None  # only called for a sendable assessment
        delivery = await self._delivery.dispatch(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
            reminder=ReminderNotice(
                kind=step.kind.value,
                outstanding=assessment.outstanding,
                days_overdue=assessment.days_overdue,
                interest=assessment.interest,
                collection_cost=assessment.collection_cost,
                pay_by=assessment.pay_by,
            ),
        )
        if delivery.status is not DeliveryStatus.SENT:
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="send_dunning_reminder",
                resource_type="dunning_reminder",
                resource_id=invoice_id,
                outcome=AuditOutcome.FAILURE,
                correlation_id=correlation_id,
                detail={"step": step.position, "delivery_status": delivery.status.value},
            )
            return delivery, False

        await self._repository.record_reminder(
            administration_id=administration_id,
            invoice_id=invoice_id,
            assessment=assessment,
            delivery_id=delivery.id,
            user_id=actor_user_id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="send_dunning_reminder",
            resource_type="dunning_reminder",
            resource_id=invoice_id,
            correlation_id=correlation_id,
            detail={
                "step": step.position,
                "kind": step.kind.value,
                "delivery_id": str(delivery.id),
                "outstanding": str(assessment.outstanding),
                "interest": str(assessment.interest) if assessment.interest is not None else None,
                "collection_cost": (
                    str(assessment.collection_cost)
                    if assessment.collection_cost is not None
                    else None
                ),
                "days_overdue": assessment.days_overdue,
            },
        )
        return delivery, True

    # -- SI-11: chase everything overdue -----------------------------------------------

    async def chase(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        items: Sequence[ChaseItem] | None = None,
        correlation_id: str | None = None,
    ) -> ChaseReport:
        """Send the next reminder step to every overdue invoice that is due one.

        Two modes, and the difference is the safety design:

        * `items is None` - "chase everything". Sends the friendly and ordinary
          reminder steps to whatever is due one. A FORMAL NOTICE is never sent this
          way: it claims interest and collection cost, and a bulk click must not be
          able to demand money from fifty customers nobody looked at. Those are
          reported as `needs_confirmation`.
        * `items` given - exactly those invoices, each with the step position the
          person REVIEWED. An item whose step has since changed (a preview that went
          stale, a retried request) is skipped as `step_changed`, never sent as
          whatever the step has become. Naming a formal notice here IS the explicit
          confirmation.

        Every invoice is re-assessed at send time, so the answer is current whichever
        mode. One customer's failure (no e-mail address, a provider refusal) does not
        stop the rest: each runs in its own savepoint and is reported. At most
        `MAX_CHASE_PER_CALL` are sent per call, because sending is sequential; the
        rest are `deferred`, and calling again is safe - anyone already reminded is
        `too_soon`.
        """
        await self._require(SEND, actor_user_id, administration_id)
        steps, _ = await self._ladder(administration_id)
        invoices = await self._repository.overdue(administration_id=administration_id, today=today)
        rates = await self._rates_for(invoices, steps)
        assessed = {
            invoice.invoice_id: (invoice, self._assess(invoice, steps, rates, today))
            for invoice in invoices
        }

        results: list[ChaseResult] = []
        candidates: list[tuple[DunningInvoice, DunningAssessment]] = []

        if items is None:
            for invoice, assessment in assessed.values():
                if not assessment.can_send or assessment.step is None:
                    results.append(_skipped(invoice, assessment, ChaseSkip.BLOCKED))
                elif assessment.step.kind is StepKind.FORMAL_NOTICE:
                    results.append(_skipped(invoice, assessment, ChaseSkip.NEEDS_CONFIRMATION))
                else:
                    candidates.append((invoice, assessment))
        else:
            seen: set[uuid.UUID] = set()
            for item in items:
                if item.invoice_id in seen:
                    continue
                seen.add(item.invoice_id)
                found = assessed.get(item.invoice_id)
                if found is None:
                    results.append(
                        ChaseResult(
                            invoice_id=item.invoice_id,
                            status=ChaseStatus.SKIPPED,
                            skip=ChaseSkip.NOT_OVERDUE_OR_UNKNOWN,
                        )
                    )
                    continue
                invoice, assessment = found
                # A step that exists but is not the one reviewed is step_changed even
                # when it is also blocked - that is the more useful thing to tell the
                # person. No step at all (paused, paid, ...) is simply blocked.
                if assessment.step is not None and assessment.step.position != item.step_position:
                    results.append(_skipped(invoice, assessment, ChaseSkip.STEP_CHANGED))
                elif not assessment.can_send:
                    results.append(_skipped(invoice, assessment, ChaseSkip.BLOCKED))
                else:
                    candidates.append((invoice, assessment))

        for invoice, assessment in candidates[MAX_CHASE_PER_CALL:]:
            results.append(_skipped(invoice, assessment, ChaseSkip.DEFERRED))

        for invoice, assessment in candidates[:MAX_CHASE_PER_CALL]:
            results.append(
                await self._chase_one(
                    invoice=invoice,
                    assessment=assessment,
                    administration_id=administration_id,
                    actor_user_id=actor_user_id,
                    correlation_id=correlation_id,
                )
            )

        report = ChaseReport(results=tuple(results))
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="chase_overdue",
            resource_type="dunning_reminder",
            resource_id=administration_id,
            correlation_id=correlation_id,
            detail={
                "sent": report.sent,
                "skipped": report.skipped,
                "failed": report.failed,
                "deferred": report.deferred,
                # Whether a person chose the invoices, or asked for everything.
                "explicit_selection": items is not None,
            },
        )
        return report

    async def _chase_one(
        self,
        *,
        invoice: DunningInvoice,
        assessment: DunningAssessment,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None,
    ) -> ChaseResult:
        """One invoice, in its own SAVEPOINT, reported rather than raised."""
        failure: ChaseFailure | None = None
        try:
            async with self._repository.savepoint():
                delivery, sent = await self._dispatch_and_record(
                    administration_id=administration_id,
                    invoice_id=invoice.invoice_id,
                    actor_user_id=actor_user_id,
                    assessment=assessment,
                    correlation_id=correlation_id,
                )
        except ReminderAlreadySent:
            failure = ChaseFailure.ALREADY_SENT
        except UnreachableCustomer:
            failure = ChaseFailure.UNREACHABLE
        except InvoiceNotRendered:
            failure = ChaseFailure.NOT_RENDERED
        except (ChannelNotAvailable, ArtifactNotAvailable):
            failure = ChaseFailure.UNAVAILABLE
        else:
            if sent:
                return ChaseResult(
                    invoice_id=invoice.invoice_id,
                    status=ChaseStatus.SENT,
                    invoice=invoice,
                    assessment=assessment,
                    delivery=delivery,
                )
            return ChaseResult(
                invoice_id=invoice.invoice_id,
                status=ChaseStatus.FAILED,
                invoice=invoice,
                assessment=assessment,
                failure=ChaseFailure.NOT_DELIVERED,
                delivery=delivery,
            )

        # The savepoint rolled this invoice's writes back, including any audit the
        # dispatch made - so the failure is recorded HERE, outside it, or a customer
        # who may or may not have been e-mailed (the already-sent race) would leave
        # no trace at all.
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="send_dunning_reminder",
            resource_type="dunning_reminder",
            resource_id=invoice.invoice_id,
            outcome=AuditOutcome.FAILURE,
            correlation_id=correlation_id,
            detail={
                "step": assessment.step.position if assessment.step else None,
                "reason": failure.value,
            },
        )
        return ChaseResult(
            invoice_id=invoice.invoice_id,
            status=ChaseStatus.FAILED,
            invoice=invoice,
            assessment=assessment,
            failure=failure,
        )

    # -- pause ----------------------------------------------------------------------

    async def pause(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Stop chasing a customer's invoices. Idempotent: pausing a paused
        customer is not an error and changes nothing."""
        await self._require(SEND, actor_user_id, administration_id)
        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise CustomerNotFoundForPause(f"customer {customer_id} not found")
        await self._repository.pause(
            administration_id=administration_id,
            customer_id=customer_id,
            user_id=actor_user_id,
            reason=reason.strip() if reason and reason.strip() else None,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="pause_dunning",
            resource_type="customer",
            resource_id=customer_id,
            correlation_id=correlation_id,
            detail={"reason_given": bool(reason and reason.strip())},
        )

    async def resume(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> None:
        """Resume chasing where the ladder stood: steps already sent stay sent."""
        await self._require(SEND, actor_user_id, administration_id)
        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise CustomerNotFoundForPause(f"customer {customer_id} not found")
        await self._repository.resume(administration_id=administration_id, customer_id=customer_id)
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="resume_dunning",
            resource_type="customer",
            resource_id=customer_id,
            correlation_id=correlation_id,
            detail={},
        )

    # -- internals ----------------------------------------------------------------------

    async def _ladder(self, administration_id: uuid.UUID) -> tuple[tuple[LadderStep, ...], bool]:
        configured = await self._repository.ladder(administration_id=administration_id)
        if configured is None:
            return DEFAULT_LADDER, True
        return configured, False

    async def _assessed(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID, today: date
    ) -> tuple[DunningInvoice, DunningAssessment]:
        facts = await self._repository.facts_for(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if facts is None:
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        steps, _ = await self._ladder(administration_id)
        rates = await self._rates_for([facts], steps)
        return facts, self._assess(facts, steps, rates, today)

    async def _rates_for(
        self, invoices: Sequence[DunningInvoice], steps: Sequence[LadderStep]
    ) -> dict[InterestRateKind, Sequence[InterestRate]]:
        """Only the rate kinds that will actually be needed, and none at all when
        no step charges interest - so an administration that does not claim
        interest never depends on the rate table."""
        if not any(step.charge_interest for step in steps) or not invoices:
            return {}
        kinds = {
            InterestRateKind.COMMERCIAL if inv.is_business else InterestRateKind.CONSUMER
            for inv in invoices
        }
        return {kind: await self._repository.rates(kind=kind) for kind in kinds}

    @staticmethod
    def _assess(
        invoice: DunningInvoice,
        steps: Sequence[LadderStep],
        rates: dict[InterestRateKind, Sequence[InterestRate]],
        today: date,
    ) -> DunningAssessment:
        kind = InterestRateKind.COMMERCIAL if invoice.is_business else InterestRateKind.CONSUMER
        return assess(
            invoice_id=invoice.invoice_id,
            outstanding=invoice.outstanding,
            due_date=invoice.due_date,
            today=today,
            steps=steps,
            sent_positions=invoice.sent_positions,
            paused=invoice.is_paused,
            is_business=invoice.is_business,
            rates=rates.get(kind, ()),
            last_sent_on=invoice.last_sent_on,
        )

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
                resource_type="sales_invoice",
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
            raise InvoiceNotFound(f"administration {administration_id} does not exist")
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

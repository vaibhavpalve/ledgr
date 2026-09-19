"""SI-07's orchestration: schedules in, invoices out - `api.invoicing.recurrence`.

--- A run is exactly what a person would have done ---

Generating an invoice is `InvoicingService.create_draft`, and - only when the
schedule asks - `InvoicingService.issue`. So FR-AR-003's statutory gate, FR-AR-004's
gapless numbering, FR-GL-006's posting and the permission checks are all the ones a
person meets, and a schedule cannot bypass any of them. Nothing here writes an
invoice, a posting or a number.

--- SENDING is never automatic ---

A run creates a draft or issues an invoice. It never e-mails one: a customer's first
sight of a bill is a decision (`send`), not a side effect of a date arriving.

--- Each run is one savepoint, and a failed ISSUE keeps the draft ---

The draft, its run row and the schedule's advance are written together, so a race
that loses on `unique (schedule, run_date)` rolls the losing draft back with it and
the customer is billed once. Issuing is attempted in a NESTED savepoint: if it fails
(the supplier's KvK number is missing, the period is locked) only the issue rolls
back - its number allocation with it, so the gapless series is untouched - and the
invoice stays a DRAFT, recorded with the reason, for a person to finish.

--- A failure stops that schedule, not the others ---

If a run fails (an archived customer, no fiscal year for the date), that schedule's
LATER dates are not attempted: generating March's invoice before February's would put
them out of order in a numbered series. Other schedules carry on. The failure is
remembered on the schedule as a code, and the next call retries the same date.

--- Who runs it ---

`create sales_invoice`, as drafting. A schedule with `auto_issue` also needs `send
sales_invoice` from whoever runs it; without it the invoice stays a draft
(`not_authorized_to_issue`), not a refusal. There is no scheduler yet, so a person (or
a future scheduled job acting for a named user) calls it; the ledger posting must have
an actor (FR-GL-004) and "the system" is not one that has been designed here.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.customers.model import (
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerNotFound,
)
from api.invoicing.issue_attempt import attempt_issue
from api.invoicing.model import (
    CustomerDetailsConflict,
    CustomerDetailsMissing,
    InvoiceNotFound,
    InvoicingError,
    NotAuthorizedToInvoice,
)
from api.invoicing.recurrence import (
    RecurringLine,
    Schedule,
    due_runs,
    indexed_price,
    next_run,
    validate_definition,
)
from api.invoicing.service import InvoicingService, NewLine

__all__ = [
    "MAX_RUNS_PER_CALL_TOTAL",
    "RecurringDefinition",
    "RecurringInvoice",
    "RecurringInvoiceService",
    "RecurringRepository",
    "RecurringNotFound",
    "ScheduleLocked",
    "RunAlreadyGenerated",
    "RunOutcome",
    "RunStatus",
    "ScheduleStatus",
]

CREATE = ("create", "sales_invoice")

#: Across ALL schedules in one call. Each run is an invoice, a posting and possibly a
#: number, so an unbounded call could generate hundreds in one request. The rest are
#: picked up by the next call, oldest first.
MAX_RUNS_PER_CALL_TOTAL = 100


class ScheduleStatus(enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class RunStatus(enum.Enum):
    GENERATED = "generated"
    #: Somebody else generated this date first; nothing was billed twice.
    ALREADY_GENERATED = "already_generated"
    FAILED = "failed"


class RecurringNotFound(InvoicingError):
    """No such schedule in this administration - indistinguishable from another
    tenant's, as `InvoiceNotFound` is."""


class ScheduleLocked(InvoicingError):
    """The rhythm of a schedule that has already billed cannot change.

    Dates are derived from the start date and interval (see `recurrence`), so moving
    either after invoices exist would re-date the whole history. Change the lines,
    the price, the end date; to change the rhythm, end this schedule and start a new
    one.
    """

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{field} cannot change once a schedule has generated an invoice")


class RunAlreadyGenerated(InvoicingError):
    """`unique (schedule, run_date)` caught a second generation of one date."""


@dataclass(frozen=True, slots=True)
class RecurringDefinition:
    """What a person supplies to create or replace a schedule."""

    customer_id: uuid.UUID
    name: str
    interval_months: int
    start_date: date
    end_date: date | None
    max_runs: int | None
    due_days: int | None
    indexation_percent: Decimal
    auto_issue: bool
    notes: str | None
    lines: tuple[RecurringLine, ...]

    @property
    def schedule(self) -> Schedule:
        return Schedule(
            start_date=self.start_date,
            interval_months=self.interval_months,
            end_date=self.end_date,
            max_runs=self.max_runs,
            indexation_percent=self.indexation_percent,
        )


@dataclass(frozen=True, slots=True)
class RecurringInvoice:
    id: uuid.UUID
    administration_id: uuid.UUID
    definition: RecurringDefinition
    status: ScheduleStatus
    runs_generated: int
    next_run_on: date | None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    schedule_id: uuid.UUID
    schedule_name: str
    run_date: date
    status: RunStatus
    invoice_id: uuid.UUID | None = None
    issued: bool = False
    #: Why the invoice was left a draft although the schedule asked to issue it.
    issue_error: str | None = None
    #: Why the run failed, as a code (the sentence is the catalogue's).
    error: str | None = None


class RecurringRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    def savepoint(self) -> AbstractAsyncContextManager[None]: ...

    async def create(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        definition: RecurringDefinition,
        next_run_on: date | None,
    ) -> RecurringInvoice: ...

    async def replace(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        definition: RecurringDefinition,
        status: ScheduleStatus,
        next_run_on: date | None,
    ) -> RecurringInvoice: ...

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool: ...

    async def get(
        self, *, administration_id: uuid.UUID, recurring_id: uuid.UUID
    ) -> RecurringInvoice | None: ...

    async def list(self, *, administration_id: uuid.UUID) -> Sequence[RecurringInvoice]: ...

    async def due(self, *, administration_id: uuid.UUID, today: date) -> Sequence[RecurringInvoice]:
        """Active schedules whose next run is on or before `today`, oldest first."""
        ...

    async def set_status(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        status: ScheduleStatus,
        next_run_on: date | None,
    ) -> None: ...

    async def fiscal_year_for(
        self, *, administration_id: uuid.UUID, on: date
    ) -> uuid.UUID | None: ...

    async def record_run(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        run_date: date,
        invoice_id: uuid.UUID,
        issued: bool,
        issue_error: str | None,
    ) -> None:
        """Raises `RunAlreadyGenerated` when that date has been generated."""
        ...

    async def advance(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        runs_generated: int,
        next_run_on: date | None,
    ) -> None: ...

    async def set_error(
        self, *, administration_id: uuid.UUID, recurring_id: uuid.UUID, error: str | None
    ) -> None: ...


class RecurringInvoiceService:
    def __init__(
        self,
        repository: RecurringRepository,
        invoicing: InvoicingService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._invoicing = invoicing
        self._authorization = authorization
        self._audit = audit_log

    # -- definitions --------------------------------------------------------------

    async def create(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        definition: RecurringDefinition,
        correlation_id: str | None = None,
    ) -> RecurringInvoice:
        await self._require(actor_user_id, administration_id)
        validate_definition(
            schedule=definition.schedule, due_days=definition.due_days, lines=definition.lines
        )
        await self._customer_or_refuse(administration_id, definition.customer_id)
        created = await self._repository.create(
            administration_id=administration_id,
            user_id=actor_user_id,
            definition=definition,
            next_run_on=next_run(definition.schedule, 0),
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_recurring_invoice",
            resource_id=created.id,
            correlation_id=correlation_id,
            detail={"interval_months": definition.interval_months, "lines": len(definition.lines)},
        )
        return created

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        definition: RecurringDefinition,
        correlation_id: str | None = None,
    ) -> RecurringInvoice:
        """Replace the definition. Applies to FUTURE runs only - invoices already
        generated are invoices, and are not touched."""
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, recurring_id)
        validate_definition(
            schedule=definition.schedule, due_days=definition.due_days, lines=definition.lines
        )

        if current.runs_generated > 0:
            old = current.definition
            for name, before, after in (
                ("start_date", old.start_date, definition.start_date),
                ("interval_months", old.interval_months, definition.interval_months),
                ("customer_id", old.customer_id, definition.customer_id),
            ):
                if before != after:
                    raise ScheduleLocked(name)

        await self._customer_or_refuse(administration_id, definition.customer_id)
        upcoming = next_run(definition.schedule, current.runs_generated)
        # A schedule whose new end date has already passed is ended by the edit; one
        # that was ended and is extended is NOT revived - resume is a deliberate act.
        status = current.status
        if upcoming is None:
            status = ScheduleStatus.ENDED
        elif status is ScheduleStatus.ENDED:
            status = ScheduleStatus.PAUSED
        updated = await self._repository.replace(
            administration_id=administration_id,
            recurring_id=recurring_id,
            definition=definition,
            status=status,
            next_run_on=upcoming,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="edit_recurring_invoice",
            resource_id=recurring_id,
            correlation_id=correlation_id,
            detail={"lines": len(definition.lines)},
        )
        return updated

    async def get(
        self, *, administration_id: uuid.UUID, recurring_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> RecurringInvoice:
        await self._require(actor_user_id, administration_id)
        return await self._get_or_refuse(administration_id, recurring_id)

    async def list(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[RecurringInvoice]:
        await self._require(actor_user_id, administration_id)
        return await self._repository.list(administration_id=administration_id)

    async def pause(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> RecurringInvoice:
        """Stop generating. Idempotent; an ended schedule stays ended."""
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, recurring_id)
        if current.status is ScheduleStatus.ACTIVE:
            await self._repository.set_status(
                administration_id=administration_id,
                recurring_id=recurring_id,
                status=ScheduleStatus.PAUSED,
                next_run_on=current.next_run_on,
            )
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="pause_recurring_invoice",
                resource_id=recurring_id,
                correlation_id=correlation_id,
                detail={},
            )
        return await self._get_or_refuse(administration_id, recurring_id)

    async def resume(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> RecurringInvoice:
        """Start generating again, where the schedule stood.

        Runs that fell due while it was paused are NOT skipped: they become due and
        are generated, each dated in its own month. A pause postpones billing; it
        does not waive it. To waive them, end the schedule and start a new one.
        """
        await self._require(actor_user_id, administration_id)
        current = await self._get_or_refuse(administration_id, recurring_id)
        if current.status is ScheduleStatus.PAUSED:
            upcoming = next_run(current.definition.schedule, current.runs_generated)
            await self._repository.set_status(
                administration_id=administration_id,
                recurring_id=recurring_id,
                status=ScheduleStatus.ACTIVE if upcoming else ScheduleStatus.ENDED,
                next_run_on=upcoming,
            )
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="resume_recurring_invoice",
                resource_id=recurring_id,
                correlation_id=correlation_id,
                detail={},
            )
        return await self._get_or_refuse(administration_id, recurring_id)

    # -- generating ---------------------------------------------------------------

    async def run_due(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
        correlation_id: str | None = None,
    ) -> Sequence[RunOutcome]:
        """Generate every invoice that should exist by `today` and does not.

        Idempotent: calling it twice generates each date once. Oldest first, across
        schedules, at most `MAX_RUNS_PER_CALL_TOTAL`; a schedule whose run fails
        stops there (see the module docstring) and the rest carry on.
        """
        await self._require(actor_user_id, administration_id)
        outcomes: list[RunOutcome] = []
        budget = MAX_RUNS_PER_CALL_TOTAL

        for schedule in await self._repository.due(
            administration_id=administration_id, today=today
        ):
            for index, run_date in due_runs(
                schedule.definition.schedule, schedule.runs_generated, today
            ):
                if budget <= 0:
                    break
                budget -= 1
                outcome = await self._run_one(
                    administration_id=administration_id,
                    actor_user_id=actor_user_id,
                    schedule=schedule,
                    index=index,
                    run_date=run_date,
                    correlation_id=correlation_id,
                )
                outcomes.append(outcome)
                if outcome.status is RunStatus.FAILED:
                    break

        generated = sum(1 for o in outcomes if o.status is RunStatus.GENERATED)
        if outcomes:
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="run_recurring_invoices",
                resource_id=administration_id,
                correlation_id=correlation_id,
                detail={
                    "generated": generated,
                    "failed": sum(1 for o in outcomes if o.status is RunStatus.FAILED),
                    "issued": sum(1 for o in outcomes if o.issued),
                },
            )
        return outcomes

    async def _run_one(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        schedule: RecurringInvoice,
        index: int,
        run_date: date,
        correlation_id: str | None,
    ) -> RunOutcome:
        definition = schedule.definition
        base = RunOutcome(schedule.id, definition.name, run_date, RunStatus.FAILED)

        fiscal_year = await self._repository.fiscal_year_for(
            administration_id=administration_id, on=run_date
        )
        if fiscal_year is None:
            return await self._failed(administration_id, schedule, base, "no_fiscal_year")

        lines = [
            NewLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=indexed_price(
                    line.unit_price,
                    definition.indexation_percent,
                    definition.start_date,
                    run_date,
                ),
                vat_treatment=line.vat_treatment,
                discount_percent=line.discount_percent,
            )
            for line in definition.lines
        ]
        due_date = (
            None if definition.due_days is None else run_date + timedelta(days=definition.due_days)
        )
        following = next_run(definition.schedule, index + 1)

        invoice_id: uuid.UUID | None = None
        issued = False
        issue_error: str | None = None
        try:
            async with self._repository.savepoint():
                view = await self._invoicing.create_draft(
                    administration_id=administration_id,
                    fiscal_year_id=fiscal_year,
                    actor_user_id=actor_user_id,
                    invoice_date=run_date,
                    customer_id=definition.customer_id,
                    due_date=due_date,
                    notes=definition.notes,
                    lines=lines,
                    correlation_id=correlation_id,
                )
                invoice_id = view.invoice.id
                if definition.auto_issue:
                    issued, issue_error = await self._try_issue(
                        administration_id, actor_user_id, invoice_id
                    )
                await self._repository.record_run(
                    administration_id=administration_id,
                    recurring_id=schedule.id,
                    run_date=run_date,
                    invoice_id=invoice_id,
                    issued=issued,
                    issue_error=issue_error,
                )
                await self._repository.advance(
                    administration_id=administration_id,
                    recurring_id=schedule.id,
                    runs_generated=index + 1,
                    next_run_on=following,
                )
        except RunAlreadyGenerated:
            # Somebody generated this date first. The savepoint took our draft with
            # it, so nothing was billed twice; bring the counter up to date so this
            # date is not offered again.
            await self._repository.advance(
                administration_id=administration_id,
                recurring_id=schedule.id,
                runs_generated=index + 1,
                next_run_on=following,
            )
            return RunOutcome(schedule.id, definition.name, run_date, RunStatus.ALREADY_GENERATED)
        except CustomerNotFound:
            return await self._failed(administration_id, schedule, base, "customer_not_found")
        except CustomerIsArchived:
            return await self._failed(administration_id, schedule, base, "customer_archived")
        except (CustomerAddressIncomplete, CustomerDetailsMissing, CustomerDetailsConflict):
            return await self._failed(
                administration_id, schedule, base, "customer_details_incomplete"
            )

        await self._repository.set_error(
            administration_id=administration_id, recurring_id=schedule.id, error=None
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="generate_recurring_invoice",
            resource_id=schedule.id,
            correlation_id=correlation_id,
            detail={
                "invoice_id": str(invoice_id),
                "run_date": run_date.isoformat(),
                "run": index + 1,
                "issued": issued,
                "issue_error": issue_error,
            },
        )
        return RunOutcome(
            schedule.id,
            definition.name,
            run_date,
            RunStatus.GENERATED,
            invoice_id=invoice_id,
            issued=issued,
            issue_error=issue_error,
        )

    async def _try_issue(
        self, administration_id: uuid.UUID, actor_user_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> tuple[bool, str | None]:
        """Issue in a NESTED savepoint, so a refusal leaves the draft behind (and the
        gapless series untouched - the number allocation rolls back with it)."""
        return await attempt_issue(
            invoicing=self._invoicing,
            savepoint=self._repository.savepoint,
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            invoice_id=invoice_id,
        )

    async def _failed(
        self,
        administration_id: uuid.UUID,
        schedule: RecurringInvoice,
        base: RunOutcome,
        error: str,
    ) -> RunOutcome:
        """Remember why on the schedule, OUTSIDE the savepoint that was rolled back."""
        await self._repository.set_error(
            administration_id=administration_id, recurring_id=schedule.id, error=error
        )
        return RunOutcome(
            base.schedule_id, base.schedule_name, base.run_date, RunStatus.FAILED, error=error
        )

    # -- internals ------------------------------------------------------------------

    async def _customer_or_refuse(
        self, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        """A schedule bills a customer of THIS administration. Checked here so a
        stranger's id is a clean 404, not the database trigger's exception."""
        if not await self._repository.customer_exists(
            administration_id=administration_id, customer_id=customer_id
        ):
            raise CustomerNotFound(f"customer {customer_id} not found")

    async def _get_or_refuse(
        self, administration_id: uuid.UUID, recurring_id: uuid.UUID
    ) -> RecurringInvoice:
        found = await self._repository.get(
            administration_id=administration_id, recurring_id=recurring_id
        )
        if found is None:
            raise RecurringNotFound(f"recurring invoice {recurring_id} not found")
        return found

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
                resource_type="recurring_invoice",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )

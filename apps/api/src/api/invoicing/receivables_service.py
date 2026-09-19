"""SI-06's read side: authorize, fetch, hand to the pure rules - `api.invoicing.receivables`.

--- One permission, and it is Appendix A's ---

`view report` (Appendix A's "View reports", ADR-012): an ageing report and a
customer statement are reports. Not `create sales_invoice`, which would let an
Invoicer read every customer's debt and payment history for having drafted an
invoice; and no new permission is invented.

--- Reading is not audited on success ---

Like listing invoices, viewing a report changes nothing. A DENIED attempt is
audited (the usual `_require` path); FR-RPT-005's audit of *exports* is the export
feature's, which this does not build.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.invoicing.model import InvoiceNotFound, InvoicingError, NotAuthorizedToInvoice
from api.invoicing.receivables import (
    AgeingReport,
    OpenItem,
    Statement,
    StatementMovement,
    age_items,
    build_statement,
)

__all__ = [
    "CustomerStatement",
    "ReceivablesRepository",
    "ReceivablesService",
    "StatementCustomerNotFound",
    "InvalidStatementPeriod",
]

VIEW_REPORT = ("view", "report")


class StatementCustomerNotFound(InvoicingError):
    """No such customer in this administration. Indistinguishable from another
    tenant's, deliberately, as `InvoiceNotFound` is."""


class InvalidStatementPeriod(InvoicingError):
    """A statement cannot end before it starts."""


@dataclass(frozen=True, slots=True)
class CustomerStatement:
    customer_id: uuid.UUID
    customer_name: str
    statement: Statement


class ReceivablesRepository(Protocol):
    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def open_items(
        self, *, administration_id: uuid.UUID, as_of: date
    ) -> Sequence[OpenItem]: ...

    async def customer_name(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> str | None: ...

    async def movements(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Sequence[StatementMovement]: ...


class ReceivablesService:
    def __init__(
        self,
        repository: ReceivablesRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log

    async def ageing(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, as_of: date
    ) -> AgeingReport:
        """Every invoice that still owed money on `as_of`, bucketed by days past
        its due date and grouped by customer. Reproducible: the same date gives the
        same report next month (see migration 0054)."""
        await self._require(actor_user_id, administration_id)
        items = await self._repository.open_items(administration_id=administration_id, as_of=as_of)
        return age_items(items, as_of)

    async def statement(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> CustomerStatement:
        """One customer's account for a period: opening balance, every movement
        with a running balance, closing balance."""
        await self._require(actor_user_id, administration_id)
        if date_from > date_to:
            raise InvalidStatementPeriod("a statement period must not end before it starts")

        name = await self._repository.customer_name(
            administration_id=administration_id, customer_id=customer_id
        )
        if name is None:
            raise StatementCustomerNotFound(f"customer {customer_id} not found")

        movements = await self._repository.movements(
            administration_id=administration_id, customer_id=customer_id
        )
        return CustomerStatement(
            customer_id=customer_id,
            customer_name=name,
            statement=build_statement(movements, date_from, date_to),
        )

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = VIEW_REPORT
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
            await self._record_denied(
                administration_id=administration_id,
                user_id=user_id,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

    async def _record_denied(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, detail: dict[str, Any]
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
                action="view_report",
                resource_type="report",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                detail=detail,
            )
        )

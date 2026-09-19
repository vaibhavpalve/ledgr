"""SQL for recurring invoices - migration 0056.

Thin, like `api.invoicing.repository`. Which dates a schedule falls on, and what a
price is after indexation, are `api.invoicing.recurrence`'s and are never computed
here; this file stores what it is told and reads it back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.recurrence import RecurringLine
from api.invoicing.recurring_service import (
    RecurringDefinition,
    RecurringInvoice,
    RunAlreadyGenerated,
    ScheduleStatus,
)

_COLUMNS = """
    id, administration_id, customer_id, name, interval_months, start_date, end_date,
    max_runs, due_days, indexation_percent, auto_issue, notes, status, runs_generated,
    next_run_on, last_error
"""


def _line(row: Any) -> RecurringLine:
    return RecurringLine(
        description=row.description,
        quantity=Decimal(row.quantity),
        unit_price=Decimal(row.unit_price),
        vat_treatment=row.vat_treatment,
        discount_percent=Decimal(row.discount_percent),
    )


def _schedule(row: Any, lines: Sequence[RecurringLine]) -> RecurringInvoice:
    return RecurringInvoice(
        id=row.id,
        administration_id=row.administration_id,
        definition=RecurringDefinition(
            customer_id=row.customer_id,
            name=row.name,
            interval_months=row.interval_months,
            start_date=row.start_date,
            end_date=row.end_date,
            max_runs=row.max_runs,
            due_days=row.due_days,
            indexation_percent=Decimal(row.indexation_percent),
            auto_issue=row.auto_issue,
            notes=row.notes,
            lines=tuple(lines),
        ),
        status=ScheduleStatus(row.status),
        runs_generated=row.runs_generated,
        next_run_on=row.next_run_on,
        last_error=row.last_error,
    )


class SqlRecurringRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """A SAVEPOINT: an error inside rolls back that unit only. One run's draft,
        run row and advance are written in one, so a race that loses on the unique
        key takes its own draft with it."""
        async with self._session.begin_nested():
            yield

    # -- definitions ----------------------------------------------------------------

    async def create(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        definition: RecurringDefinition,
        next_run_on: date | None,
    ) -> RecurringInvoice:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO recurring_invoice (
                    organization_id, administration_id, customer_id, name, interval_months,
                    start_date, end_date, max_runs, due_days, indexation_percent,
                    auto_issue, notes, status, next_run_on, created_by_user_id
                ) VALUES (
                    -- overwritten by recurring_invoice_guard()
                    (SELECT organization_id FROM administration WHERE id = :admin),
                    :admin, :customer, :name, :interval, :start, :end, :max_runs, :due_days,
                    :indexation, :auto_issue, :notes,
                    CASE WHEN CAST(:next AS date) IS NULL THEN 'ended' ELSE 'active' END,
                    CAST(:next AS date), :user
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                **_definition_params(definition),
                "admin": str(administration_id),
                "user": str(user_id),
                "next": next_run_on,
            },
        )
        row = result.one()
        await self._insert_lines(administration_id, row.id, definition.lines)
        return await self._reread(administration_id, row.id)

    async def replace(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        definition: RecurringDefinition,
        status: ScheduleStatus,
        next_run_on: date | None,
    ) -> RecurringInvoice:
        """Replace wholesale, as an invoice's lines are: a schedule is edited as a
        block. Lines are DELETEd and re-INSERTed; nothing that has been generated
        refers to them (an invoice copies what it needs)."""
        result = await self._session.execute(
            text(
                f"""
                UPDATE recurring_invoice SET
                    customer_id = :customer, name = :name, interval_months = :interval,
                    start_date = :start, end_date = :end, max_runs = :max_runs,
                    due_days = :due_days, indexation_percent = :indexation,
                    auto_issue = :auto_issue, notes = :notes, status = :status,
                    next_run_on = CAST(:next AS date)
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_COLUMNS}
                """
            ),
            {
                **_definition_params(definition),
                "id": str(recurring_id),
                "admin": str(administration_id),
                "status": status.value,
                "next": next_run_on,
            },
        )
        row = result.one()
        await self._session.execute(
            text("DELETE FROM recurring_invoice_line WHERE recurring_invoice_id = :id"),
            {"id": str(recurring_id)},
        )
        await self._insert_lines(administration_id, recurring_id, definition.lines)
        return await self._reread(administration_id, row.id)

    async def _reread(
        self, administration_id: uuid.UUID, recurring_id: uuid.UUID
    ) -> RecurringInvoice:
        """What was WRITTEN, as the database stores it. Answering with the input would
        show `49.95` here and `49.9500` on the next read - a difference a client
        comparing the two would report as a bug, and would be right to."""
        found = await self.get(administration_id=administration_id, recurring_id=recurring_id)
        assert found is not None  # just written in this transaction
        return found

    async def _insert_lines(
        self, administration_id: uuid.UUID, recurring_id: uuid.UUID, lines: Sequence[RecurringLine]
    ) -> None:
        for position, line in enumerate(lines, start=1):
            await self._session.execute(
                text(
                    """
                    INSERT INTO recurring_invoice_line (
                        organization_id, administration_id, recurring_invoice_id, position,
                        description, quantity, unit_price, discount_percent, vat_treatment
                    ) VALUES (
                        -- overwritten by recurring_invoice_child_guard()
                        (SELECT organization_id FROM recurring_invoice WHERE id = :schedule),
                        :admin, :schedule, :position, :description, :quantity, :unit_price,
                        :discount, :treatment
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "schedule": str(recurring_id),
                    "position": position,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "discount": line.discount_percent,
                    "treatment": line.vat_treatment,
                },
            )

    # -- reading ----------------------------------------------------------------------

    async def _with_lines(self, rows: Sequence[Any]) -> list[RecurringInvoice]:
        if not rows:
            return []
        lines_by_schedule: dict[uuid.UUID, list[RecurringLine]] = {}
        result = await self._session.execute(
            text(
                "SELECT recurring_invoice_id, description, quantity, unit_price, "
                "       discount_percent, vat_treatment "
                "  FROM recurring_invoice_line "
                " WHERE recurring_invoice_id = ANY(CAST(:ids AS uuid[])) "
                " ORDER BY recurring_invoice_id, position"
            ),
            {"ids": [str(row.id) for row in rows]},
        )
        for line in result:
            lines_by_schedule.setdefault(line.recurring_invoice_id, []).append(_line(line))
        return [_schedule(row, lines_by_schedule.get(row.id, ())) for row in rows]

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM customer WHERE id = :id AND administration_id = :admin"),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    async def get(
        self, *, administration_id: uuid.UUID, recurring_id: uuid.UUID
    ) -> RecurringInvoice | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM recurring_invoice "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(recurring_id), "admin": str(administration_id)},
        )
        found = await self._with_lines(result.all())
        return found[0] if found else None

    async def list(self, *, administration_id: uuid.UUID) -> Sequence[RecurringInvoice]:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM recurring_invoice "
                "WHERE administration_id = :admin ORDER BY created_at DESC, name"
            ),
            {"admin": str(administration_id)},
        )
        return await self._with_lines(result.all())

    async def due(self, *, administration_id: uuid.UUID, today: date) -> Sequence[RecurringInvoice]:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM recurring_invoice "
                "WHERE administration_id = :admin AND status = 'active' "
                "  AND next_run_on <= :today "
                "ORDER BY next_run_on, created_at"
            ),
            {"admin": str(administration_id), "today": today},
        )
        return await self._with_lines(result.all())

    # -- state ----------------------------------------------------------------------------

    async def set_status(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        status: ScheduleStatus,
        next_run_on: date | None,
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE recurring_invoice SET status = :status, next_run_on = CAST(:next AS date) "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {
                "status": status.value,
                "next": next_run_on,
                "id": str(recurring_id),
                "admin": str(administration_id),
            },
        )

    async def fiscal_year_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        """The fiscal year containing `on`, preferring an OPEN one. A date in no
        fiscal year returns None, and the run fails with a code rather than
        inventing one."""
        result = await self._session.execute(
            text(
                "SELECT id FROM fiscal_year "
                " WHERE administration_id = :admin AND :on BETWEEN start_date AND end_date "
                " ORDER BY (status = 'open') DESC LIMIT 1"
            ),
            {"admin": str(administration_id), "on": on},
        )
        row = result.first()
        return None if row is None else row.id

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
        try:
            await self._session.execute(
                text(
                    """
                    INSERT INTO recurring_invoice_run (
                        organization_id, administration_id, recurring_invoice_id, run_date,
                        invoice_id, issued, issue_error
                    ) VALUES (
                        (SELECT organization_id FROM recurring_invoice WHERE id = :schedule),
                        :admin, :schedule, :run_date, :invoice, :issued, :issue_error
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "schedule": str(recurring_id),
                    "run_date": run_date,
                    "invoice": str(invoice_id),
                    "issued": issued,
                    "issue_error": issue_error,
                },
            )
        except IntegrityError as exc:
            if "once_per_date" in str(exc.orig):
                raise RunAlreadyGenerated(f"{run_date} was already generated") from exc
            raise

    async def advance(
        self,
        *,
        administration_id: uuid.UUID,
        recurring_id: uuid.UUID,
        runs_generated: int,
        next_run_on: date | None,
    ) -> None:
        # Only ever forward: a stale writer must not move the counter back and
        # re-offer a date that has been billed.
        await self._session.execute(
            text(
                "UPDATE recurring_invoice SET runs_generated = :runs, "
                "  next_run_on = CAST(:next AS date), "
                "  status = CASE WHEN CAST(:next AS date) IS NULL THEN 'ended' ELSE status END "
                " WHERE id = :id AND administration_id = :admin AND runs_generated < :runs"
            ),
            {
                "runs": runs_generated,
                "next": next_run_on,
                "id": str(recurring_id),
                "admin": str(administration_id),
            },
        )

    async def set_error(
        self, *, administration_id: uuid.UUID, recurring_id: uuid.UUID, error: str | None
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE recurring_invoice SET last_error = :error, "
                "  last_error_at = CASE WHEN CAST(:error AS text) IS NULL THEN NULL ELSE now() END "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {"error": error, "id": str(recurring_id), "admin": str(administration_id)},
        )


def _definition_params(definition: RecurringDefinition) -> dict[str, object]:
    return {
        "customer": str(definition.customer_id),
        "name": definition.name.strip(),
        "interval": definition.interval_months,
        "start": definition.start_date,
        "end": definition.end_date,
        "max_runs": definition.max_runs,
        "due_days": definition.due_days,
        "indexation": definition.indexation_percent,
        "auto_issue": definition.auto_issue,
        "notes": definition.notes,
    }

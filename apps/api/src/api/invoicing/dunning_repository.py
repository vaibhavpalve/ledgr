"""SQL for dunning - migration 0053.

Thin, like `api.invoicing.repository`. "Overdue" and "outstanding" are defined in
SQL once (`invoicing.overdue_invoices`, `invoicing.invoice_balances`) and read from
there; the rules that DECIDE live in `api.invoicing.dunning` and never in this file.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.invoicing.dunning import (
    DunningAssessment,
    InterestRate,
    InterestRateKind,
    LadderStep,
    StepKind,
)
from api.invoicing.dunning_service import DunningInvoice, ReminderAlreadySent


def _invoice(row: Any) -> DunningInvoice:
    return DunningInvoice(
        invoice_id=row.invoice_id,
        invoice_reference=row.invoice_reference,
        invoice_date=row.invoice_date,
        due_date=row.due_date,
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        outstanding=Decimal(row.outstanding),
        is_business=bool(row.is_business),
        is_paused=bool(row.is_paused),
        sent_positions=frozenset(row.sent_positions or ()),
    )


class SqlDunningRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def ladder(self, *, administration_id: uuid.UUID) -> tuple[LadderStep, ...] | None:
        configured = await self._session.execute(
            text("SELECT 1 FROM dunning_ladder_configured WHERE administration_id = :admin"),
            {"admin": str(administration_id)},
        )
        if configured.first() is None:
            return None
        result = await self._session.execute(
            text(
                "SELECT position, days_after_due, kind, charge_interest, charge_collection_cost "
                "  FROM dunning_step WHERE administration_id = :admin ORDER BY position"
            ),
            {"admin": str(administration_id)},
        )
        return tuple(
            LadderStep(
                position=row.position,
                days_after_due=row.days_after_due,
                kind=StepKind(row.kind),
                charge_interest=row.charge_interest,
                charge_collection_cost=row.charge_collection_cost,
            )
            for row in result
        )

    async def replace_ladder(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, steps: Sequence[LadderStep]
    ) -> None:
        """Replace wholesale, as `SqlInvoiceRepository.replace_lines` does: a ladder
        is edited as a block on one screen, and a diff would have to decide what a
        moved step is. Atomic in the request's transaction."""
        params = {"admin": str(administration_id), "user": str(user_id)}
        await self._session.execute(
            text("DELETE FROM dunning_step WHERE administration_id = :admin"), params
        )
        for step in steps:
            await self._session.execute(
                text(
                    """
                    INSERT INTO dunning_step (
                        organization_id, administration_id, position, days_after_due,
                        kind, charge_interest, charge_collection_cost
                    ) VALUES (
                        -- overwritten by dunning_derive_organization()
                        (SELECT organization_id FROM administration WHERE id = :admin),
                        :admin, :position, :days, :kind, :interest, :cost
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "position": step.position,
                    "days": step.days_after_due,
                    "kind": step.kind.value,
                    "interest": step.charge_interest,
                    "cost": step.charge_collection_cost,
                },
            )
        # Recorded even for an empty ladder: "chase nobody" is a choice, and this
        # row is what tells it apart from "never configured".
        await self._session.execute(
            text(
                """
                INSERT INTO dunning_ladder_configured (
                    administration_id, organization_id, configured_by_user_id
                ) VALUES (
                    :admin, (SELECT organization_id FROM administration WHERE id = :admin), :user
                )
                ON CONFLICT (administration_id) DO UPDATE
                   SET configured_by_user_id = :user, configured_at = now()
                """
            ),
            params,
        )

    async def rates(self, *, kind: InterestRateKind) -> Sequence[InterestRate]:
        result = await self._session.execute(
            text(
                "SELECT valid_from, rate FROM statutory_interest_rate "
                " WHERE kind = :kind ORDER BY valid_from"
            ),
            {"kind": kind.value},
        )
        return [InterestRate(valid_from=row.valid_from, rate=Decimal(row.rate)) for row in result]

    async def overdue(
        self, *, administration_id: uuid.UUID, today: date
    ) -> Sequence[DunningInvoice]:
        result = await self._session.execute(
            text(
                "SELECT invoice_id, invoice_reference, invoice_date, due_date, customer_id, "
                "       customer_name, outstanding, is_business, is_paused, sent_positions "
                "  FROM invoicing.overdue_invoices(:admin, :today)"
            ),
            {"admin": str(administration_id), "today": today},
        )
        return [_invoice(row) for row in result]

    async def facts_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> DunningInvoice | None:
        """One invoice's facts whether or not it is overdue - an assessment of a
        not-yet-due invoice is a real answer ("not overdue"), not a 404."""
        result = await self._session.execute(
            text(
                """
                SELECT b.invoice_id, b.invoice_reference, b.invoice_date, b.due_date,
                       b.customer_id, b.customer_name, b.outstanding,
                       (si.customer_vat_number IS NOT NULL
                        OR EXISTS (SELECT 1 FROM customer c
                                    WHERE c.id = b.customer_id AND c.kvk_number IS NOT NULL))
                           AS is_business,
                       EXISTS (SELECT 1 FROM dunning_pause p
                                WHERE p.customer_id = b.customer_id) AS is_paused,
                       COALESCE(ARRAY(SELECT r.step_position FROM dunning_reminder r
                                       WHERE r.invoice_id = b.invoice_id
                                       ORDER BY r.step_position), '{}'::integer[])
                           AS sent_positions
                  FROM invoicing.invoice_balances(:admin, :invoice) b
                  JOIN sales_invoice si ON si.id = b.invoice_id
                """
            ),
            {"admin": str(administration_id), "invoice": str(invoice_id)},
        )
        row = result.first()
        return None if row is None else _invoice(row)

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM customer WHERE id = :id AND administration_id = :admin"),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    async def pause(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str | None,
    ) -> None:
        # ON CONFLICT DO NOTHING: pausing a paused customer changes nothing - the
        # ORIGINAL pause (who, when, why) is the fact worth keeping.
        await self._session.execute(
            text(
                """
                INSERT INTO dunning_pause (
                    customer_id, organization_id, administration_id, paused_by_user_id, reason
                ) VALUES (
                    :customer, (SELECT organization_id FROM administration WHERE id = :admin),
                    :admin, :user, :reason
                )
                ON CONFLICT (customer_id) DO NOTHING
                """
            ),
            {
                "customer": str(customer_id),
                "admin": str(administration_id),
                "user": str(user_id),
                "reason": reason,
            },
        )

    async def resume(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text(
                "DELETE FROM dunning_pause WHERE customer_id = :customer "
                "AND administration_id = :admin RETURNING customer_id"
            ),
            {"customer": str(customer_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    async def record_reminder(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        assessment: DunningAssessment,
        delivery_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        step = assessment.step
        assert step is not None  # only called for a sendable assessment
        try:
            await self._session.execute(
                text(
                    """
                    INSERT INTO dunning_reminder (
                        organization_id, administration_id, invoice_id, step_position, kind,
                        delivery_id, outstanding_amount, interest_amount,
                        collection_cost_amount, pay_by, days_overdue, sent_by_user_id
                    ) VALUES (
                        (SELECT organization_id FROM administration WHERE id = :admin),
                        :admin, :invoice, :position, :kind, :delivery, :outstanding,
                        :interest, :cost, :pay_by, :days, :user
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "invoice": str(invoice_id),
                    "position": step.position,
                    "kind": step.kind.value,
                    "delivery": str(delivery_id),
                    "outstanding": assessment.outstanding,
                    "interest": assessment.interest,
                    "cost": assessment.collection_cost,
                    "pay_by": assessment.pay_by,
                    "days": assessment.days_overdue,
                    "user": str(user_id),
                },
            )
        except IntegrityError as exc:
            # `dunning_reminder_once_per_step`: two requests raced to send one step.
            if "once_per_step" in str(exc.orig):
                raise ReminderAlreadySent(
                    f"step {step.position} was already sent for invoice {invoice_id}"
                ) from exc
            raise

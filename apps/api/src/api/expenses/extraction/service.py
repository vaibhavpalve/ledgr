"""Reading a captured invoice into its expense - FR-EXP-001c, FR-AP-002.

--- This can fail in every way and the capture still succeeds ---

FR-EXP-001c: "the product never blocks on extraction being available." So every
failure of the reading - provider down, credentials missing, a timeout, an
answer that is not an invoice, a value that fails its checks - ends the same
way: the invoice is already captured and stored, the expense is a draft, and it
is filled in by hand. The one difference is a record of what happened, so the
review screen can say "we couldn't read this" instead of showing a form that
looks like nobody tried.

--- The reading goes through the form, like a person's typing ---

The fields are written with `ExpenseFormService.update`, the same path the form
uses, so the VAT rate and amounts are computed from the treatment and the DATE
(CMP-014) by the one implementation of that rule. This module derives no VAT: it
passes a treatment and lets the form work out the rest.

--- What is recorded ---

Which fields a machine wrote and how sure it was (`expense.extraction`), and an
audit entry naming the fields - never their values (the audit log has its own
retention, IAM-093, and is not a second copy of the claim).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.expenses.extraction.model import ExtractedInvoice, ExtractionError
from api.expenses.extraction.ports import READABLE_CONTENT_TYPES, InvoiceExtractor
from api.expenses.form import ExpenseFormService
from api.expenses.model import Expense, VatRateUnavailable, VatTreatment

#: Only the two treatments a single printed percentage identifies. 0% is not
#: here: it is `btw_0`, `btw_vrijgesteld`, `btw_verlegd` or an export, and the
#: invoice's rate cannot say which - so it is left for a person to choose.
_TREATMENT_BY_RATE = {Decimal("21"): VatTreatment.BTW_21, Decimal("9"): VatTreatment.BTW_9}


class ExtractionRepository(Protocol):
    async def expense_for_item(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID
    ) -> Expense | None: ...

    async def record_extraction(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, extraction: dict[str, Any]
    ) -> None: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class InvoiceExtractionService:
    def __init__(
        self,
        *,
        extractor: InvoiceExtractor | None,
        form: ExpenseFormService,
        repository: ExtractionRepository,
        audit_log: AuditLog,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._extractor = extractor
        self._form = form
        self._repository = repository
        self._audit = audit_log
        self._timeout = timeout_seconds

    async def read_into_expense(
        self,
        *,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        data: bytes,
        content_type: str,
        correlation_id: str | None = None,
    ) -> None:
        """Reads a newly captured invoice into its draft expense. Never raises
        for a failed reading."""
        if self._extractor is None:
            return
        expense = await self._repository.expense_for_item(
            administration_id=administration_id, item_id=item_id
        )
        if expense is None:
            return

        if content_type not in READABLE_CONTENT_TYPES:
            await self._record(
                expense, administration_id, actor_user_id, correlation_id,
                status="skipped", reason="unsupported_type",
            )  # fmt: skip
            return

        try:
            reading = await asyncio.wait_for(
                self._extractor.extract(data=data, content_type=content_type),
                timeout=self._timeout,
            )
        except ExtractionError as exc:
            await self._record(
                expense, administration_id, actor_user_id, correlation_id,
                status="failed", reason=exc.reason,
            )  # fmt: skip
            return
        except TimeoutError:
            await self._record(
                expense, administration_id, actor_user_id, correlation_id,
                status="failed", reason="timeout",
            )  # fmt: skip
            return
        except Exception as exc:  # noqa: BLE001 - a reading may never break a capture
            await self._record(
                expense, administration_id, actor_user_id, correlation_id,
                status="failed", reason="unexpected", detail=type(exc).__name__,
            )  # fmt: skip
            return

        if reading.is_empty:
            await self._record(
                expense, administration_id, actor_user_id, correlation_id,
                status="failed", reason="nothing_found",
            )  # fmt: skip
            return

        applied = await self._apply(
            expense, administration_id, actor_user_id, reading, correlation_id
        )
        await self._record(
            expense, administration_id, actor_user_id, correlation_id,
            status="done", reading=reading, applied=applied,
        )  # fmt: skip

    # -- internals ---------------------------------------------------------

    async def _apply(
        self,
        expense: Expense,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reading: ExtractedInvoice,
        correlation_id: str | None,
    ) -> frozenset[str]:
        """Writes the reading through the form. Returns the fields that landed."""
        fields: dict[str, Any] = {}
        if reading.supplier is not None:
            fields["supplier"] = reading.supplier
        if reading.invoice_number is not None:
            fields["invoice_number"] = reading.invoice_number
        if reading.invoice_date is not None:
            fields["expense_date"] = reading.invoice_date
        if reading.gross_amount is not None:
            fields["gross_amount"] = reading.gross_amount
        treatment = (
            _TREATMENT_BY_RATE.get(reading.vat_rate) if reading.vat_rate is not None else None
        )
        if treatment is not None:
            fields["vat_treatment"] = treatment

        if not fields:
            return frozenset()
        try:
            await self._form.update(
                administration_id=administration_id,
                expense_id=expense.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                **fields,
            )
        except VatRateUnavailable:
            # The ruleset has no rate for that treatment on that date - a date
            # the model misread, most likely. Keep everything else; a VAT
            # treatment with no rate is not something to write.
            fields.pop("vat_treatment", None)
            if not fields:
                return frozenset()
            await self._form.update(
                administration_id=administration_id,
                expense_id=expense.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                **fields,
            )
        return frozenset(_READING_FIELD_FOR.get(name, name) for name in fields)

    async def _record(
        self,
        expense: Expense,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None,
        *,
        status: str,
        reason: str | None = None,
        detail: str | None = None,
        reading: ExtractedInvoice | None = None,
        applied: frozenset[str] = frozenset(),
    ) -> None:
        assert self._extractor is not None
        confidence = {
            name: round(value, 2)
            for name, value in (reading.confidence if reading else {}).items()
            if name in applied
        }
        record: dict[str, Any] = {
            "status": status,
            "provider": self._extractor.provider,
            "model": self._extractor.model,
            "read_at": datetime.now(UTC).isoformat(),
            "fields": confidence,
        }
        if reason is not None:
            record["reason"] = reason
        await self._repository.record_extraction(
            administration_id=administration_id, expense_id=expense.id, extraction=record
        )

        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            return
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action="extract_invoice",
                resource_type="expense",
                resource_id=expense.id,
                outcome=AuditOutcome.SUCCESS if status == "done" else AuditOutcome.FAILURE,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                # Which fields and how it ended - never what they said.
                detail={
                    "status": status,
                    "provider": self._extractor.provider,
                    "model": self._extractor.model,
                    "fields": sorted(confidence),
                    **({"reason": reason} if reason else {}),
                    **({"error_type": detail} if detail else {}),
                },
            )
        )


#: The names a reading uses, for the form fields that carry them.
_READING_FIELD_FOR = {
    "expense_date": "invoice_date",
    "vat_treatment": "vat_rate",
}

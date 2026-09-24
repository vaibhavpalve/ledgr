"""The BTW return service: the overview, one period's return, the drill-down, and filing.

Composition only - every figure is `api.vat_returns.model`'s, every period transition is
`api.ledger.periods.PeriodService`'s (the ledger owns the period lifecycle; PRD §13 lists
VatReturn as "locks its period"), and the rules are `api.vat.rules.VatRulesService`'s.

--- Filing, in order ---

1. The period must be this administration's and not already filed.
2. The return is computed and its checks evaluated. A blocking check refuses. Every warning must
   be in the caller's `acknowledged` list - the filer saw it and chose to file anyway, and that
   choice is stored with the return (FR-VAT-002).
3. If the caller says which total it reviewed (`expected_total`) and the ledger now says
   something else, the filing is refused: somebody posted while the return was on screen.
4. The channel submits (today: records the manual reference).
5. `PeriodService.mark_filed` hard-locks the period - its own authorization check
   (`file vat_return`, scoped to the period, IAM-033) and its FILING audit entry.
6. The return is computed AGAIN, now that nothing can post into the period, and must equal the
   one from step 2. It is then stored as filed. Steps 5 and 6 are one transaction with the
   request, so a mismatch rolls the lock back too.

Step 6 narrows, but does not close, one window that belongs to the ledger rather than here: a
posting whose `journal_entry_validate()` read the period as open before step 5's UPDATE, and that
commits after this transaction, lands in a filed period. Closing it needs the ledger's validate
trigger to read the period row with a lock that conflicts with the status update; ADR-087 records
it as the ledger's to fix.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from api.ledger.periods import Period, PeriodService
from api.vat.rules import EffectiveRules, VatRulesService
from api.vat_returns.filing import VatFilingChannel
from api.vat_returns.model import (
    Box,
    BoxLine,
    PeriodFacts,
    VatReturn,
    box_lines_for,
    build_return,
)
from api.vat_returns.repository import FiledReturn, SqlVatReturnRepository


class VatReturnError(Exception):
    pass


class PeriodNotFound(VatReturnError):
    pass


class AlreadyFiled(VatReturnError):
    pass


class NotFileable(VatReturnError):
    def __init__(self, codes: Sequence[str]) -> None:
        super().__init__(", ".join(codes))
        self.codes = tuple(codes)


class WarningsNotAcknowledged(VatReturnError):
    def __init__(self, codes: Sequence[str]) -> None:
        super().__init__(", ".join(codes))
        self.codes = tuple(codes)


class FiguresChanged(VatReturnError):
    pass


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    period: Period
    due_date: date
    #: The live computation. None once the period is filed: the stored return is the answer.
    prepared: VatReturn | None
    filed: FiledReturn | None


def due_date(period_end: date) -> date:
    """The last day of the month after the period ends - `api.vat.deadlines`' rule, applied to a
    period whose dates are already known rather than to a date inside it."""
    year, month = (
        (period_end.year + 1, 1)
        if period_end.month == 12
        else (
            period_end.year,
            period_end.month + 1,
        )
    )
    return date(year, month, calendar.monthrange(year, month)[1])


def box_json(box: Box) -> dict[str, Any]:
    """The wire shape of one box - also what `vat_return.boxes` stores, so a filed return reads
    back in exactly the shape a prepared one is shown in."""

    def s(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "code": box.code,
        "kind": box.kind.value,
        "description_nl": box.description_nl,
        "description_en": box.description_en,
        "turnover": s(box.turnover),
        "turnover_rounded": s(box.turnover_rounded),
        "vat": s(box.vat),
        "vat_rounded": s(box.vat_rounded),
        "treatments": list(box.treatments),
    }


class VatReturnService:
    def __init__(
        self,
        repository: SqlVatReturnRepository,
        rules: VatRulesService,
        periods: PeriodService,
        channel: VatFilingChannel,
    ) -> None:
        self._repository = repository
        self._rules = rules
        self._periods = periods
        self._channel = channel
        self._rules_cache: dict[date, EffectiveRules] = {}

    async def _rules_for(self, period_end: date) -> EffectiveRules:
        if period_end not in self._rules_cache:
            self._rules_cache[period_end] = await self._rules.rules_for_period(end_date=period_end)
        return self._rules_cache[period_end]

    async def _period(self, administration_id: uuid.UUID, period_id: uuid.UUID) -> Period:
        period = await self._periods.period(period_id)
        if period is None or period.administration_id != administration_id:
            raise PeriodNotFound(str(period_id))
        return period

    async def _provisional(self) -> bool:
        return bool(await self._rules.provisional_rulesets())

    async def _facts(self, period: Period, *, today: date) -> PeriodFacts:
        administration_id = period.administration_id
        untagged = await self._repository.untagged_vat(
            administration_id=administration_id, period_id=period.id
        )
        earlier = await self._repository.earlier_unfiled(
            administration_id=administration_id, before=period.start_date
        )
        return PeriodFacts(
            today=today,
            unposted_purchases=await self._repository.unposted_purchases(
                administration_id=administration_id, start=period.start_date, end=period.end_date
            ),
            draft_sales_invoices=await self._repository.draft_sales_invoices(
                administration_id=administration_id, start=period.start_date, end=period.end_date
            ),
            unreconciled_bank=await self._repository.unreconciled_bank(
                administration_id=administration_id, start=period.start_date, end=period.end_date
            ),
            untagged_vat_lines=untagged.lines,
            untagged_vat_amount=untagged.amount,
            earlier_unfiled=tuple(
                f"{start.isoformat()}/{end.isoformat()}" for start, end in earlier
            ),
            ruleset_provisional=await self._provisional(),
        )

    async def _compute(self, period: Period, *, today: date) -> VatReturn:
        totals = await self._repository.totals_by_period(
            administration_id=period.administration_id, period_ids=[period.id]
        )
        return build_return(
            period_start=period.start_date,
            period_end=period.end_date,
            totals=totals.get(period.id, []),
            rules=await self._rules_for(period.end_date),
            facts=await self._facts(period, today=today),
        )

    # -- reads ------------------------------------------------------------

    async def overview(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID, today: date
    ) -> list[PeriodReturn]:
        """Every period of the year with its return. The per-period checks here are the cheap
        ones (period still running, unplaceable treatments, provisional rules); the full
        FR-VAT-002 set is evaluated when one period is opened."""
        periods = await self._periods.periods_for_year(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )
        filed = await self._repository.filed_returns(administration_id=administration_id)
        open_ids = [p.id for p in periods if p.id not in filed]
        totals = await self._repository.totals_by_period(
            administration_id=administration_id, period_ids=open_ids
        )
        provisional = await self._provisional()
        rows: list[PeriodReturn] = []
        for period in sorted(periods, key=lambda p: p.start_date):
            stored = filed.get(period.id)
            prepared = None
            if stored is None:
                prepared = build_return(
                    period_start=period.start_date,
                    period_end=period.end_date,
                    totals=totals.get(period.id, []),
                    rules=await self._rules_for(period.end_date),
                    facts=PeriodFacts(today=today, ruleset_provisional=provisional),
                )
            rows.append(
                PeriodReturn(
                    period=period,
                    due_date=due_date(period.end_date),
                    prepared=prepared,
                    filed=stored,
                )
            )
        return rows

    async def prepare(
        self, *, administration_id: uuid.UUID, period_id: uuid.UUID, today: date
    ) -> PeriodReturn:
        period = await self._period(administration_id, period_id)
        stored = await self._repository.filed_return(period_id=period_id)
        if stored is not None:
            return PeriodReturn(period, due_date(period.end_date), None, stored)
        return PeriodReturn(
            period, due_date(period.end_date), await self._compute(period, today=today), None
        )

    async def box_lines(
        self, *, administration_id: uuid.UUID, period_id: uuid.UUID, code: str
    ) -> tuple[BoxLine, ...]:
        """FR-VAT-011. Read from the ledger for a filed period too: a filed period is
        hard-locked, so its lines are the ones that were filed."""
        period = await self._period(administration_id, period_id)
        lines = await self._repository.lines_in_period(
            administration_id=administration_id, period_id=period_id
        )
        return box_lines_for(code, lines, await self._rules_for(period.end_date))

    # -- filing -----------------------------------------------------------

    async def file(
        self,
        *,
        administration_id: uuid.UUID,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        reference: str | None,
        acknowledged: Sequence[str],
        expected_total: Decimal | None,
        today: date,
    ) -> PeriodReturn:
        period = await self._period(administration_id, period_id)
        if period.is_hard_locked or await self._repository.filed_return(period_id=period_id):
            raise AlreadyFiled(str(period_id))

        prepared = await self._compute(period, today=today)
        if prepared.blocking:
            raise NotFileable([check.code.value for check in prepared.blocking])
        unacknowledged = [
            check.code.value for check in prepared.warnings if check.code.value not in acknowledged
        ]
        if unacknowledged:
            raise WarningsNotAcknowledged(unacknowledged)
        if expected_total is not None and expected_total != prepared.total_due:
            raise FiguresChanged(
                f"reviewed {expected_total}, the ledger now says {prepared.total_due}"
            )

        receipt = await self._channel.submit(prepared, reference=reference)
        filed_period = await self._periods.mark_filed(
            period_id=period_id, user_id=user_id, filing_reference=receipt.reference
        )

        after = await self._compute(filed_period, today=today)
        if [box_json(b) for b in after.boxes] != [box_json(b) for b in prepared.boxes]:
            raise FiguresChanged("a posting landed in the period while it was being filed")

        stored = await self._repository.record_filed(
            administration_id=administration_id,
            period_id=period_id,
            period_start=period.start_date,
            period_end=period.end_date,
            filing_channel=receipt.channel,
            filing_reference=receipt.reference,
            boxes=[box_json(box) for box in prepared.boxes],
            output_vat=prepared.output_vat,
            input_vat=prepared.input_vat,
            total_due=prepared.total_due,
            rules_fingerprint=prepared.rules_fingerprint,
            ruleset_provisional=any(
                check.code.value == "provisional_ruleset" for check in prepared.warnings
            ),
            warnings_acknowledged=[check.code.value for check in prepared.warnings],
            filed_by_user_id=user_id,
        )
        return PeriodReturn(filed_period, due_date(period.end_date), None, stored)

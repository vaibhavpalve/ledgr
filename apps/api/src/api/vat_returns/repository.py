"""The ledger reads the BTW return is built from, and the filed-return record (0068).

Reads only on the posting tables - CLAUDE.md non-negotiable #1 is about WRITES to
journal_entry/journal_line, the same distinction `api.reports.repository` draws. The one write is
to `vat_return`, which is this module's own table.

--- An account's role ---

`_ROLES` decides, per account, what it is for the return (`api.vat_returns.model.AccountRole`):
an output-VAT account is whatever the administration's sales configuration posts VAT to, or an
account carrying the RGS code for "te betalen omzetbelasting"; input VAT likewise from the
expense configuration or "te vorderen omzetbelasting". Revenue is the account type. Everything
else is OTHER. The configuration is read rather than guessed from a name, because a renamed
account keeps its role and an account called "BTW" that nothing posts VAT to is not one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.vat_returns.model import AccountRole, BoxLine, VatLineTotal, signed_amount

_ROLES = """
    roles AS (
        SELECT a.id,
               CASE
                   WHEN a.id IN (SELECT account_id FROM sales_posting_account
                                  WHERE administration_id = :admin AND purpose = 'vat_output')
                        OR a.rgs_code = 'BSchObe' THEN 'vat_output'
                   WHEN a.id IN (SELECT account_id FROM expense_posting_account
                                  WHERE administration_id = :admin AND purpose = 'vat_input')
                        OR a.rgs_code = 'BVorObe' THEN 'vat_input'
                   WHEN a.account_type = 'revenue' THEN 'revenue'
                   ELSE 'other'
               END AS role,
               a.code, a.name
          FROM ledger_account a
         WHERE a.administration_id = :admin
    )
"""


@dataclass(frozen=True, slots=True)
class FiledReturn:
    id: uuid.UUID
    period_id: uuid.UUID
    period_start: date
    period_end: date
    filing_channel: str
    filing_reference: str | None
    boxes: list[dict[str, Any]]
    output_vat: Decimal
    input_vat: Decimal
    total_due: Decimal
    rules_fingerprint: str
    ruleset_provisional: bool
    warnings_acknowledged: list[str]
    filed_by_user_id: uuid.UUID
    filed_at: datetime


@dataclass(frozen=True, slots=True)
class UntaggedVat:
    lines: int
    amount: Decimal


def _jsonb(value: object) -> list[Any]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    return list(value) if isinstance(value, list) else []


class SqlVatReturnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- the ledger --------------------------------------------------------

    async def totals_by_period(
        self, *, administration_id: uuid.UUID, period_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[VatLineTotal]]:
        """Every VAT-tagged line in the given periods, summed by treatment and account role."""
        if not period_ids:
            return {}
        result = await self._session.execute(
            text(
                f"WITH {_ROLES} "
                "SELECT e.period_id, l.vat_treatment, r.role, "
                "       COALESCE(SUM(l.debit), 0) AS debit, COALESCE(SUM(l.credit), 0) AS credit "
                "  FROM journal_line l "
                "  JOIN journal_entry e ON e.id = l.journal_entry_id "
                "  JOIN roles r ON r.id = l.account_id "
                " WHERE l.administration_id = :admin "
                "   AND e.period_id = ANY(CAST(:periods AS uuid[])) "
                "   AND l.vat_treatment IS NOT NULL "
                " GROUP BY e.period_id, l.vat_treatment, r.role"
            ),
            {"admin": str(administration_id), "periods": [str(p) for p in period_ids]},
        )
        totals: dict[uuid.UUID, list[VatLineTotal]] = {}
        for row in result:
            totals.setdefault(row.period_id, []).append(
                VatLineTotal(
                    vat_treatment=row.vat_treatment,
                    role=AccountRole(row.role),
                    debit=Decimal(row.debit),
                    credit=Decimal(row.credit),
                )
            )
        return totals

    async def lines_in_period(
        self, *, administration_id: uuid.UUID, period_id: uuid.UUID
    ) -> list[BoxLine]:
        """FR-VAT-011's raw material: each VAT-tagged line with its entry and account."""
        result = await self._session.execute(
            text(
                f"WITH {_ROLES} "
                "SELECT e.id AS entry_id, e.entry_number, e.entry_date, e.description, "
                "       e.document_reference, e.source_system, r.code, r.name, r.role, "
                "       l.vat_treatment, l.debit, l.credit "
                "  FROM journal_line l "
                "  JOIN journal_entry e ON e.id = l.journal_entry_id "
                "  JOIN roles r ON r.id = l.account_id "
                " WHERE l.administration_id = :admin AND e.period_id = :period "
                "   AND l.vat_treatment IS NOT NULL "
                " ORDER BY e.entry_date, e.entry_number, l.line_number"
            ),
            {"admin": str(administration_id), "period": str(period_id)},
        )
        lines: list[BoxLine] = []
        for row in result:
            role = AccountRole(row.role)
            lines.append(
                BoxLine(
                    entry_id=row.entry_id,
                    entry_number=int(row.entry_number),
                    entry_date=row.entry_date,
                    description=row.description,
                    document_reference=row.document_reference,
                    source_system=row.source_system,
                    account_code=row.code,
                    account_name=row.name,
                    vat_treatment=row.vat_treatment,
                    role=role,
                    amount=signed_amount(role, Decimal(row.debit), Decimal(row.credit)),
                    column="",
                )
            )
        return lines

    async def untagged_vat(
        self, *, administration_id: uuid.UUID, period_id: uuid.UUID
    ) -> UntaggedVat:
        """Lines on a VAT account that carry no treatment - VAT the return cannot place. A
        manual journal to 1700/1720 is the usual source (FR-VAT-002's "unbalanced VAT control
        accounts": the account moved and the return does not show it)."""
        result = await self._session.execute(
            text(
                f"WITH {_ROLES} "
                "SELECT COUNT(*) AS n, COALESCE(SUM(ABS(l.debit - l.credit)), 0) AS amount "
                "  FROM journal_line l "
                "  JOIN journal_entry e ON e.id = l.journal_entry_id "
                "  JOIN roles r ON r.id = l.account_id "
                " WHERE l.administration_id = :admin AND e.period_id = :period "
                "   AND l.vat_treatment IS NULL "
                "   AND r.role IN ('vat_output', 'vat_input')"
            ),
            {"admin": str(administration_id), "period": str(period_id)},
        )
        row = result.one()
        return UntaggedVat(lines=int(row.n), amount=Decimal(row.amount))

    # -- FR-VAT-002's other facts ---------------------------------------------

    async def unposted_purchases(
        self, *, administration_id: uuid.UUID, start: date, end: date
    ) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(*) FROM expense "
                " WHERE administration_id = :admin AND status IN ('draft', 'ready') "
                "   AND (expense_date IS NULL OR expense_date BETWEEN :start AND :end)"
            ),
            {"admin": str(administration_id), "start": start, "end": end},
        )
        return int(result.scalar_one())

    async def draft_sales_invoices(
        self, *, administration_id: uuid.UUID, start: date, end: date
    ) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(*) FROM sales_invoice "
                " WHERE administration_id = :admin AND status = 'draft' "
                "   AND invoice_date BETWEEN :start AND :end"
            ),
            {"admin": str(administration_id), "start": start, "end": end},
        )
        return int(result.scalar_one())

    async def unreconciled_bank(
        self, *, administration_id: uuid.UUID, start: date, end: date
    ) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(*) FROM bank_transaction "
                " WHERE administration_id = :admin AND status = 'unmatched' "
                "   AND booking_date BETWEEN :start AND :end"
            ),
            {"admin": str(administration_id), "start": start, "end": end},
        )
        return int(result.scalar_one())

    async def earlier_unfiled(
        self, *, administration_id: uuid.UUID, before: date
    ) -> list[tuple[date, date]]:
        """Earlier periods with VAT postings that are not filed. A period with no VAT activity
        is not reported - a business that started in June has nothing to file for January."""
        result = await self._session.execute(
            text(
                "SELECT p.start_date, p.end_date FROM period p "
                " WHERE p.administration_id = :admin AND p.end_date < :before "
                "   AND p.status <> 'vat_filed' "
                "   AND EXISTS (SELECT 1 FROM journal_entry e "
                "                 JOIN journal_line l ON l.journal_entry_id = e.id "
                "                WHERE e.period_id = p.id AND l.vat_treatment IS NOT NULL) "
                " ORDER BY p.start_date"
            ),
            {"admin": str(administration_id), "before": before},
        )
        return [(row.start_date, row.end_date) for row in result]

    # -- the filed return -------------------------------------------------

    async def filed_returns(self, *, administration_id: uuid.UUID) -> dict[uuid.UUID, FiledReturn]:
        result = await self._session.execute(
            text("SELECT * FROM vat_return WHERE administration_id = :admin"),
            {"admin": str(administration_id)},
        )
        return {row.period_id: _filed(row) for row in result}

    async def filed_return(self, *, period_id: uuid.UUID) -> FiledReturn | None:
        result = await self._session.execute(
            text("SELECT * FROM vat_return WHERE period_id = :period"),
            {"period": str(period_id)},
        )
        row = result.first()
        return None if row is None else _filed(row)

    async def record_filed(
        self,
        *,
        administration_id: uuid.UUID,
        period_id: uuid.UUID,
        period_start: date,
        period_end: date,
        filing_channel: str,
        filing_reference: str | None,
        boxes: list[dict[str, Any]],
        output_vat: Decimal,
        input_vat: Decimal,
        total_due: Decimal,
        rules_fingerprint: str,
        ruleset_provisional: bool,
        warnings_acknowledged: list[str],
        filed_by_user_id: uuid.UUID,
    ) -> FiledReturn:
        result = await self._session.execute(
            text(
                "INSERT INTO vat_return (organization_id, administration_id, period_id, "
                "  period_start, period_end, filing_channel, filing_reference, boxes, "
                "  output_vat, input_vat, total_due, rules_fingerprint, ruleset_provisional, "
                "  warnings_acknowledged, filed_by_user_id) "
                "SELECT a.organization_id, a.id, :period, :start, :end, :channel, :reference, "
                "  CAST(:boxes AS jsonb), :output_vat, :input_vat, :total_due, :fingerprint, "
                "  :provisional, CAST(:warnings AS jsonb), :user_id "
                "  FROM administration a WHERE a.id = :admin "
                "RETURNING *"
            ),
            {
                "admin": str(administration_id),
                "period": str(period_id),
                "start": period_start,
                "end": period_end,
                "channel": filing_channel,
                "reference": filing_reference,
                "boxes": json.dumps(boxes),
                "output_vat": output_vat,
                "input_vat": input_vat,
                "total_due": total_due,
                "fingerprint": rules_fingerprint,
                "provisional": ruleset_provisional,
                "warnings": json.dumps(warnings_acknowledged),
                "user_id": str(filed_by_user_id),
            },
        )
        return _filed(result.one())


def _filed(row: Any) -> FiledReturn:
    return FiledReturn(
        id=row.id,
        period_id=row.period_id,
        period_start=row.period_start,
        period_end=row.period_end,
        filing_channel=row.filing_channel,
        filing_reference=row.filing_reference,
        boxes=_jsonb(row.boxes),
        output_vat=Decimal(row.output_vat),
        input_vat=Decimal(row.input_vat),
        total_due=Decimal(row.total_due),
        rules_fingerprint=row.rules_fingerprint,
        ruleset_provisional=bool(row.ruleset_provisional),
        warnings_acknowledged=[str(code) for code in _jsonb(row.warnings_acknowledged)],
        filed_by_user_id=row.filed_by_user_id,
        filed_at=row.filed_at,
    )

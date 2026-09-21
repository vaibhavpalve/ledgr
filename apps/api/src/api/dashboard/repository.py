"""SQL reads for the dashboard's two computed figures that no existing
repository already exposes: the fiscal year's date range, and the VAT totals
already frozen on issued invoices and posted expenses.

--- Why this queries `sales_invoice`, `sales_invoice_vat_total`, `expense` and
    `fiscal_year` directly rather than going through a higher-level service ---

`api.invoicing.repository.SqlInvoiceRepository` and `api.expenses.repository.
SqlCaptureRepository` already read these tables with plain SELECTs - neither
is a ledger posting table, so `ledgr_app`'s grants allow it, the same as every
other read those two repositories already do. This repository reuses that
posture for two sums neither of them computes today, rather than adding a
report-shaped method to a repository whose job is a single invoice or expense.

`api.dashboard.service.DashboardService` composes this repository alongside
`SqlInvoiceRepository`, `SqlCaptureRepository` and `LedgerService` directly -
not through `InvoicingService` or `ExpenseFormService`, whose own
authorization checks are narrower or differently shaped than "View reports"
(Appendix A) and would deny a Viewer who legitimately holds that permission.
`ChartOfAccountsService` IS used (its "View chart of accounts" permission is
coextensive with "View reports" - see `api.dashboard.service`'s docstring).
The dashboard route checks "View reports" once, for the whole aggregation.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class DashboardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fiscal_year_range(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> tuple[date, date] | None:
        """The year's own start/end dates, or None if it does not exist for
        THIS administration - the tenant predicate is defence in depth (RLS is
        the actual boundary, ADR-003), and `None` here is what lets the route
        answer 404 rather than a different tenant's dates.
        """
        result = await self._session.execute(
            text(
                "SELECT start_date, end_date FROM fiscal_year "
                "WHERE id = :fiscal_year_id AND administration_id = :administration_id"
            ),
            {"fiscal_year_id": str(fiscal_year_id), "administration_id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else (row.start_date, row.end_date)

    async def fiscal_year_period_scheme(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Literal["monthly", "quarterly"]:
        """The year's period scheme, which stands in for how often the business
        files BTW until the product has a filing-frequency setting of its own
        (see `api.vat.deadlines`)."""
        result = await self._session.execute(
            text(
                "SELECT period_scheme FROM fiscal_year "
                "WHERE id = :fiscal_year_id AND administration_id = :administration_id"
            ),
            {"fiscal_year_id": str(fiscal_year_id), "administration_id": str(administration_id)},
        )
        return "quarterly" if result.scalar_one() == "quarterly" else "monthly"

    async def output_vat_total(
        self, *, administration_id: uuid.UUID, start: date, end: date
    ) -> Decimal:
        """Output VAT: the sum of `sales_invoice_vat_total.vat_amount` for
        issued invoices dated within the range.

        `sales_invoice_vat_total` (migration 0037) freezes the per-treatment
        VAT at issue (ADR-037) and is never touched again - see
        `api.invoicing.posting`'s module docstring. A credit note's own totals
        are negative (its lines carry negative quantities), so this SUM already
        nets a credited sale's VAT back out; nothing here special-cases credit
        notes.
        """
        result = await self._session.execute(
            text(
                "SELECT COALESCE(SUM(t.vat_amount), 0) AS total "
                "FROM sales_invoice_vat_total t "
                "JOIN sales_invoice i ON i.id = t.invoice_id "
                "WHERE i.administration_id = :administration_id "
                "AND i.status = 'issued' "
                "AND i.invoice_date >= :start AND i.invoice_date <= :end"
            ),
            {"administration_id": str(administration_id), "start": start, "end": end},
        )
        return Decimal(result.scalar_one())

    async def input_vat_total(
        self, *, administration_id: uuid.UUID, start: date, end: date
    ) -> Decimal:
        """Input VAT: the sum of `expense.vat_amount` for POSTED expenses
        dated within the range.

        POSTED, not READY - a ready-but-unposted expense has not reached the
        books yet, and `cash_position` makes the same "reflects what has been
        posted" promise; using a different population for VAT would make the
        two figures disagree about what "in the books" means.
        """
        result = await self._session.execute(
            text(
                "SELECT COALESCE(SUM(vat_amount), 0) AS total FROM expense "
                "WHERE administration_id = :administration_id "
                "AND status = 'posted' "
                "AND expense_date >= :start AND expense_date <= :end"
            ),
            {"administration_id": str(administration_id), "start": start, "end": end},
        )
        return Decimal(result.scalar_one())

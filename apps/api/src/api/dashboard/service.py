"""The dashboard's entry point: FR-UX-005, MOB-006.

Wires the ledger bounded context's own narrow API (`LedgerService` and
`ChartOfAccountsService`, both via their `build_*_service` constructors -
CLAUDE.md's first non-negotiable, never `api.ledger.repository` or `api.ledger.
chart_repository` directly; `tests/ledger/test_bounded_context.py` fails the
build on either) alongside the existing invoicing and expense repositories and
this package's own `DashboardRepository`, then hands every row to `api.
dashboard.model.summarize()` - the pure function that is actually unit-tested.

--- Why this reads `SqlInvoiceRepository` and `SqlCaptureRepository` directly
    rather than `InvoicingService` or `ExpenseFormService` ---

Those two services each run their OWN authorization check before returning
anything - `InvoicingService.list_invoices()` needs "create sales_invoice",
`ExpenseFormService.list_by_status()` needs "submit expense". Neither is the
permission this endpoint checks ("View reports"), and Appendix A's matrix does
not make them coextensive with it: a Viewer holds "View reports" (R) but not
"submit expense" (N) or "create sales_invoice" (N). Composing through those
services would deny a Viewer's own dashboard for lacking a permission the
dashboard was never supposed to need. Their repositories do no authorization
of their own (a repository never does, in this codebase), which is exactly the
shape needed here: one permission check, in the route, gates the whole
aggregation.

`ChartOfAccountsService` is different: its `chart()` needs "View chart of
accounts", and that permission IS coextensive with "View reports" - checked
against `api.authz.matrix.MATRIX` before writing this, every role holding
"View reports" also holds "View chart of accounts" at the same or a wider,
unconditional level (Owner/Accountant/Bookkeeper F/F, Approver C/R with no
conditional narrowing on the chart cell, Viewer R/R). So `chart()` is called
through the real service - `LedgerService.trial_balance()` and
`subledger_balance()` need no such service wrapper because the ledger's own
reporting methods have never done their own authorization (see their
docstrings); `ChartOfAccountsService.chart()` does, so it is threaded the
caller's own `actor_user_id` rather than bypassed.
"""

from __future__ import annotations

import uuid
from datetime import date

from api.dashboard.model import DashboardSummary, summarize
from api.dashboard.repository import DashboardRepository
from api.expenses.repository import SqlCaptureRepository
from api.invoicing.receivables_repository import SqlReceivablesRepository
from api.invoicing.repository import SqlInvoiceRepository
from api.ledger.chart import ChartOfAccountsService
from api.ledger.model import ControlKind
from api.ledger.service import LedgerService

#: No pagination exists anywhere in this codebase yet (checked: `list_invoices`
#: and `list_by_status` both take a bounded `limit` with no cursor). This is
#: generously larger than either route's own default (50) or ceiling (200) so
#: an administration busy enough to have more open items than this fits is the
#: known gap this task's ADR records, not a silent truncation nobody could
#: predict.
_LIST_LIMIT = 1000


class DashboardError(Exception):
    """Base for this module's refusals."""


class FiscalYearNotFound(DashboardError):
    """No such fiscal year for this administration.

    One exception for "does not exist" and "belongs to another tenant" - under
    RLS the query cannot tell them apart and the API must not appear to, the
    same posture every other *NotFound in this codebase takes.
    """


class DashboardService:
    def __init__(
        self,
        *,
        ledger: LedgerService,
        chart: ChartOfAccountsService,
        invoices: SqlInvoiceRepository,
        expenses: SqlCaptureRepository,
        dashboard: DashboardRepository,
        receivables: SqlReceivablesRepository,
    ) -> None:
        self.ledger = ledger
        self.chart = chart
        self.invoices = invoices
        self.expenses = expenses
        self.dashboard = dashboard
        self.receivables = receivables

    async def summary(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        today: date,
    ) -> DashboardSummary:
        """MOB-006's four figures, fiscal-year-to-date.

        "To date" is `min(today, fiscal_year.end_date)` - a fiscal year still
        open when queried is summed up to today; one already ended (a prior
        year looked up after year-end) is summed to its own last day, not
        artificially extended to whatever today happens to be.
        """
        year_range = await self.dashboard.fiscal_year_range(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )
        if year_range is None:
            raise FiscalYearNotFound(
                f"fiscal year {fiscal_year_id} does not exist for administration "
                f"{administration_id}"
            )
        start, end = year_range
        period_end = min(today, end)

        trial_balance_rows = await self.ledger.trial_balance(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )
        chart_accounts = await self.chart.chart(
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            include_blocked=False,
        )
        receivable_rows = await self.ledger.subledger_balance(
            administration_id=administration_id, control_kind=ControlKind.ACCOUNTS_RECEIVABLE
        )
        output_vat = await self.dashboard.output_vat_total(
            administration_id=administration_id, start=start, end=period_end
        )
        input_vat = await self.dashboard.input_vat_total(
            administration_id=administration_id, start=start, end=period_end
        )
        invoices = await self.invoices.list_invoices(
            administration_id=administration_id, limit=_LIST_LIMIT
        )
        expenses = await self.expenses.list_by_status(
            administration_id=administration_id, status=None, limit=_LIST_LIMIT
        )

        # One query, the same one the ageing report reads, so an overdue row shows
        # the figure the receivables screen shows for it. The permission is this
        # route's own `view report`, which that report needs too.
        open_items = await self.receivables.open_items(
            administration_id=administration_id, as_of=today
        )

        return summarize(
            trial_balance_rows=trial_balance_rows,
            chart_accounts=chart_accounts,
            receivable_rows=receivable_rows,
            output_vat=output_vat,
            input_vat=input_vat,
            vat_period_start=start,
            vat_period_end=period_end,
            invoices=invoices,
            expenses=expenses,
            today=today,
            outstanding_by_invoice={item.invoice_id: item.outstanding for item in open_items},
        )

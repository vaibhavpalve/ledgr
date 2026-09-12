"""The mobile home screen's dashboard: FR-UX-005, MOB-006 (PRD §7.4).

    FR-UX-005  The home screen is a prioritised list of what needs the user's
               attention, not a menu of everything the product can do.
    MOB-006    Dashboard: cash position, receivables, VAT estimate, items
               needing attention.

--- What this module is, and is not ---

This is the PURE aggregation half - the same split `api.templates.compliance.
check()` makes between "what the rule says" and "how the rows got fetched".
Every function here takes already-fetched rows (trial balance rows, chart
accounts, subledger rows, invoices, expenses, VAT totals) and produces a
`DashboardSummary`, with no database and no tenant context. `api.dashboard.
service.DashboardService` is the other half: it fetches those rows through the
ledger's own narrow API and the existing invoicing/expense repositories, and
hands them here.

--- Two figures are honest approximations, not the real thing, and say so ---

FR-BNK (bank feeds) and FR-VAT-001 (the VAT return engine) are both P1 and not
built. That does not make `cash_position` or `vat_estimate` placeholders:

    cash_position   the current balance of the administration's liquid-means
                    GL accounts (RGS group "Liquide middelen"), which is fully
                    computable from `ledger.trial_balance()` because FR-GL (the
                    ledger engine) is complete. It reflects what has been
                    POSTED to the books - manually entered bank/cash movements
                    - not a live bank sync. The UI caption says so; this module
                    only computes the number.

    vat_estimate    output VAT recorded on issued invoices minus input VAT
                    recorded on posted expenses, for a date range the caller
                    supplies (fiscal-year-to-date - see api.dashboard.service).
                    It is exactly what "estimate" means and no more: it
                    excludes anything FR-VAT-002's pre-filing validation would
                    catch (unposted documents, unreconciled items) because that
                    engine does not exist. The UI caption says so.

--- Where "liquid means" comes from ---

RGS 3.8 MKB (this codebase's seed data, `apps/api/data/rgs/rgs-3.8-mkb.json`)
groups "Liquide middelen" under the code prefix `BLim`, with two postable
leaves this seed profile uses: `BLimKas` (1000, Kas / cash on hand) and
`BLimBan` (1100, Bank). `BLimKrp` (Kruisposten / transfers in transit) is a
third leaf under the same group and is included on the same basis: it is still
liquid means, mid-transfer. `LIQUID_RGS_PREFIX` matches the whole group by
prefix rather than naming the three leaf codes, so a chart that adds a fourth
`BLim*` leaf (a future RGS version, or a customer's own extension under
FR-ONB-005) is picked up without a code change here.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from api.expenses.model import Expense, ExpenseStatus
from api.invoicing.model import InvoiceStatus, SalesInvoice
from api.ledger.chart import ChartAccount
from api.ledger.model import ZERO, SubledgerRow, TrialBalanceRow

#: RGS 3.8 MKB's "Liquide middelen" group - verified against this codebase's
#: own seed data rather than assumed. See the module docstring.
LIQUID_RGS_PREFIX = "BLim"


class ActionItemKind(enum.Enum):
    """FR-UX-005's three sources for "what needs attention", in priority order.

    Ordering here is documentation only - `build_action_items` is what
    actually orders the returned tuple, and it is tested directly. This enum
    exists so a client can branch on `kind` without parsing a description.
    """

    OVERDUE_INVOICE = "overdue_invoice"
    DRAFT_EXPENSE = "draft_expense"
    DRAFT_INVOICE = "draft_invoice"


@dataclass(frozen=True, slots=True)
class ActionItem:
    """One row of the prioritised list.

    Carries the raw facts a description is built FROM (`api.dashboard.routes`
    translates them at the HTTP boundary, the same split `_view_json` makes for
    `statutory_failures` via `describe()`) plus enough identity to route a tap
    to the right existing tab - `kind` and `id` are that pair.

    Only the fields a given `kind` actually uses are non-None; the others stay
    None rather than being made kind-specific subclasses, because a flat shape
    is what a JSON encoder and a unit test both want and this is not a type
    hierarchy that grows.
    """

    kind: ActionItemKind
    id: uuid.UUID
    #: OVERDUE_INVOICE only.
    invoice_reference: str | None = None
    customer_name: str | None = None
    days_overdue: int | None = None
    #: DRAFT_EXPENSE only.
    supplier: str | None = None
    gross_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    """MOB-006's four figures, as a value the route serialises."""

    cash_position: Decimal
    receivables: Decimal
    vat_estimate: Decimal
    #: The date range `vat_estimate` was computed over, so the UI can show it
    #: plainly ("1 Jan - today") rather than imply the figure covers all time.
    vat_period_start: date
    vat_period_end: date
    items_needing_action: tuple[ActionItem, ...]


def liquid_account_ids(accounts: Sequence[ChartAccount]) -> frozenset[uuid.UUID]:
    """Which of this administration's accounts are liquid means.

    Matched by RGS code prefix against the chart, not against the trial
    balance rows themselves - `TrialBalanceRow` carries no `rgs_code` (it is a
    ledger-owned report shape; RGS mapping belongs to the chart-of-accounts
    bounded context), so the two are joined here, in application code, on
    `account_id`.
    """
    return frozenset(
        account.account_id
        for account in accounts
        if account.rgs_code is not None and account.rgs_code.startswith(LIQUID_RGS_PREFIX)
    )


def cash_position(rows: Sequence[TrialBalanceRow], liquid_ids: frozenset[uuid.UUID]) -> Decimal:
    """The summed balance of the liquid-means rows.

    `TrialBalanceRow.balance` is `debit - credit` regardless of account type
    (see `ledger.trial_balance()`), which for an asset account like cash or
    bank is exactly "money in the account, positive when there is some" - no
    sign flip needed.
    """
    return sum((row.balance for row in rows if row.account_id in liquid_ids), ZERO)


def receivables_total(rows: Sequence[SubledgerRow]) -> Decimal:
    """The AR control account's current balance, across every party.

    Not fiscal-year-scoped: `ledger.subledger_balance()` takes no fiscal year
    parameter, because a receivable does not become uncollectable at a
    calendar boundary - the same reason the control account balance it mirrors
    is not scoped either (FR-GL-006).
    """
    return sum((row.balance for row in rows), ZERO)


def vat_estimate(*, output_vat: Decimal, input_vat: Decimal) -> Decimal:
    """Output VAT (on issued invoices) minus input VAT (on posted expenses).

    Trivial arithmetic, and still its own function: a caller unit-tests the
    Decimal path without assembling the rest of `DashboardSummary`, and a
    reader sees the definition in one place rather than inline at the call
    site.
    """
    return output_vat - input_vat


def build_action_items(
    *,
    invoices: Sequence[SalesInvoice],
    expenses: Sequence[Expense],
    today: date,
) -> tuple[ActionItem, ...]:
    """FR-UX-005's prioritised list.

    The rule, in order:

      1. Overdue issued invoices - the most urgent, because money is owed and
         late - sorted MOST-overdue-first. Somebody with five overdue
         invoices chases the oldest one first.
      2. Draft expenses awaiting completion - captured but not yet reviewed,
         in the order the repository already returns them (newest-first; see
         `api.expenses.repository.SqlCaptureRepository.list_by_status`).
      3. Draft sales invoices not yet issued - a half-finished mobile-created
         invoice, same repository ordering as (2).

    "Overdue" is FR-BNK's honest definition, not an invented one: no
    payment/settlement concept exists against `sales_invoice` (FR-BNK's bank
    reconciliation is P1 and not built), so an issued invoice past its
    `due_date` and not credited is overdue. A credit note against it is the
    only way an issued invoice's claim is withdrawn today, so it is excluded
    once credited rather than left to look permanently overdue.
    """
    credited_ids = frozenset(
        invoice.credits_invoice_id for invoice in invoices if invoice.credits_invoice_id is not None
    )

    overdue = [
        invoice
        for invoice in invoices
        if invoice.status is InvoiceStatus.ISSUED
        # A credit note is itself issued and carries no due_date obligation of
        # its own - it is the correction, not a claim that can be overdue.
        and invoice.credits_invoice_id is None
        and invoice.due_date is not None
        and invoice.due_date < today
        and invoice.id not in credited_ids
    ]
    overdue.sort(key=lambda invoice: (today - invoice.due_date).days, reverse=True)  # type: ignore[operator]

    draft_expenses = [expense for expense in expenses if expense.status is ExpenseStatus.DRAFT]
    draft_invoices = [invoice for invoice in invoices if invoice.status is InvoiceStatus.DRAFT]

    items: list[ActionItem] = [
        ActionItem(
            kind=ActionItemKind.OVERDUE_INVOICE,
            id=invoice.id,
            invoice_reference=invoice.invoice_reference,
            customer_name=invoice.customer_name,
            days_overdue=(today - invoice.due_date).days,  # type: ignore[operator]
        )
        for invoice in overdue
    ]
    items.extend(
        ActionItem(
            kind=ActionItemKind.DRAFT_EXPENSE,
            id=expense.id,
            supplier=expense.supplier,
            gross_amount=expense.gross_amount,
        )
        for expense in draft_expenses
    )
    items.extend(
        ActionItem(
            kind=ActionItemKind.DRAFT_INVOICE,
            id=invoice.id,
            customer_name=invoice.customer_name,
        )
        for invoice in draft_invoices
    )
    return tuple(items)


def summarize(
    *,
    trial_balance_rows: Sequence[TrialBalanceRow],
    chart_accounts: Sequence[ChartAccount],
    receivable_rows: Sequence[SubledgerRow],
    output_vat: Decimal,
    input_vat: Decimal,
    vat_period_start: date,
    vat_period_end: date,
    invoices: Sequence[SalesInvoice],
    expenses: Sequence[Expense],
    today: date,
) -> DashboardSummary:
    """The one call `DashboardService.summary()` makes once every row is in
    hand. Everything above exists so this function's body is composition, not
    logic.
    """
    liquid_ids = liquid_account_ids(chart_accounts)
    return DashboardSummary(
        cash_position=cash_position(trial_balance_rows, liquid_ids),
        receivables=receivables_total(receivable_rows),
        vat_estimate=vat_estimate(output_vat=output_vat, input_vat=input_vat),
        vat_period_start=vat_period_start,
        vat_period_end=vat_period_end,
        items_needing_action=build_action_items(invoices=invoices, expenses=expenses, today=today),
    )

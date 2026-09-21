"""Pure aggregation logic for the mobile home screen - FR-UX-005, MOB-006.

No database, no tenant context, no FastAPI: every function in
`api.dashboard.model` takes already-constructed rows and asserts on the
`DashboardSummary`/`ActionItem` it produces, the same "smallest fake that
exercises the real code path" posture `tests/invoicing/test_list_invoices.py`
and `tests/templates/test_compliance.py` already take for this codebase's other
pure-logic modules.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from api.dashboard.model import (
    ActionItemKind,
    build_action_items,
    cash_position,
    liquid_account_ids,
    receivables_total,
    summarize,
    vat_estimate,
)
from api.expenses.model import Expense, ExpenseStatus
from api.i18n.language import Language
from api.invoicing.model import InvoiceStatus, SalesInvoice
from api.ledger.chart import ChartAccount
from api.ledger.model import AccountStatus, AccountType, SubledgerRow, TrialBalanceRow

# --- factories ---------------------------------------------------------------


def _chart_account(*, rgs_code: str | None, account_id: uuid.UUID | None = None) -> ChartAccount:
    return ChartAccount(
        account_id=account_id or uuid.uuid4(),
        code="1000",
        name="Test account",
        account_type=AccountType.ASSET,
        status=AccountStatus.ACTIVE,
        rgs_code=rgs_code,
    )


def _trial_balance_row(*, account_id: uuid.UUID, balance: Decimal) -> TrialBalanceRow:
    return TrialBalanceRow(
        account_id=account_id,
        account_code="1000",
        account_name="Test account",
        account_type=AccountType.ASSET,
        total_debit=balance if balance >= 0 else Decimal("0.00"),
        total_credit=Decimal("0.00") if balance >= 0 else -balance,
        balance=balance,
    )


def _subledger_row(*, balance: Decimal) -> SubledgerRow:
    return SubledgerRow(
        party_id=uuid.uuid4(),
        party_name="Some customer",
        total_debit=balance,
        total_credit=Decimal("0.00"),
        balance=balance,
    )


def _invoice(**overrides: object) -> SalesInvoice:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        fiscal_year_id=uuid.uuid4(),
        status=InvoiceStatus.DRAFT,
        invoice_number=None,
        number_prefix=None,
        invoice_reference=None,
        invoice_date=date(2026, 1, 1),
        supply_date=None,
        due_date=None,
        customer_name="Jansen BV",
        customer_address="Damrak 1",
        customer_country="NL",
        customer_vat_number=None,
        customer_id=None,
        customer_language=Language.NL,
        credits_invoice_id=None,
        notes=None,
        issued_at=None,
    )
    defaults.update(overrides)
    return SalesInvoice(**defaults)  # type: ignore[arg-type]


def _expense(**overrides: object) -> Expense:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.DRAFT,
        submitted_by_user_id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return Expense(**defaults)  # type: ignore[arg-type]


TODAY = date(2026, 9, 12)

# --- cash position -----------------------------------------------------------


def test_liquid_account_ids_matches_the_blim_rgs_group() -> None:
    """RGS 3.8 MKB's 'Liquide middelen' group - verified against
    apps/api/data/rgs/rgs-3.8-mkb.json, not assumed.
    """
    kas = _chart_account(rgs_code="BLimKas")
    bank = _chart_account(rgs_code="BLimBan")
    receivables = _chart_account(rgs_code="TFvordAf")
    unmapped = _chart_account(rgs_code=None)

    ids = liquid_account_ids([kas, bank, receivables, unmapped])

    assert ids == {kas.account_id, bank.account_id}


def test_cash_position_sums_only_liquid_accounts() -> None:
    bank_id, cash_id, revenue_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        _trial_balance_row(account_id=bank_id, balance=Decimal("1250.00")),
        _trial_balance_row(account_id=cash_id, balance=Decimal("50.00")),
        # Not liquid means - must not be counted even though it is an asset.
        _trial_balance_row(account_id=revenue_id, balance=Decimal("999999.00")),
    ]
    liquid_ids = frozenset({bank_id, cash_id})

    result = cash_position(rows, liquid_ids)

    assert result == Decimal("1300.00")
    assert isinstance(result, Decimal)


def test_cash_position_is_zero_with_no_liquid_accounts() -> None:
    rows = [_trial_balance_row(account_id=uuid.uuid4(), balance=Decimal("500.00"))]

    assert cash_position(rows, frozenset()) == Decimal("0.00")


# --- receivables --------------------------------------------------------------


def test_receivables_total_sums_every_party() -> None:
    rows = [
        _subledger_row(balance=Decimal("1000.00")),
        _subledger_row(balance=Decimal("250.50")),
    ]

    assert receivables_total(rows) == Decimal("1250.50")


def test_receivables_total_is_zero_with_no_parties() -> None:
    assert receivables_total([]) == Decimal("0.00")


# --- VAT estimate --------------------------------------------------------------


def test_vat_estimate_is_output_minus_input() -> None:
    result = vat_estimate(output_vat=Decimal("2100.00"), input_vat=Decimal("315.00"))

    assert result == Decimal("1785.00")
    assert isinstance(result, Decimal)


def test_vat_estimate_can_be_negative_when_input_exceeds_output() -> None:
    """A quiet month with heavy purchases: a legitimate negative estimate,
    not clamped to zero - clamping would misreport an actual reclaimable
    position as nothing owed either way.
    """
    result = vat_estimate(output_vat=Decimal("100.00"), input_vat=Decimal("400.00"))

    assert result == Decimal("-300.00")


# --- prioritised action items --------------------------------------------------


def test_overdue_invoices_rank_before_draft_expenses_and_invoices() -> None:
    overdue = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2026, 9, 1), invoice_number=1)
    draft_expense = _expense(status=ExpenseStatus.DRAFT)
    draft_invoice = _invoice(status=InvoiceStatus.DRAFT)

    items = build_action_items(
        invoices=[draft_invoice, overdue], expenses=[draft_expense], today=TODAY
    )

    assert [item.kind for item in items] == [
        ActionItemKind.OVERDUE_INVOICE,
        ActionItemKind.DRAFT_EXPENSE,
        ActionItemKind.DRAFT_INVOICE,
    ]


def test_a_more_overdue_invoice_ranks_above_a_less_overdue_one() -> None:
    """The specific ordering proof this feature's test plan calls for: within
    the overdue group, most-days-late sorts first.
    """
    barely_overdue = _invoice(
        status=InvoiceStatus.ISSUED, due_date=date(2026, 9, 10), invoice_number=1
    )
    very_overdue = _invoice(
        status=InvoiceStatus.ISSUED, due_date=date(2026, 8, 1), invoice_number=2
    )

    items = build_action_items(invoices=[barely_overdue, very_overdue], expenses=[], today=TODAY)

    assert [item.id for item in items] == [very_overdue.id, barely_overdue.id]
    assert items[0].days_overdue is not None and items[1].days_overdue is not None
    assert items[0].days_overdue > items[1].days_overdue


def test_an_invoice_due_today_is_not_overdue() -> None:
    due_today = _invoice(status=InvoiceStatus.ISSUED, due_date=TODAY, invoice_number=1)

    items = build_action_items(invoices=[due_today], expenses=[], today=TODAY)

    assert items == ()


def test_an_invoice_not_yet_due_is_not_an_action_item() -> None:
    not_due_yet = _invoice(
        status=InvoiceStatus.ISSUED, due_date=date(2026, 12, 1), invoice_number=1
    )

    items = build_action_items(invoices=[not_due_yet], expenses=[], today=TODAY)

    assert items == ()


def test_a_credited_overdue_invoice_is_excluded() -> None:
    """FR-BNK has no payment/settlement concept yet, so a credit note is the
    only way an issued invoice's claim is withdrawn - and it must stop
    looking overdue once one exists.
    """
    original = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2026, 1, 1), invoice_number=1)
    credit_note = _invoice(
        status=InvoiceStatus.ISSUED,
        credits_invoice_id=original.id,
        invoice_number=2,
    )

    items = build_action_items(invoices=[original, credit_note], expenses=[], today=TODAY)

    assert items == ()


def test_a_credit_note_itself_is_never_flagged_as_overdue() -> None:
    """A credit note is issued with no due_date obligation of its own; even
    one dated in the past by coincidence is not a claim that can be overdue.
    """
    credit_note = _invoice(
        status=InvoiceStatus.ISSUED,
        credits_invoice_id=uuid.uuid4(),
        due_date=date(2020, 1, 1),
        invoice_number=1,
    )

    items = build_action_items(invoices=[credit_note], expenses=[], today=TODAY)

    assert items == ()


def test_draft_expenses_and_invoices_keep_repository_order() -> None:
    """FR-UX-005 ranks overdue invoices first; within the draft groups this
    module trusts the repository's own newest-first ordering rather than
    re-sorting - see `build_action_items`'s docstring.
    """
    first_expense = _expense(status=ExpenseStatus.DRAFT)
    second_expense = _expense(status=ExpenseStatus.DRAFT)
    first_invoice = _invoice(status=InvoiceStatus.DRAFT)
    second_invoice = _invoice(status=InvoiceStatus.DRAFT)

    items = build_action_items(
        invoices=[first_invoice, second_invoice],
        expenses=[first_expense, second_expense],
        today=TODAY,
    )

    expense_ids = [item.id for item in items if item.kind is ActionItemKind.DRAFT_EXPENSE]
    invoice_ids = [item.id for item in items if item.kind is ActionItemKind.DRAFT_INVOICE]
    assert expense_ids == [first_expense.id, second_expense.id]
    assert invoice_ids == [first_invoice.id, second_invoice.id]


def test_posted_and_ready_expenses_are_not_action_items() -> None:
    ready = _expense(status=ExpenseStatus.READY)
    posted = _expense(status=ExpenseStatus.POSTED)

    items = build_action_items(invoices=[], expenses=[ready, posted], today=TODAY)

    assert items == ()


def test_issued_invoices_are_not_draft_items() -> None:
    issued = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2027, 1, 1), invoice_number=1)

    items = build_action_items(invoices=[issued], expenses=[], today=TODAY)

    assert items == ()


# --- summarize composes everything ---------------------------------------------


def test_summarize_composes_every_figure() -> None:
    bank_id = uuid.uuid4()
    chart_accounts = [_chart_account(rgs_code="BLimBan", account_id=bank_id)]
    trial_balance_rows = [_trial_balance_row(account_id=bank_id, balance=Decimal("400.00"))]
    receivable_rows = [_subledger_row(balance=Decimal("150.00"))]
    overdue = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2026, 1, 1), invoice_number=1)

    summary = summarize(
        trial_balance_rows=trial_balance_rows,
        chart_accounts=chart_accounts,
        receivable_rows=receivable_rows,
        output_vat=Decimal("210.00"),
        input_vat=Decimal("50.00"),
        vat_period_start=date(2026, 1, 1),
        vat_period_end=TODAY,
        invoices=[overdue],
        expenses=[],
        today=TODAY,
    )

    assert summary.cash_position == Decimal("400.00")
    assert summary.receivables == Decimal("150.00")
    assert summary.vat_estimate == Decimal("160.00")
    assert summary.vat_period_start == date(2026, 1, 1)
    assert summary.vat_period_end == TODAY
    assert len(summary.items_needing_action) == 1
    assert summary.items_needing_action[0].kind is ActionItemKind.OVERDUE_INVOICE


def test_no_floats_anywhere_in_the_money_path() -> None:
    """NFR-031 / CLAUDE.md rule four, asserted the way `test_ledger` modules
    assert it: every figure `summarize` produces is a Decimal, never a float,
    even when every input is exact.
    """
    summary = summarize(
        trial_balance_rows=[],
        chart_accounts=[],
        receivable_rows=[],
        output_vat=Decimal("100.00"),
        input_vat=Decimal("33.33"),
        vat_period_start=date(2026, 1, 1),
        vat_period_end=TODAY,
        invoices=[],
        expenses=[],
        today=TODAY,
    )

    assert isinstance(summary.cash_position, Decimal)
    assert isinstance(summary.receivables, Decimal)
    assert isinstance(summary.vat_estimate, Decimal)
    assert summary.vat_estimate == Decimal("66.67")


# --- what the Review nav item and the attention rows read ---------------------


def test_summarize_counts_the_draft_receipts_awaiting_review() -> None:
    summary = summarize(
        trial_balance_rows=[],
        chart_accounts=[],
        receivable_rows=[],
        output_vat=Decimal("0.00"),
        input_vat=Decimal("0.00"),
        vat_period_start=date(2026, 1, 1),
        vat_period_end=TODAY,
        invoices=[_invoice(status=InvoiceStatus.DRAFT)],
        expenses=[
            _expense(supplier="Papierhuis", gross_amount=Decimal("52.80")),
            _expense(supplier="Kantoor BV", gross_amount=Decimal("10.00")),
            _expense(status=ExpenseStatus.POSTED),
        ],
        today=TODAY,
    )

    # Two drafts; the posted one and the draft invoice are not receipts to review.
    assert summary.receipts_to_review == 2


def test_a_draft_receipt_row_carries_its_gross_amount_and_an_invoice_row_its_customer() -> None:
    items = build_action_items(
        invoices=[_invoice(status=InvoiceStatus.DRAFT, customer_name="Studio Noord")],
        expenses=[_expense(supplier="Papierhuis", gross_amount=Decimal("52.80"))],
        today=TODAY,
    )

    receipt = next(item for item in items if item.kind is ActionItemKind.DRAFT_EXPENSE)
    invoice = next(item for item in items if item.kind is ActionItemKind.DRAFT_INVOICE)
    assert receipt.gross_amount == Decimal("52.80")
    assert invoice.customer_name == "Studio Noord"


def test_an_overdue_row_carries_what_is_still_owed_not_the_invoice_total() -> None:
    partly_paid = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2026, 1, 1), invoice_number=1)
    unknown = _invoice(status=InvoiceStatus.ISSUED, due_date=date(2026, 2, 1), invoice_number=2)

    items = build_action_items(
        invoices=[partly_paid, unknown],
        expenses=[],
        today=TODAY,
        outstanding_by_invoice={partly_paid.id: Decimal("310.25")},
    )

    by_id = {item.id: item for item in items}
    assert by_id[partly_paid.id].outstanding == Decimal("310.25")
    # No open-item row for it: no amount, rather than a guessed one.
    assert by_id[unknown.id].outstanding is None

"""Value types for the Reports screen.

Both reports are computed entirely from `ledger.balances_as_of` (migration
0062) - a cumulative debit-minus-credit balance per account, as of a date,
within one fiscal year. Nothing here posts, writes, or names a posting table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


class ReportsError(Exception):
    pass


class InvalidReportRange(ReportsError):
    pass


@dataclass(frozen=True, slots=True)
class ReportLine:
    account_id: uuid.UUID
    account_code: str
    account_name: str
    #: Always shown POSITIVE for its section's normal side - an asset's debit
    #: balance, a liability/equity/revenue's credit balance, an expense's
    #: debit balance. The sign flip from the ledger's debit-minus-credit
    #: convention happens once, here, so nothing downstream has to know it.
    amount: Decimal


@dataclass(frozen=True, slots=True)
class BalanceSheet:
    fiscal_year_id: uuid.UUID
    as_of: date
    assets: tuple[ReportLine, ...]
    liabilities: tuple[ReportLine, ...]
    equity: tuple[ReportLine, ...]
    #: Revenue less expense, year-to-date through `as_of` - folded into
    #: equity as its own line (`current_year_result`), the way an interim
    #: balance sheet always must: a balance sheet with no P&L line for the
    #: year in progress does not balance.
    current_year_result: Decimal

    @property
    def total_assets(self) -> Decimal:
        return sum((line.amount for line in self.assets), Decimal("0.00"))

    @property
    def total_liabilities(self) -> Decimal:
        return sum((line.amount for line in self.liabilities), Decimal("0.00"))

    @property
    def total_equity(self) -> Decimal:
        return (
            sum((line.amount for line in self.equity), Decimal("0.00")) + self.current_year_result
        )

    @property
    def is_balanced(self) -> bool:
        return self.total_assets == self.total_liabilities + self.total_equity


@dataclass(frozen=True, slots=True)
class IncomeStatement:
    fiscal_year_id: uuid.UUID
    period_start: date
    period_end: date
    revenue: tuple[ReportLine, ...]
    expense: tuple[ReportLine, ...]

    @property
    def total_revenue(self) -> Decimal:
        return sum((line.amount for line in self.revenue), Decimal("0.00"))

    @property
    def total_expense(self) -> Decimal:
        return sum((line.amount for line in self.expense), Decimal("0.00"))

    @property
    def net_result(self) -> Decimal:
        return self.total_revenue - self.total_expense

"""Balance sheet and income statement - built entirely on
`LedgerService.balances_as_of` (migration 0062), never a new query against a
posting table.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from api.ledger.model import AccountType
from api.ledger.service import LedgerService
from api.reports.model import BalanceSheet, IncomeStatement, InvalidReportRange, ReportLine
from api.reports.repository import SqlReportsRepository

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

_ZERO = Decimal("0.00")

#: Which of the ledger's debit-minus-credit balance is flipped to show a
#: positive figure on its own statement's normal side.
_CREDIT_NORMAL = frozenset({AccountType.LIABILITY, AccountType.EQUITY, AccountType.REVENUE})


class ReportsService:
    def __init__(self, ledger: LedgerService, repository: SqlReportsRepository) -> None:
        self._ledger = ledger
        self._repository = repository

    async def balance_sheet(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID, as_of: date
    ) -> BalanceSheet:
        accounts = await self._repository.accounts_by_id(administration_id=administration_id)
        balances = await self._ledger.balances_as_of(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id, as_of=as_of
        )

        assets: list[ReportLine] = []
        liabilities: list[ReportLine] = []
        equity: list[ReportLine] = []
        revenue_total = _ZERO
        expense_total = _ZERO

        for row in balances:
            account = accounts.get(row.account_id)
            if account is None:
                continue  # pragma: no cover - RLS/administration mismatch, defensive only
            account_type = AccountType(account.account_type)
            amount = -row.balance if account_type in _CREDIT_NORMAL else row.balance
            if amount == 0:
                continue
            line = ReportLine(
                account_id=row.account_id,
                account_code=account.code,
                account_name=account.name,
                amount=amount,
            )
            if account_type is AccountType.ASSET:
                assets.append(line)
            elif account_type is AccountType.LIABILITY:
                liabilities.append(line)
            elif account_type is AccountType.EQUITY:
                equity.append(line)
            elif account_type is AccountType.REVENUE:
                revenue_total += amount
            elif account_type is AccountType.EXPENSE:
                expense_total += amount

        return BalanceSheet(
            fiscal_year_id=fiscal_year_id,
            as_of=as_of,
            assets=tuple(sorted(assets, key=lambda line: line.account_code)),
            liabilities=tuple(sorted(liabilities, key=lambda line: line.account_code)),
            equity=tuple(sorted(equity, key=lambda line: line.account_code)),
            current_year_result=revenue_total - expense_total,
        )

    async def income_statement(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        period_start: date,
        period_end: date,
    ) -> IncomeStatement:
        """Revenue and expense for a range, both within one fiscal year:
        `balances_as_of(period_end)` minus `balances_as_of(the day before
        period_start)`, per account - `balances_as_of` is already cumulative
        from the fiscal year's own start, so the difference isolates exactly
        this range without a second SQL shape.
        """
        if period_end < period_start:
            raise InvalidReportRange("the period's end date is before its start date")

        accounts = await self._repository.accounts_by_id(administration_id=administration_id)
        end_balances = {
            row.account_id: row.balance
            for row in await self._ledger.balances_as_of(
                administration_id=administration_id,
                fiscal_year_id=fiscal_year_id,
                as_of=period_end,
            )
        }
        day_before = period_start - timedelta(days=1)
        start_balances = {
            row.account_id: row.balance
            for row in await self._ledger.balances_as_of(
                administration_id=administration_id,
                fiscal_year_id=fiscal_year_id,
                as_of=day_before,
            )
        }

        revenue: list[ReportLine] = []
        expense: list[ReportLine] = []
        for account_id in set(end_balances) | set(start_balances):
            account = accounts.get(account_id)
            if account is None:
                continue  # pragma: no cover - defensive only
            account_type = AccountType(account.account_type)
            if account_type not in (AccountType.REVENUE, AccountType.EXPENSE):
                continue
            delta = end_balances.get(account_id, _ZERO) - start_balances.get(account_id, _ZERO)
            amount = -delta if account_type is AccountType.REVENUE else delta
            if amount == 0:
                continue
            line = ReportLine(
                account_id=account_id,
                account_code=account.code,
                account_name=account.name,
                amount=amount,
            )
            (revenue if account_type is AccountType.REVENUE else expense).append(line)

        return IncomeStatement(
            fiscal_year_id=fiscal_year_id,
            period_start=period_start,
            period_end=period_end,
            revenue=tuple(sorted(revenue, key=lambda line: line.account_code)),
            expense=tuple(sorted(expense, key=lambda line: line.account_code)),
        )


def build_reports_service(session: AsyncSession, ledger: LedgerService) -> ReportsService:
    return ReportsService(ledger, SqlReportsRepository(session))

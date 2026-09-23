"""The Reports screen's HTTP surface (`/reports` in the web app).

    GET /v1/administrations/{id}/reports/balance-sheet?fiscal_year_id=&as_of=
    GET /v1/administrations/{id}/reports/income-statement?fiscal_year_id=&period_start=&period_end=

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register` for why.

Both routes ride Appendix A's existing "View reports" permission (`view
report`) - the same one `api.ledger_reads.routes` uses for the trial balance
and the journal. No new capability, no new migration: both reports are
computed entirely from `ledger.balances_as_of` (0062).
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import get_fiscal_year_service
from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.db import get_db_session
from api.i18n.http import problem
from api.ledger import FiscalYear, FiscalYearService, NotAuthorizedToDefineYear
from api.ledger.service import build_ledger_service
from api.reports.model import BalanceSheet, IncomeStatement, InvalidReportRange, ReportLine
from api.reports.service import ReportsService, build_reports_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"


def register(app: FastAPI) -> None:
    app.add_api_route(
        f"{_BASE}/reports/balance-sheet",
        get_balance_sheet,
        methods=["GET"],
        name="get_balance_sheet",
    )
    app.add_api_route(
        f"{_BASE}/reports/income-statement",
        get_income_statement,
        methods=["GET"],
        name="get_income_statement",
    )


async def get_reports_service(
    session: AsyncSession = Depends(get_db_session),
) -> ReportsService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return build_reports_service(session, build_ledger_service(session, audit_log))


async def _fiscal_year_of(
    request: Request,
    fiscal: FiscalYearService,
    *,
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> FiscalYear:
    """The year must be THIS administration's - the same posture
    `api.ledger_reads.routes._fiscal_year_of` takes, and for the same
    reason: a year belonging to another administration would otherwise
    report every account at zero, which is wrong in a way nothing on the
    page says.
    """
    try:
        years = await fiscal.visible_years(
            administration_id=administration_id, actor_user_id=actor_user_id
        )
    except NotAuthorizedToDefineYear as exc:
        raise problem(
            request,
            403,
            "errors.not_permitted",
            reason="no_matching_grant",
            action=exc.action,
            resource_type=exc.resource_type,
            detail=exc.detail,
        ) from exc
    for year in years:
        if year.id == fiscal_year_id:
            return year
    raise problem(request, 404, "errors.fiscal_year_not_found", reason="fiscal_year_not_found")


def _line_json(line: ReportLine) -> dict[str, object]:
    return {
        "account_id": str(line.account_id),
        "account_code": line.account_code,
        "account_name": line.account_name,
        "amount": str(line.amount),
    }


def _balance_sheet_json(sheet: BalanceSheet) -> dict[str, object]:
    return {
        "fiscal_year_id": str(sheet.fiscal_year_id),
        "as_of": sheet.as_of.isoformat(),
        "assets": [_line_json(line) for line in sheet.assets],
        "liabilities": [_line_json(line) for line in sheet.liabilities],
        "equity": [_line_json(line) for line in sheet.equity],
        "current_year_result": str(sheet.current_year_result),
        "total_assets": str(sheet.total_assets),
        "total_liabilities": str(sheet.total_liabilities),
        "total_equity": str(sheet.total_equity),
        "is_balanced": sheet.is_balanced,
    }


def _income_statement_json(statement: IncomeStatement) -> dict[str, object]:
    return {
        "fiscal_year_id": str(statement.fiscal_year_id),
        "period_start": statement.period_start.isoformat(),
        "period_end": statement.period_end.isoformat(),
        "revenue": [_line_json(line) for line in statement.revenue],
        "expense": [_line_json(line) for line in statement.expense],
        "total_revenue": str(statement.total_revenue),
        "total_expense": str(statement.total_expense),
        "net_result": str(statement.net_result),
    }


async def get_balance_sheet(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    as_of: date | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    reports: ReportsService = Depends(get_reports_service),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    year = await _fiscal_year_of(
        request,
        fiscal,
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        actor_user_id=tenant.user_id,
    )
    sheet = await reports.balance_sheet(
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        as_of=as_of or year.end_date,
    )
    return _balance_sheet_json(sheet)


async def get_income_statement(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    period_start: date | None = None,
    period_end: date | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    reports: ReportsService = Depends(get_reports_service),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    year = await _fiscal_year_of(
        request,
        fiscal,
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        actor_user_id=tenant.user_id,
    )
    try:
        statement = await reports.income_statement(
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            period_start=period_start or year.start_date,
            period_end=period_end or year.end_date,
        )
    except InvalidReportRange as exc:
        raise problem(
            request, 422, "errors.report_period_invalid", reason="report_period_invalid"
        ) from exc
    return _income_statement_json(statement)

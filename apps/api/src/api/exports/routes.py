"""Exports of the books (ADR-089).

    GET /v1/administrations/{id}/exports/journal.csv?fiscal_year_id=&dialect=
        every posted journal line of the year (grootboekmutaties)
    GET /v1/administrations/{id}/exports/trial-balance.csv?fiscal_year_id=&dialect=
        the year's trial balance (saldibalans)

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

Appendix A's "Export data" (`export report_data`), audited as an EXPORT (IAM-090 names exports
explicitly: data that leaves the product is the event an auditor asks about). Reads only.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import get_fiscal_year_service
from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import administration_from_path, require_permission
from api.authz.model import AuthorizationDecision
from api.db import get_db_session
from api.exports.csv import render
from api.i18n.http import problem
from api.ledger import FiscalYear, FiscalYearService, NotAuthorizedToDefineYear
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"


def register(app: FastAPI) -> None:
    app.add_api_route(
        f"{_BASE}/exports/journal.csv", export_journal, methods=["GET"], name="export_journal"
    )
    app.add_api_route(
        f"{_BASE}/exports/trial-balance.csv",
        export_trial_balance,
        methods=["GET"],
        name="export_trial_balance",
    )


_EXPORT = require_permission(
    "export",
    "report_data",
    scope=administration_from_path("administration_id"),
    audit=AuditCategory.EXPORT,
)


async def _year(
    request: Request,
    tenant: TenantContext,
    fiscal: FiscalYearService,
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
) -> FiscalYear:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        years = await fiscal.visible_years(
            administration_id=administration_id, actor_user_id=tenant.user_id
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


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _slug(start: date, end: date) -> str:
    return f"{start.isoformat()}_{end.isoformat()}"


async def export_journal(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    dialect: Literal["nl", "international"] = "nl",
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(_EXPORT),
) -> Response:
    year = await _year(request, tenant, fiscal, administration_id, fiscal_year_id)
    result = await session.execute(
        text(
            "SELECT e.entry_number, e.entry_date, j.code AS journal, e.description, "
            "       e.document_reference, a.code AS account_code, a.name AS account_name, "
            "       l.description AS line_description, l.debit, l.credit, l.vat_treatment, "
            "       e.reverses_entry_id IS NOT NULL AS is_reversal "
            "  FROM journal_line l "
            "  JOIN journal_entry e ON e.id = l.journal_entry_id "
            "  JOIN ledger_journal j ON j.id = e.journal_id "
            "  JOIN ledger_account a ON a.id = l.account_id "
            " WHERE l.administration_id = :admin AND e.fiscal_year_id = :year "
            " ORDER BY e.entry_date, e.entry_number, l.line_number"
        ),
        {"admin": str(administration_id), "year": str(year.id)},
    )
    body = render(
        [
            "boeking",
            "datum",
            "dagboek",
            "omschrijving",
            "document",
            "rekening",
            "rekeningnaam",
            "regelomschrijving",
            "debet",
            "credit",
            "btw",
            "tegenboeking",
        ],
        (
            [
                row.entry_number,
                row.entry_date.isoformat(),
                row.journal,
                row.description,
                row.document_reference or "",
                row.account_code,
                row.account_name,
                row.line_description or "",
                Decimal(row.debit),
                Decimal(row.credit),
                row.vat_treatment or "",
                "ja" if row.is_reversal else "",
            ]
            for row in result
        ),
        dialect=dialect,
    )
    return _csv(body, f"grootboekmutaties_{_slug(year.start_date, year.end_date)}.csv")


async def export_trial_balance(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    dialect: Literal["nl", "international"] = "nl",
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(_EXPORT),
) -> Response:
    year = await _year(request, tenant, fiscal, administration_id, fiscal_year_id)
    ledger = build_ledger_service(session, AuditLog(SqlAuditRepository(session)))
    rows = await ledger.trial_balance(administration_id=administration_id, fiscal_year_id=year.id)
    body = render(
        ["rekening", "rekeningnaam", "soort", "debet", "credit", "saldo"],
        (
            [
                row.account_code,
                row.account_name,
                row.account_type.value,
                row.total_debit,
                row.total_credit,
                row.balance,
            ]
            for row in rows
        ),
        dialect=dialect,
    )
    return _csv(body, f"saldibalans_{_slug(year.start_date, year.end_date)}.csv")

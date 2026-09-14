"""The Grootboek screen's reads (docs/founder-review-2026-09-14.md §4.3).

    GET /v1/administrations/{id}/chart-of-accounts
    GET /v1/administrations/{id}/trial-balance?fiscal_year_id=
    GET /v1/administrations/{id}/journal-entries?fiscal_year_id=&cursor=&limit=
    GET /v1/administrations/{id}/journal-entries/{entry_id}

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register`'s docstring for why.

Every amount crosses the wire as a Decimal-shaped STRING (NFR-031), the same
convention `api.dashboard.routes` and `api.customers.routes` follow. Every
read goes through `LedgerService`/`ChartOfAccountsService` obtained from the
context's builders - never the repositories; tests/ledger/test_bounded_context.py
fails the build otherwise.

--- Permissions ---

The chart rides on Appendix A's "View chart of accounts" (`view
chart_of_accounts`), which is exactly what it is. The trial balance and the
journal ride on "View reports" (`view report`), the same choice
`api.dashboard.routes` made and for the same reason: Appendix A has no
"read the journal" row - "Post journal entries" is full-only - and a
trial balance and a journal listing ARE reports, read-only and asserting
nothing to anyone. Every read declares `FINANCIAL_READ`, IAM-090's "data
reads of financial records", which these are.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from fastapi import Depends, FastAPI, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import fiscal_year_json, get_fiscal_year_service
from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.db import get_db_session
from api.i18n.http import problem
from api.ledger import (
    ZERO,
    ChartAccount,
    ChartNotAuthorized,
    ChartOfAccountsService,
    EntryCursor,
    FiscalYear,
    FiscalYearService,
    JournalEntrySummary,
    LedgerService,
    NotAuthorizedToDefineYear,
    PostedEntry,
    PostedLine,
    TrialBalanceRow,
)
from api.ledger.chart import build_chart_service
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"


def register(app: FastAPI) -> None:
    app.add_api_route(
        f"{_BASE}/chart-of-accounts",
        get_chart_of_accounts,
        methods=["GET"],
        name="get_chart_of_accounts",
    )
    app.add_api_route(
        f"{_BASE}/trial-balance",
        get_trial_balance,
        methods=["GET"],
        name="get_trial_balance",
    )
    app.add_api_route(
        f"{_BASE}/journal-entries",
        list_journal_entries,
        methods=["GET"],
        name="list_journal_entries",
    )
    app.add_api_route(
        f"{_BASE}/journal-entries/{{entry_id}}",
        get_journal_entry,
        methods=["GET"],
        name="get_journal_entry",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def get_ledger_service(
    session: AsyncSession = Depends(get_db_session),
) -> LedgerService:
    return build_ledger_service(session, AuditLog(SqlAuditRepository(session)))


async def get_chart_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ChartOfAccountsService:
    return build_chart_service(session, authorization, AuditLog(SqlAuditRepository(session)))


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _account_json(account: ChartAccount) -> dict[str, object]:
    return {
        "id": str(account.account_id),
        "code": account.code,
        "name": account.name,
        "account_type": account.account_type.value,
        "status": account.status.value,
        "rgs_code": account.rgs_code,
        "rgs_description_nl": account.rgs_description_nl,
        "rgs_description_en": account.rgs_description_en,
        "rgs_version": account.rgs_version,
        "default_vat_code": (account.default_vat_code.value if account.default_vat_code else None),
        "control_kind": account.control_kind.value if account.control_kind else None,
        "is_seeded": account.is_seeded,
    }


def _trial_balance_row_json(row: TrialBalanceRow) -> dict[str, object]:
    return {
        "account_id": str(row.account_id),
        "account_code": row.account_code,
        "account_name": row.account_name,
        "account_type": row.account_type.value,
        "total_debit": str(row.total_debit),
        "total_credit": str(row.total_credit),
        "balance": str(row.balance),
    }


def _summary_json(entry: JournalEntrySummary) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "entry_number": entry.entry_number,
        "entry_date": entry.entry_date.isoformat(),
        "description": entry.description,
        "document_reference": entry.document_reference,
        "journal_id": str(entry.journal_id),
        "journal_code": entry.journal_code,
        "journal_name": entry.journal_name,
        "period_id": str(entry.period_id),
        "fiscal_year_id": str(entry.fiscal_year_id),
        "posted_at": entry.posted_at.isoformat(),
        "posted_by_user_id": str(entry.posted_by_user_id) if entry.posted_by_user_id else None,
        "source_system": entry.source_system,
        "reverses_entry_id": str(entry.reverses_entry_id) if entry.reverses_entry_id else None,
        "total_debit": str(entry.total_debit),
        "total_credit": str(entry.total_credit),
        "line_count": entry.line_count,
    }


def _line_json(line: PostedLine) -> dict[str, object]:
    return {
        "id": str(line.id),
        "line_number": line.line_number,
        "account_id": str(line.account_id),
        "account_code": line.account_code,
        "account_name": line.account_name,
        "debit": str(line.debit),
        "credit": str(line.credit),
        "description": line.description,
        "subledger_party_id": str(line.subledger_party_id) if line.subledger_party_id else None,
        "cost_centre_id": str(line.cost_centre_id) if line.cost_centre_id else None,
    }


def _entry_json(entry: PostedEntry) -> dict[str, object]:
    total_debit = sum((line.debit for line in entry.lines), ZERO)
    total_credit = sum((line.credit for line in entry.lines), ZERO)
    return {
        "id": str(entry.id),
        "entry_number": entry.entry_number,
        "entry_date": entry.entry_date.isoformat(),
        "description": entry.description,
        "document_reference": entry.document_reference,
        "journal_id": str(entry.journal_id),
        "period_id": str(entry.period_id),
        "fiscal_year_id": str(entry.fiscal_year_id),
        "posted_at": entry.posted_at.isoformat(),
        "posted_by_user_id": str(entry.posted_by_user_id) if entry.posted_by_user_id else None,
        "source_system": entry.source_system,
        "reverses_entry_id": str(entry.reverses_entry_id) if entry.reverses_entry_id else None,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "lines": [_line_json(line) for line in entry.lines],
    }


async def _fiscal_year_of(
    request: Request,
    fiscal: FiscalYearService,
    *,
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> FiscalYear:
    """The year must be THIS administration's. `ledger.trial_balance` would
    otherwise answer a year of some other administration with every account
    at zero - a report that is wrong in a way nothing on the page says.
    Indistinguishable from "does not exist", the same posture
    errors.fiscal_year_not_found already takes for the dashboard.
    """
    try:
        years: Sequence[FiscalYear] = await fiscal.visible_years(
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def get_chart_of_accounts(
    administration_id: uuid.UUID,
    request: Request,
    include_blocked: bool = True,
    tenant: TenantContext = Depends(get_tenant_context),
    chart: ChartOfAccountsService = Depends(get_chart_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "chart_of_accounts",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    """FR-GL-005. Blocked accounts are included by default, for the reason
    ChartOfAccountsService.chart gives: they keep their history, and a chart
    that hid them would make last year's numbers unreadable.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        accounts = await chart.chart(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            include_blocked=include_blocked,
        )
    except ChartNotAuthorized as exc:
        raise problem(
            request,
            403,
            "errors.not_permitted",
            reason="no_matching_grant",
            action=exc.action,
            resource_type=exc.resource_type,
            detail=exc.detail,
        ) from exc
    return {
        "administration_id": str(administration_id),
        "accounts": [_account_json(account) for account in accounts],
    }


async def get_trial_balance(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    ledger: LedgerService = Depends(get_ledger_service),
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
    """FR-GL-001 as a report. `balanced` is computed here from the rows so a
    reader does not have to add two columns to see the invariant hold; it
    is true by construction (journal_entry_balanced_trg) and reported anyway,
    for the reason 0020 gives about its gap report.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    year = await _fiscal_year_of(
        request,
        fiscal,
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        actor_user_id=tenant.user_id,
    )
    rows = await ledger.trial_balance(
        administration_id=administration_id, fiscal_year_id=fiscal_year_id
    )
    total_debit = sum((row.total_debit for row in rows), ZERO)
    total_credit = sum((row.total_credit for row in rows), ZERO)
    return {
        "administration_id": str(administration_id),
        "fiscal_year": fiscal_year_json(year, today=date.today()),
        "rows": [_trial_balance_row_json(row) for row in rows],
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "balanced": total_debit == total_credit,
    }


async def list_journal_entries(
    administration_id: uuid.UUID,
    request: Request,
    fiscal_year_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    tenant: TenantContext = Depends(get_tenant_context),
    ledger: LedgerService = Depends(get_ledger_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    """The journal, newest posting first, one keyset page at a time - see
    api.ledger.model.EntryCursor for why not an offset. `next_cursor` is
    null on the last page.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    after: EntryCursor | None = None
    if cursor:
        try:
            after = EntryCursor.decode(cursor)
        except ValueError as exc:
            raise problem(request, 422, "errors.cursor_invalid", reason="invalid_cursor") from exc
    page = await ledger.entries(
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        after=after,
        limit=limit,
    )
    return {
        "administration_id": str(administration_id),
        "items": [_summary_json(entry) for entry in page.items],
        "next_cursor": page.next_cursor.encode() if page.next_cursor else None,
    }


async def get_journal_entry(
    administration_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    ledger: LedgerService = Depends(get_ledger_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    """One entry with its lines. The administration in the path must be the
    entry's own: RLS scopes `journal_entry` to the TENANT, and a firm engaged
    on two of a client's administrations holds grants per administration
    (IAM-107) - an entry reachable by id alone would let the one grant read
    the other's books.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    entry = await ledger.entry(entry_id)
    if entry is None or entry.administration_id != administration_id:
        raise problem(
            request, 404, "errors.journal_entry_not_found", reason="journal_entry_not_found"
        )
    return _entry_json(entry)

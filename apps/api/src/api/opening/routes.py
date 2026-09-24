"""The opening balance's HTTP surface (`/ledger/opening-balance` in the web app). ADR-088.

    GET  /v1/administrations/{id}/opening-balance?fiscal_year_id=   the accounts, and the posted one
    POST /v1/administrations/{id}/opening-balance                    post it

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

Posting goes through `LedgerService.post` - the ledger's one write path (non-negotiable #1) -
into the administration's `opening` journal, dated the fiscal year's first day, in its first
period. The permission is Appendix A's "Post journal entries"; reading is "view
chart_of_accounts", the permission the journal picker already uses. Amounts are decimal strings
(NFR-031).
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import get_fiscal_year_service
from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.auth.email_verification import require_verified_email
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.db import get_db_session
from api.i18n.http import problem
from api.ledger import FiscalYear, FiscalYearService, NotAuthorizedToDefineYear
from api.ledger.model import EntryInput, JournalType, LedgerError, LineInput
from api.ledger.periods import Period, build_period_service
from api.ledger.service import LedgerService, build_ledger_service
from api.opening.model import (
    OpeningAccount,
    OpeningBalanceError,
    OpeningLine,
    plan_opening,
)
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"

#: The code the opening journal gets when onboarding did not create one.
OPENING_JOURNAL_CODE = "BB"


def register(app: FastAPI) -> None:
    app.add_api_route(
        f"{_BASE}/opening-balance", get_opening_balance, methods=["GET"], name="get_opening_balance"
    )
    app.add_api_route(
        f"{_BASE}/opening-balance",
        post_opening_balance,
        methods=["POST"],
        name="post_opening_balance",
    )


# ---------------------------------------------------------------------------
# Reads - plain SELECTs; nothing here writes a posting table.
# ---------------------------------------------------------------------------


async def _accounts(
    session: AsyncSession, administration_id: uuid.UUID
) -> dict[uuid.UUID, OpeningAccount]:
    result = await session.execute(
        text(
            "SELECT id, code, name, account_type, control_kind, status FROM ledger_account "
            "WHERE administration_id = :admin ORDER BY code"
        ),
        {"admin": str(administration_id)},
    )
    return {
        row.id: OpeningAccount(
            id=row.id,
            code=row.code,
            name=row.name,
            account_type=row.account_type,
            control_kind=row.control_kind,
            status=row.status,
        )
        for row in result
    }


async def _posted_opening(
    session: AsyncSession, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
) -> uuid.UUID | None:
    """The year's opening entry that still stands: in an opening journal, not a reversal, and
    not reversed. Reversing it (Journal screen) is how an opening balance is redone."""
    result = await session.execute(
        text(
            "SELECT e.id FROM journal_entry e "
            "JOIN ledger_journal j ON j.id = e.journal_id "
            "WHERE e.administration_id = :admin AND e.fiscal_year_id = :year "
            "  AND j.journal_type = 'opening' AND e.reverses_entry_id IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM journal_entry r WHERE r.reverses_entry_id = e.id) "
            "ORDER BY e.entry_number DESC LIMIT 1"
        ),
        {"admin": str(administration_id), "year": str(fiscal_year_id)},
    )
    row = result.first()
    return None if row is None else row.id


async def _year(
    request: Request,
    fiscal: FiscalYearService,
    *,
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    user_id: uuid.UUID,
) -> FiscalYear:
    try:
        years = await fiscal.visible_years(
            administration_id=administration_id, actor_user_id=user_id
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


def _account_json(account: OpeningAccount) -> dict[str, object]:
    return {
        "id": str(account.id),
        "code": account.code,
        "name": account.name,
        "account_type": account.account_type,
    }


def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


async def _entry_json(ledger: LedgerService, entry_id: uuid.UUID) -> dict[str, object] | None:
    entry = await ledger.entry(entry_id)
    if entry is None:
        return None
    return {
        "id": str(entry.id),
        "entry_number": entry.entry_number,
        "entry_date": entry.entry_date.isoformat(),
        "lines": [
            {
                "account_id": str(line.account_id),
                "account_code": line.account_code,
                "account_name": line.account_name,
                "debit": str(line.debit),
                "credit": str(line.credit),
            }
            for line in entry.lines
        ],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def get_opening_balance(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "chart_of_accounts",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    year = await _year(
        request,
        fiscal,
        administration_id=administration_id,
        fiscal_year_id=fiscal_year_id,
        user_id=user_id,
    )
    accounts = await _accounts(session, administration_id)
    posted_id = await _posted_opening(session, administration_id, fiscal_year_id)
    ledger = build_ledger_service(session, AuditLog(SqlAuditRepository(session)))
    return {
        "fiscal_year_id": str(year.id),
        "entry_date": year.start_date.isoformat(),
        "accounts": [_account_json(a) for a in accounts.values() if a.eligible],
        "balance_accounts": [
            _account_json(a)
            for a in accounts.values()
            if a.account_type == "equity" and a.status == "active"
        ],
        "posted": await _entry_json(ledger, posted_id) if posted_id else None,
    }


class OpeningLineBody(BaseModel):
    account_id: uuid.UUID
    debit: str | None = None
    credit: str | None = None


class OpeningBody(BaseModel):
    fiscal_year_id: uuid.UUID
    lines: list[OpeningLineBody] = Field(default_factory=list)
    #: Where a difference goes; an equity account. Omitted, the lines must balance.
    balance_account_id: uuid.UUID | None = None


def _decimal(request: Request, value: str | None) -> Decimal:
    if value is None or not value.strip():
        return Decimal("0.00")
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise problem(
            request, 422, "errors.opening_amount_invalid", reason="opening_amount_invalid", code=""
        ) from exc


async def post_opening_balance(
    administration_id: uuid.UUID,
    body: OpeningBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    year = await _year(
        request,
        fiscal,
        administration_id=administration_id,
        fiscal_year_id=body.fiscal_year_id,
        user_id=user_id,
    )
    if await _posted_opening(session, administration_id, year.id) is not None:
        raise problem(
            request, 409, "errors.opening_already_posted", reason="opening_already_posted"
        )

    accounts = await _accounts(session, administration_id)
    try:
        planned = plan_opening(
            [
                OpeningLine(
                    account_id=line.account_id,
                    debit=_decimal(request, line.debit),
                    credit=_decimal(request, line.credit),
                )
                for line in body.lines
            ],
            accounts,
            balance_account_id=body.balance_account_id,
        )
    except OpeningBalanceError as exc:
        raise problem(
            request,
            422,
            f"errors.opening_{exc.reason}",
            reason=f"opening_{exc.reason}",
            code=exc.account_code or "",
            difference=exc.detail,
        ) from exc

    periods = build_period_service(session, authorization, AuditLog(SqlAuditRepository(session)))
    year_periods: list[Period] = sorted(
        await periods.periods_for_year(administration_id=administration_id, fiscal_year_id=year.id),
        key=lambda period: period.start_date,
    )
    first = year_periods[0] if year_periods else None
    if first is None or not first.is_open:
        raise problem(request, 409, "errors.opening_period_closed", reason="opening_period_closed")

    ledger = build_ledger_service(session, AuditLog(SqlAuditRepository(session)))
    journal = next(
        (
            j
            for j in await ledger.journals(administration_id=administration_id)
            if j.journal_type is JournalType.OPENING and j.status == "active"
        ),
        None,
    )
    if journal is None:
        journal = await ledger.create_journal(
            administration_id=administration_id,
            code=OPENING_JOURNAL_CODE,
            name="Beginbalans",
            journal_type=JournalType.OPENING,
        )

    try:
        posted = await ledger.post(
            EntryInput(
                administration_id=administration_id,
                journal_id=journal.id,
                period_id=first.id,
                entry_date=year.start_date,
                description=f"Beginbalans {year.start_date.year}",
                lines=[
                    LineInput(account_id=line.account_id, debit=line.debit, credit=line.credit)
                    for line in planned
                ],
                posted_by_user_id=user_id,
                source_system="opening_balance",
            ),
            actor_user_id=user_id,
        )
    except LedgerError as exc:
        raise problem(
            request,
            422,
            "errors.journal_entry_invalid",
            reason="journal_entry_invalid",
            detail=str(exc),
        ) from exc
    return {"posted": await _entry_json(ledger, posted.id)}

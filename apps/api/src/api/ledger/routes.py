"""The Journal screen's HTTP surface (`/journal` in the web app).

Everything `ledger_reads.routes` exposes is a read. This module is the
writes the engine already had and no route had ever reached:

    POST /v1/administrations/{id}/journal-entries              post a manual entry
    POST /v1/administrations/{id}/journal-entries/{eid}/reverse reverse a posting
    GET  /v1/administrations/{id}/journals                     pickable journals
    GET  /v1/administrations/{id}/periods                      a fiscal year's periods
    POST /v1/administrations/{id}/periods/{pid}/lock            close a period
    POST /v1/administrations/{id}/periods/{pid}/unlock          reopen a period

Registered via `register(app)`, not `include_router`, for the reason every
other module in this codebase gives: see `api.documents.routes.register`.

--- Two authorization checks on lock/unlock, and why that is not redundant ---

`api.ledger.periods.PeriodService` already checks `("lock", "period")` itself,
scoped to the SPECIFIC period (so a Bookkeeper's IAM-033 `period_ids`
condition binds - see its module docstring). But
`AuthorizationEnforcementMiddleware` and `tests/test_authz_coverage.py` both
require every route to declare a requirement of its own, and a route-level
`require_permission` cannot express "scoped to this one period" (that scope
API has no such target). So both routes ALSO declare the same
`(action, resource_type)` at the coarse administration scope. The two checks
cannot disagree in the caller's favour: PeriodService's is at least as
strict, so a caller who fails the coarse check never reaches it, and a caller
who fails PeriodService's period-scoped check was never let any further by
the coarse one having passed.

--- Amounts on the wire ---

Every debit/credit crosses as a Decimal-shaped STRING (NFR-031), the same
convention `api.ledger_reads.routes` and `api.customers.routes` follow.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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
from api.ledger.model import (
    EntryInput,
    Journal,
    LedgerError,
    LineInput,
    PostedEntry,
    PostedLine,
)
from api.ledger.periods import (
    HardLocked,
    NotAuthorized,
    Period,
    PeriodError,
    PeriodService,
    build_period_service,
)
from api.ledger.service import AlreadyReversed, LedgerService, build_ledger_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"


def register(app: FastAPI) -> None:
    app.add_api_route(f"{_BASE}/journals", list_journals, methods=["GET"], name="list_journals")
    app.add_api_route(
        f"{_BASE}/journal-entries",
        post_journal_entry,
        methods=["POST"],
        name="post_journal_entry",
    )
    app.add_api_route(
        f"{_BASE}/journal-entries/{{entry_id}}/reverse",
        reverse_journal_entry,
        methods=["POST"],
        name="reverse_journal_entry",
    )
    app.add_api_route(f"{_BASE}/periods", list_periods, methods=["GET"], name="list_periods")
    app.add_api_route(
        f"{_BASE}/periods/{{period_id}}/lock",
        lock_period,
        methods=["POST"],
        name="lock_period",
    )
    app.add_api_route(
        f"{_BASE}/periods/{{period_id}}/unlock",
        unlock_period,
        methods=["POST"],
        name="unlock_period",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def get_ledger_service(
    session: AsyncSession = Depends(get_db_session),
) -> LedgerService:
    return build_ledger_service(session, AuditLog(SqlAuditRepository(session)))


async def get_period_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> PeriodService:
    return build_period_service(session, authorization, AuditLog(SqlAuditRepository(session)))


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _journal_json(journal: Journal) -> dict[str, object]:
    return {
        "id": str(journal.id),
        "code": journal.code,
        "name": journal.name,
        "journal_type": journal.journal_type.value,
        "status": journal.status,
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
    }


def _entry_json(entry: PostedEntry) -> dict[str, object]:
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
        "reverses_entry_id": str(entry.reverses_entry_id) if entry.reverses_entry_id else None,
        "lines": [_line_json(line) for line in entry.lines],
    }


def _period_json(period: Period) -> dict[str, object]:
    return {
        "id": str(period.id),
        "fiscal_year_id": str(period.fiscal_year_id),
        "period_number": period.period_number,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "status": period.status.value,
        "locked_at": period.locked_at.isoformat() if period.locked_at else None,
        "locked_by_user_id": (str(period.locked_by_user_id) if period.locked_by_user_id else None),
        "filed_at": period.filed_at.isoformat() if period.filed_at else None,
        "filing_reference": period.filing_reference,
    }


def _parse_amount(request: Request, field: str, value: str | None) -> Decimal:
    """A required decimal-shaped string. '' and null both mean zero, which is
    what the empty side of a debit/credit line is (`LineInput` requires
    exactly one side non-zero, not both fields present).
    """
    if value is None or not value.strip():
        return Decimal("0.00")
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise problem(
            request,
            422,
            "errors.journal_field_invalid",
            reason="journal_field_invalid",
            field=field,
        ) from exc


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class JournalLineBody(BaseModel):
    account_id: str
    debit: str | None = None
    credit: str | None = None
    description: str | None = None


class PostJournalEntryBody(BaseModel):
    journal_id: str
    period_id: str
    entry_date: str
    description: str
    document_reference: str | None = None
    lines: list[JournalLineBody]


class ReverseJournalEntryBody(BaseModel):
    period_id: str
    entry_date: str
    description: str | None = None


class UnlockPeriodBody(BaseModel):
    reason: str


def _refuse(request: Request, exc: Exception) -> Exception:
    if isinstance(exc, AlreadyReversed):
        return problem(
            request,
            409,
            "errors.journal_entry_already_reversed",
            reason="journal_entry_already_reversed",
        )
    if isinstance(exc, HardLocked):
        return problem(request, 409, "errors.period_hard_locked", reason="period_hard_locked")
    if isinstance(exc, NotAuthorized):
        return problem(
            request,
            403,
            "errors.not_permitted",
            reason="no_matching_grant",
            action=exc.action,
            resource_type=exc.resource_type,
            detail=exc.detail,
        )
    if isinstance(exc, PeriodError):
        return problem(
            request, 422, "errors.period_invalid", reason="period_invalid", detail=str(exc)
        )
    if isinstance(exc, LedgerError):
        return problem(
            request,
            422,
            "errors.journal_entry_invalid",
            reason="journal_entry_invalid",
            detail=str(exc),
        )
    return exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def list_journals(
    administration_id: uuid.UUID,
    request: Request,
    ledger: LedgerService = Depends(get_ledger_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "chart_of_accounts",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    del request
    journals = await ledger.journals(administration_id=administration_id)
    return {"journals": [_journal_json(journal) for journal in journals]}


async def post_journal_entry(
    administration_id: uuid.UUID,
    body: PostJournalEntryBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    ledger: LedgerService = Depends(get_ledger_service),
    periods: PeriodService = Depends(get_period_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    """FR-GL-001/FR-GL-004: a memorial posting, typed by hand.

    `entry_date` is a plain string parsed here rather than a pydantic `date`,
    so a malformed date reaches `errors.journal_field_invalid` in the
    caller's language instead of FastAPI's own validation-error shape - the
    same choice `api.customers.routes._details` makes for `credit_limit`.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    try:
        entry_date = date.fromisoformat(body.entry_date)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.journal_field_invalid",
            reason="journal_field_invalid",
            field="entry_date",
        ) from exc

    try:
        journal_id = uuid.UUID(body.journal_id)
        period_id = uuid.UUID(body.period_id)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.journal_field_invalid",
            reason="journal_field_invalid",
            field="journal_id",
        ) from exc

    # Checked here for a clean, translated refusal; journal_entry_validate()
    # (0020/0021) is the actual guarantee and would otherwise surface this as
    # a raw database exception the client cannot render (see PeriodService's
    # module docstring: "if this file and the database ever disagree, the
    # database is right" - this is the fast, well-worded rejection, not it).
    target_period = await periods.period(period_id)
    if target_period is None or target_period.administration_id != administration_id:
        raise problem(request, 404, "errors.period_not_found", reason="period_not_found")
    if target_period.is_hard_locked:
        raise problem(request, 409, "errors.period_hard_locked", reason="period_hard_locked")
    if not target_period.is_open:
        raise problem(request, 409, "errors.period_invalid", reason="period_invalid")

    lines: list[LineInput] = []
    for index, line in enumerate(body.lines):
        try:
            account_id = uuid.UUID(line.account_id)
        except ValueError as exc:
            raise problem(
                request,
                422,
                "errors.journal_field_invalid",
                reason="journal_field_invalid",
                field=f"lines[{index}].account_id",
            ) from exc
        try:
            lines.append(
                LineInput(
                    account_id=account_id,
                    debit=_parse_amount(request, f"lines[{index}].debit", line.debit),
                    credit=_parse_amount(request, f"lines[{index}].credit", line.credit),
                    description=line.description,
                )
            )
        except LedgerError as exc:
            raise _refuse(request, exc) from exc

    try:
        entry = EntryInput(
            administration_id=administration_id,
            journal_id=journal_id,
            period_id=period_id,
            entry_date=entry_date,
            description=body.description,
            lines=lines,
            document_reference=body.document_reference,
            posted_by_user_id=tenant.user_id,
        )
    except LedgerError as exc:
        raise _refuse(request, exc) from exc

    try:
        posted = await ledger.post(entry, actor_user_id=tenant.user_id)
    except LedgerError as exc:
        raise _refuse(request, exc) from exc
    return _entry_json(posted)


async def reverse_journal_entry(
    administration_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: ReverseJournalEntryBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    ledger: LedgerService = Depends(get_ledger_service),
    periods: PeriodService = Depends(get_period_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reverse",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    """FR-GL-003. The entry named in the path must be this administration's -
    the same "indistinguishable from does not exist" posture
    `ledger_reads.get_journal_entry` takes, so a firm engaged on one of a
    client's administrations cannot reverse a posting in another.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    original = await ledger.entry(entry_id)
    if original is None or original.administration_id != administration_id:
        raise problem(
            request, 404, "errors.journal_entry_not_found", reason="journal_entry_not_found"
        )

    try:
        entry_date = date.fromisoformat(body.entry_date)
        period_id = uuid.UUID(body.period_id)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.journal_field_invalid",
            reason="journal_field_invalid",
            field="entry_date",
        ) from exc

    target_period = await periods.period(period_id)
    if target_period is None or target_period.administration_id != administration_id:
        raise problem(request, 404, "errors.period_not_found", reason="period_not_found")
    if target_period.is_hard_locked:
        raise problem(request, 409, "errors.period_hard_locked", reason="period_hard_locked")
    if not target_period.is_open:
        raise problem(request, 409, "errors.period_invalid", reason="period_invalid")

    description = body.description or f"Reversal of entry {original.entry_number}"

    try:
        reversal = await ledger.reverse(
            entry_id=entry_id,
            period_id=period_id,
            entry_date=entry_date,
            description=description,
            actor_user_id=tenant.user_id,
        )
    except LedgerError as exc:
        raise _refuse(request, exc) from exc
    return _entry_json(reversal)


async def list_periods(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    periods: PeriodService = Depends(get_period_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "chart_of_accounts",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    del request
    rows = await periods.periods_for_year(
        administration_id=administration_id, fiscal_year_id=fiscal_year_id
    )
    return {"periods": [_period_json(period) for period in rows]}


async def lock_period(
    administration_id: uuid.UUID,
    period_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    periods: PeriodService = Depends(get_period_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "lock",
            "period",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-GL-007. See the module docstring for why this route AND
    `PeriodService.lock` both check `("lock", "period")`.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    existing = await periods.period(period_id)
    if existing is None or existing.administration_id != administration_id:
        raise problem(request, 404, "errors.period_not_found", reason="period_not_found")
    try:
        locked = await periods.lock(period_id=period_id, user_id=tenant.user_id)
    except PeriodError as exc:
        raise _refuse(request, exc) from exc
    return _period_json(locked)


async def unlock_period(
    administration_id: uuid.UUID,
    period_id: uuid.UUID,
    body: UnlockPeriodBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    periods: PeriodService = Depends(get_period_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "lock",
            "period",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    existing = await periods.period(period_id)
    if existing is None or existing.administration_id != administration_id:
        raise problem(request, 404, "errors.period_not_found", reason="period_not_found")
    if not body.reason.strip():
        raise problem(
            request,
            422,
            "errors.period_unlock_reason_required",
            reason="period_unlock_reason_required",
        )
    try:
        unlocked = await periods.unlock(
            period_id=period_id, user_id=tenant.user_id, reason=body.reason
        )
    except PeriodError as exc:
        raise _refuse(request, exc) from exc
    return _period_json(unlocked)

"""The BTW screen's HTTP surface (`/vat` in the web app). See ADR-087.

    GET  /v1/administrations/{id}/vat-returns?fiscal_year_id=              every period's return
    GET  /v1/administrations/{id}/vat-returns/{period_id}                  one return, with checks
    GET  /v1/administrations/{id}/vat-returns/{period_id}/boxes/{code}/lines   FR-VAT-011
    POST /v1/administrations/{id}/vat-returns/{period_id}/file             file it

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

Reads ride `view vat_return` ("View filed returns": Owner, Accountant, Bookkeeper, Viewer) - a
prepared return is the same figures a filed one will be, and IAM-105 gives a client read access
to their filed returns. Filing is `file vat_return` (Owner and Accountant), checked here at the
administration and again by `PeriodService.mark_filed` scoped to the period (IAM-033), the same
double check `api.ledger.routes` explains for period locking.

Amounts are decimal strings (NFR-031).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import get_fiscal_year_service
from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.config import settings
from api.db import get_db_session
from api.i18n.http import problem
from api.ledger import FiscalYearService, NotAuthorizedToDefineYear
from api.ledger.periods import NotAuthorized, build_period_service
from api.tenancy import TenantContext, get_tenant_context
from api.vat.rules import VatRulesService
from api.vat.rules_repository import SqlVatRulesRepository
from api.vat_returns.filing import FilingRefused, build_filing_channel
from api.vat_returns.model import FORM_ORDER, BoxLine, Check
from api.vat_returns.repository import SqlVatReturnRepository
from api.vat_returns.service import (
    AlreadyFiled,
    FiguresChanged,
    NotFileable,
    PeriodNotFound,
    PeriodReturn,
    VatReturnService,
    WarningsNotAcknowledged,
    box_json,
)

_BASE = "/v1/administrations/{administration_id}"


def register(app: FastAPI) -> None:
    app.add_api_route(
        f"{_BASE}/vat-returns", list_vat_returns, methods=["GET"], name="list_vat_returns"
    )
    app.add_api_route(
        f"{_BASE}/vat-returns/{{period_id}}",
        get_vat_return,
        methods=["GET"],
        name="get_vat_return",
    )
    app.add_api_route(
        f"{_BASE}/vat-returns/{{period_id}}/boxes/{{code}}/lines",
        get_vat_box_lines,
        methods=["GET"],
        name="get_vat_box_lines",
    )
    app.add_api_route(
        f"{_BASE}/vat-returns/{{period_id}}/file",
        file_vat_return,
        methods=["POST"],
        name="file_vat_return",
    )


async def get_vat_return_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> VatReturnService:
    return VatReturnService(
        SqlVatReturnRepository(session),
        VatRulesService(SqlVatRulesRepository(session)),
        build_period_service(session, authorization, AuditLog(SqlAuditRepository(session))),
        build_filing_channel(settings.vat_filing_provider),
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _check_json(check: Check) -> dict[str, object]:
    return {
        "code": check.code.value,
        "severity": check.severity.value,
        "count": check.count,
        "amount": None if check.amount is None else str(check.amount),
        "detail": list(check.detail),
    }


def _return_json(row: PeriodReturn) -> dict[str, object]:
    period = row.period
    body: dict[str, object] = {
        "period_id": str(period.id),
        "fiscal_year_id": str(period.fiscal_year_id),
        "period_number": period.period_number,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "period_status": period.status.value,
        "due_date": row.due_date.isoformat(),
    }
    if row.filed is not None:
        filed = row.filed
        body.update(
            {
                "status": "filed",
                "boxes": filed.boxes,
                "output_vat": str(filed.output_vat),
                "input_vat": str(filed.input_vat),
                "total_due": str(filed.total_due),
                "exempt_turnover": None,
                "checks": [],
                "can_be_filed": False,
                "filed": {
                    "filed_at": filed.filed_at.isoformat(),
                    "filed_by_user_id": str(filed.filed_by_user_id),
                    "filing_channel": filed.filing_channel,
                    "filing_reference": filed.filing_reference,
                    "warnings_acknowledged": filed.warnings_acknowledged,
                    "ruleset_provisional": filed.ruleset_provisional,
                },
            }
        )
        return body
    prepared = row.prepared
    assert prepared is not None
    body.update(
        {
            "status": "ready" if prepared.can_be_filed else "open",
            "boxes": [box_json(box) for box in prepared.boxes],
            "output_vat": str(prepared.output_vat),
            "input_vat": str(prepared.input_vat),
            "total_due": str(prepared.total_due),
            "exempt_turnover": str(prepared.exempt_turnover),
            "checks": [_check_json(check) for check in prepared.checks],
            "can_be_filed": prepared.can_be_filed,
            "filed": None,
        }
    )
    return body


def _line_json(line: BoxLine) -> dict[str, object]:
    return {
        "entry_id": str(line.entry_id),
        "entry_number": line.entry_number,
        "entry_date": line.entry_date.isoformat(),
        "description": line.description,
        "document_reference": line.document_reference,
        "source_system": line.source_system,
        "account_code": line.account_code,
        "account_name": line.account_name,
        "vat_treatment": line.vat_treatment,
        "column": line.column,
        "amount": str(line.amount),
    }


def _not_found(request: Request) -> Exception:
    return problem(request, 404, "errors.vat_period_not_found", reason="vat_period_not_found")


def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def list_vat_returns(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: VatReturnService = Depends(get_vat_return_service),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "vat_return",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    # The year must be this administration's - a foreign year would list no periods, which
    # reads as "nothing to file" rather than "not yours" (the posture api.reports.routes takes).
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
    if not any(year.id == fiscal_year_id for year in years):
        raise problem(request, 404, "errors.fiscal_year_not_found", reason="fiscal_year_not_found")
    rows = await service.overview(
        administration_id=administration_id, fiscal_year_id=fiscal_year_id, today=date.today()
    )
    return {"returns": [_return_json(row) for row in rows]}


async def get_vat_return(
    administration_id: uuid.UUID,
    period_id: uuid.UUID,
    request: Request,
    service: VatReturnService = Depends(get_vat_return_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "vat_return",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    try:
        row = await service.prepare(
            administration_id=administration_id, period_id=period_id, today=date.today()
        )
    except PeriodNotFound as exc:
        raise _not_found(request) from exc
    return _return_json(row)


async def get_vat_box_lines(
    administration_id: uuid.UUID,
    period_id: uuid.UUID,
    code: str,
    request: Request,
    service: VatReturnService = Depends(get_vat_return_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "vat_return",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    if code not in FORM_ORDER:
        raise problem(request, 404, "errors.vat_box_unknown", reason="vat_box_unknown", code=code)
    try:
        lines = await service.box_lines(
            administration_id=administration_id, period_id=period_id, code=code
        )
    except PeriodNotFound as exc:
        raise _not_found(request) from exc
    return {"code": code, "lines": [_line_json(line) for line in lines]}


class FileBody(BaseModel):
    #: The reference the Belastingdienst gave back (manual channel). Optional: some filers
    #: record it later in their own files, and the lock and the stored figures stand without it.
    filing_reference: str | None = Field(default=None, max_length=200)
    #: The warning codes the filer saw and accepts (FR-VAT-002).
    acknowledged_warnings: list[str] = Field(default_factory=list)
    #: The total the filer reviewed, as a decimal string. Given, a different total refuses.
    expected_total: str | None = None


async def file_vat_return(
    administration_id: uuid.UUID,
    period_id: uuid.UUID,
    body: FileBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: VatReturnService = Depends(get_vat_return_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "file",
            "vat_return",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.FILING,
        )
    ),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    expected: Decimal | None = None
    if body.expected_total is not None and body.expected_total.strip():
        try:
            expected = Decimal(body.expected_total.strip())
        except InvalidOperation as exc:
            raise problem(
                request,
                422,
                "errors.vat_expected_total_invalid",
                reason="vat_expected_total_invalid",
            ) from exc
    try:
        row = await service.file(
            administration_id=administration_id,
            period_id=period_id,
            user_id=user_id,
            reference=body.filing_reference,
            acknowledged=body.acknowledged_warnings,
            expected_total=expected,
            today=date.today(),
        )
    except PeriodNotFound as exc:
        raise _not_found(request) from exc
    except AlreadyFiled as exc:
        raise problem(
            request, 409, "errors.vat_return_already_filed", reason="vat_return_already_filed"
        ) from exc
    except NotFileable as exc:
        raise problem(
            request,
            409,
            "errors.vat_return_not_fileable",
            reason="vat_return_not_fileable",
            checks=list(exc.codes),
        ) from exc
    except WarningsNotAcknowledged as exc:
        raise problem(
            request,
            409,
            "errors.vat_warnings_not_acknowledged",
            reason="vat_warnings_not_acknowledged",
            checks=list(exc.codes),
        ) from exc
    except FiguresChanged as exc:
        raise problem(
            request, 409, "errors.vat_figures_changed", reason="vat_figures_changed"
        ) from exc
    except FilingRefused as exc:
        raise problem(request, 422, f"errors.{exc.reason}", reason=exc.reason) from exc
    except NotAuthorized as exc:
        raise problem(
            request,
            403,
            "errors.not_permitted",
            reason="no_matching_grant",
            action=exc.action,
            resource_type=exc.resource_type,
            detail=exc.detail,
        ) from exc
    return _return_json(row)

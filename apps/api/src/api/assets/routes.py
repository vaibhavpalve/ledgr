"""The Assets screen's HTTP surface (`/assets` in the web app).

    POST /v1/administrations/{id}/assets
    GET  /v1/administrations/{id}/assets
    GET  /v1/administrations/{id}/assets/{asset_id}
    GET  /v1/administrations/{id}/assets/{asset_id}/depreciation-runs
    POST /v1/administrations/{id}/assets/{asset_id}/depreciate
    POST /v1/administrations/{id}/assets/{asset_id}/dispose

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register` for why.

Gated on `view fixed_asset` / `manage fixed_asset` (api.authz.matrix's
EXTENSION_CAPABILITIES, migration 0064). Every amount crosses the wire as a
Decimal-shaped STRING (NFR-031), the same convention every other module in
this codebase follows.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.assets.model import (
    AssetAlreadyDisposed,
    AssetDetails,
    AssetError,
    AssetNotFound,
    DepreciationAlreadyPosted,
    DepreciationRun,
    FixedAsset,
    FullyDepreciated,
    InvalidAssetField,
    NoOpenPeriod,
)
from api.assets.repository import SqlAssetRepository
from api.assets.service import AssetService
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
from api.ledger.periods import build_period_service
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"
_ASSETS = f"{_BASE}/assets"
_ASSET = f"{_ASSETS}/{{asset_id}}"


def register(app: FastAPI) -> None:
    app.add_api_route(_ASSETS, create_asset, methods=["POST"], name="create_asset")
    app.add_api_route(_ASSETS, list_assets, methods=["GET"], name="list_assets")
    app.add_api_route(_ASSET, get_asset, methods=["GET"], name="get_asset")
    app.add_api_route(
        f"{_ASSET}/depreciation-runs",
        list_depreciation_runs,
        methods=["GET"],
        name="list_depreciation_runs",
    )
    app.add_api_route(
        f"{_ASSET}/depreciate", depreciate_asset, methods=["POST"], name="depreciate_asset"
    )
    app.add_api_route(f"{_ASSET}/dispose", dispose_asset, methods=["POST"], name="dispose_asset")


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def get_asset_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> AssetService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return AssetService(
        SqlAssetRepository(session),
        build_ledger_service(session, audit_log),
        build_period_service(session, authorization, audit_log),
        audit_log,
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _asset_json(asset: FixedAsset) -> dict[str, object]:
    return {
        "id": str(asset.id),
        "name": asset.name,
        "category": asset.category,
        "acquisition_date": asset.acquisition_date.isoformat(),
        "acquisition_cost": str(asset.acquisition_cost),
        "residual_value": str(asset.residual_value),
        "useful_life_months": asset.useful_life_months,
        "depreciation_method": asset.depreciation_method.value,
        "asset_account_id": str(asset.asset_account_id),
        "depreciation_expense_account_id": str(asset.depreciation_expense_account_id),
        "accumulated_depreciation_account_id": str(asset.accumulated_depreciation_account_id),
        "status": asset.status.value,
        "disposal_date": asset.disposal_date.isoformat() if asset.disposal_date else None,
        "disposal_proceeds": (
            str(asset.disposal_proceeds) if asset.disposal_proceeds is not None else None
        ),
        "disposal_journal_entry_id": (
            str(asset.disposal_journal_entry_id) if asset.disposal_journal_entry_id else None
        ),
    }


def _run_json(run: DepreciationRun) -> dict[str, object]:
    return {
        "id": str(run.id),
        "fixed_asset_id": str(run.fixed_asset_id),
        "period_id": str(run.period_id),
        "amount": str(run.amount),
        "journal_entry_id": str(run.journal_entry_id),
        "posted_at": run.posted_at.isoformat(),
    }


def _parse_decimal(
    request: Request, field: str, value: str | None, *, default: str = "0"
) -> Decimal:
    raw = (value if value is not None else default).strip()
    if raw == "":
        raw = default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise problem(
            request, 422, "errors.asset_field_invalid", reason="asset_field_invalid", field=field
        ) from exc


def _parse_uuid(request: Request, field: str, value: str | None) -> uuid.UUID | None:
    if value is None or not value.strip():
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise problem(
            request, 422, "errors.asset_field_invalid", reason="asset_field_invalid", field=field
        ) from exc


def _refuse(request: Request, exc: Exception) -> Exception:
    if isinstance(exc, AssetNotFound):
        return problem(request, 404, "errors.asset_not_found", reason="asset_not_found")
    if isinstance(exc, AssetAlreadyDisposed):
        return problem(
            request, 409, "errors.asset_already_disposed", reason="asset_already_disposed"
        )
    if isinstance(exc, DepreciationAlreadyPosted):
        return problem(
            request,
            409,
            "errors.asset_depreciation_already_posted",
            reason="asset_depreciation_already_posted",
        )
    if isinstance(exc, FullyDepreciated):
        return problem(
            request, 409, "errors.asset_fully_depreciated", reason="asset_fully_depreciated"
        )
    if isinstance(exc, NoOpenPeriod):
        return problem(request, 409, "errors.period_invalid", reason="period_invalid")
    if isinstance(exc, InvalidAssetField):
        return problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field=exc.field,
        )
    if isinstance(exc, AssetError):
        return problem(request, 422, "errors.asset_field_invalid", reason="asset_field_invalid")
    return exc


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class CreateAssetBody(BaseModel):
    name: str
    category: str | None = None
    acquisition_date: str
    acquisition_cost: str
    residual_value: str = "0"
    useful_life_months: int
    asset_account_id: str
    depreciation_expense_account_id: str
    accumulated_depreciation_account_id: str


class DepreciateAssetBody(BaseModel):
    period_id: str


class DisposeAssetBody(BaseModel):
    period_id: str
    disposal_date: str
    proceeds: str = "0"
    proceeds_account_id: str | None = None
    gain_loss_account_id: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def create_asset(
    administration_id: uuid.UUID,
    body: CreateAssetBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "fixed_asset",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        acquisition_date = date.fromisoformat(body.acquisition_date)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field="acquisition_date",
        ) from exc

    asset_account_id = _parse_uuid(request, "asset_account_id", body.asset_account_id)
    expense_account_id = _parse_uuid(
        request, "depreciation_expense_account_id", body.depreciation_expense_account_id
    )
    accumulated_account_id = _parse_uuid(
        request, "accumulated_depreciation_account_id", body.accumulated_depreciation_account_id
    )
    if asset_account_id is None or expense_account_id is None or accumulated_account_id is None:
        raise problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field="asset_account_id",
        )

    try:
        details = AssetDetails(
            name=body.name,
            category=body.category,
            acquisition_date=acquisition_date,
            acquisition_cost=_parse_decimal(request, "acquisition_cost", body.acquisition_cost),
            residual_value=_parse_decimal(request, "residual_value", body.residual_value),
            useful_life_months=body.useful_life_months,
            asset_account_id=asset_account_id,
            depreciation_expense_account_id=expense_account_id,
            accumulated_depreciation_account_id=accumulated_account_id,
        )
        asset = await service.create(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            details=details,
        )
    except AssetError as exc:
        raise _refuse(request, exc) from exc
    return _asset_json(asset)


async def list_assets(
    administration_id: uuid.UUID,
    request: Request,
    include_disposed: bool = True,
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "fixed_asset", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    del request
    assets = await service.list(
        administration_id=administration_id, include_disposed=include_disposed
    )
    return {"assets": [_asset_json(asset) for asset in assets]}


async def get_asset(
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "fixed_asset", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    try:
        asset = await service.get(administration_id=administration_id, asset_id=asset_id)
    except AssetError as exc:
        raise _refuse(request, exc) from exc
    return _asset_json(asset)


async def list_depreciation_runs(
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
    request: Request,
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "fixed_asset", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    try:
        asset = await service.get(administration_id=administration_id, asset_id=asset_id)
    except AssetError as exc:
        raise _refuse(request, exc) from exc
    runs = await service.depreciation_runs(asset=asset)
    return {"runs": [_run_json(run) for run in runs]}


async def depreciate_asset(
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: DepreciateAssetBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "fixed_asset",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    period_id = _parse_uuid(request, "period_id", body.period_id)
    if period_id is None:
        raise problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field="period_id",
        )
    try:
        run = await service.depreciate(
            administration_id=administration_id,
            asset_id=asset_id,
            period_id=period_id,
            actor_user_id=tenant.user_id,
        )
    except AssetError as exc:
        raise _refuse(request, exc) from exc
    return _run_json(run)


async def dispose_asset(
    administration_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: DisposeAssetBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: AssetService = Depends(get_asset_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "fixed_asset",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    period_id = _parse_uuid(request, "period_id", body.period_id)
    if period_id is None:
        raise problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field="period_id",
        )
    try:
        disposal_date = date.fromisoformat(body.disposal_date)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.asset_field_invalid",
            reason="asset_field_invalid",
            field="disposal_date",
        ) from exc

    proceeds_account_id = _parse_uuid(request, "proceeds_account_id", body.proceeds_account_id)
    gain_loss_account_id = _parse_uuid(request, "gain_loss_account_id", body.gain_loss_account_id)

    try:
        asset = await service.dispose(
            administration_id=administration_id,
            asset_id=asset_id,
            period_id=period_id,
            disposal_date=disposal_date,
            proceeds=_parse_decimal(request, "proceeds", body.proceeds),
            proceeds_account_id=proceeds_account_id,
            gain_loss_account_id=gain_loss_account_id,
            actor_user_id=tenant.user_id,
        )
    except AssetError as exc:
        raise _refuse(request, exc) from exc
    return _asset_json(asset)

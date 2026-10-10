"""Receipt chasing's HTTP surface (firm home wave 2, decision 5; ADR-114).

    GET  /v1/administrations/{id}/missing-receipts   the client's list of lines missing a receipt
    GET  /v1/administrations/{id}/chase-setting      whether this client is chased, and how often
    POST /v1/administrations/{id}/chase-setting      opt in / out, cadence
    POST /v1/firm/chase/preview                      per ticked client: count, last chased, blocked
    POST /v1/firm/chase/send                         request a chase now for the unblocked ones

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

--- Permissions (all existing; none new) ---

* missing-receipts: `view bank_transaction`. The list shows bank lines (date, amount,
  counterparty, description) - exactly the Bank screen's data. Owner, Accountant, Bookkeeper and
  Viewer hold it; an Expense Submitter does not, and must not see the company's bank lines (§8.4).
* chase-setting GET: `view bank_transaction`, the same family: who may see the list may see
  whether it is being chased.
* chase-setting POST, preview, send: `reconcile bank_transaction`. Chasing exists to finish
  reconciling the bank, and these are the people who do that (Owner, Accountant, Bookkeeper) - the
  same permission the auto-bookings review decides with (ADR-110). A Viewer can read the list but
  cannot make the product e-mail the client.

The per-client routes declare `require_permission` on the path's administration; the two /v1/firm
routes name none and declare `require_portfolio_permission` (ADR-109), so an administration the
caller is not authorized on is never read - `send` reports it as `administration_not_found`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import administration_from_path, require_permission
from api.authz.model import AuthorizationDecision
from api.chasing.model import Cadence
from api.chasing.repository import ChaseSetting, SqlChaseRepository
from api.chasing.service import (
    MAX_ADMINISTRATIONS,
    ChaseService,
    PreviewItem,
    TooManyAdministrations,
)
from api.db import get_db_session
from api.firm.worklist_access import Portfolio, require_portfolio_permission
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

MISSING_RECEIPTS_PATH = "/v1/administrations/{administration_id}/missing-receipts"
CHASE_SETTING_PATH = "/v1/administrations/{administration_id}/chase-setting"
CHASE_PREVIEW_PATH = "/v1/firm/chase/preview"
CHASE_SEND_PATH = "/v1/firm/chase/send"


def register(app: FastAPI) -> None:
    app.add_api_route(
        MISSING_RECEIPTS_PATH, list_missing_receipts, methods=["GET"], name="list_missing_receipts"
    )
    app.add_api_route(
        CHASE_SETTING_PATH, get_chase_setting, methods=["GET"], name="get_chase_setting"
    )
    app.add_api_route(
        CHASE_SETTING_PATH, set_chase_setting, methods=["POST"], name="set_chase_setting"
    )
    app.add_api_route(CHASE_PREVIEW_PATH, preview_chase, methods=["POST"], name="preview_chase")
    app.add_api_route(CHASE_SEND_PATH, send_chase, methods=["POST"], name="send_chase")


async def get_chase_repository(
    session: AsyncSession = Depends(get_db_session),
) -> SqlChaseRepository:
    return SqlChaseRepository(session)


async def get_chase_service(
    session: AsyncSession = Depends(get_db_session),
) -> ChaseService:
    return ChaseService(SqlChaseRepository(session), AuditLog(SqlAuditRepository(session)))


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Declared requirements - built once at import
# ---------------------------------------------------------------------------

_READ_LIST = require_permission(
    "view", "bank_transaction", scope=administration_from_path("administration_id")
)
_WRITE_SETTING = require_permission(
    "reconcile",
    "bank_transaction",
    scope=administration_from_path("administration_id"),
    # IAM-090 has no "notification" category; turning on e-mails to a client changes how the
    # product acts on that tenant - a configuration change (ADR-111's reasoning for questions).
    audit=AuditCategory.CONFIGURATION,
)
# The preview is a POST only because it carries an id list; it changes nothing, but every POST
# declares a category (tests/test_audit_coverage.py). It reads per-client bank facts (counts).
_PREVIEW = require_portfolio_permission(
    "reconcile", "bank_transaction", audit=AuditCategory.FINANCIAL_READ
)
_SEND = require_portfolio_permission(
    "reconcile", "bank_transaction", audit=AuditCategory.CONFIGURATION
)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _setting_json(setting: ChaseSetting) -> dict[str, object]:
    return {
        "enabled": setting.enabled,
        "cadence": setting.cadence.value,
        "set_at": setting.set_at.isoformat() if setting.set_at else None,
    }


def _preview_item_json(item: PreviewItem) -> dict[str, object]:
    return {
        "administration_id": str(item.administration_id),
        "display_name": item.display_name,
        "missing_count": item.missing_count,
        # None: not counted yet - the sweep resolves the recipients when it delivers.
        "recipient_count": item.recipient_count,
        "last_chased_at": item.last_chased_at.isoformat() if item.last_chased_at else None,
        "blocked_reason": item.blocked_reason.value if item.blocked_reason else None,
    }


def _user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


def _too_many(request: Request) -> Exception:
    return problem(
        request,
        422,
        "errors.chase_too_many_administrations",
        reason="chase_too_many_administrations",
        max=MAX_ADMINISTRATIONS,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def list_missing_receipts(
    administration_id: uuid.UUID,
    repository: SqlChaseRepository = Depends(get_chase_repository),
    _: AuthorizationDecision = Depends(_READ_LIST),
) -> dict[str, object]:
    """The lines the client is asked for, newest first (up to 500; `count` is the full number).
    Money is a decimal string (NFR-031)."""
    lines = await repository.missing_lines(administration_id)
    count = await repository.missing_count(administration_id)
    return {
        "items": [
            {
                "bank_transaction_id": str(line.bank_transaction_id),
                "booking_date": line.booking_date.isoformat(),
                "amount": str(line.amount),
                "counterparty": line.counterparty,
                "description": line.description,
            }
            for line in lines
        ],
        "count": count,
    }


async def get_chase_setting(
    administration_id: uuid.UUID,
    repository: SqlChaseRepository = Depends(get_chase_repository),
    _: AuthorizationDecision = Depends(_READ_LIST),
) -> dict[str, object]:
    setting = await repository.setting(administration_id)
    facts = (await repository.facts([administration_id])).get(administration_id)
    last = facts.last_chased_at if facts is not None else None
    return {**_setting_json(setting), "last_chased_at": last.isoformat() if last else None}


class ChaseSettingBody(BaseModel):
    enabled: bool
    cadence: Literal["weekly", "fortnightly"] = "weekly"


async def set_chase_setting(
    administration_id: uuid.UUID,
    payload: ChaseSettingBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    repository: SqlChaseRepository = Depends(get_chase_repository),
    _: AuthorizationDecision = Depends(_WRITE_SETTING),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    setting = await repository.record_setting(
        administration_id,
        enabled=payload.enabled,
        cadence=Cadence(payload.cadence),
        user_id=user_id,
    )
    if setting is None:  # pragma: no cover - require_permission already resolved it
        raise problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        )
    return _setting_json(setting)


class ChaseBatchBody(BaseModel):
    administration_ids: list[uuid.UUID]


async def preview_chase(
    payload: ChaseBatchBody,
    request: Request,
    service: ChaseService = Depends(get_chase_service),
    portfolio: Portfolio = Depends(_PREVIEW),
) -> dict[str, object]:
    try:
        items = await service.preview(
            requested=payload.administration_ids, portfolio=portfolio.names(), now=_now()
        )
    except TooManyAdministrations as exc:
        raise _too_many(request) from exc
    return {"items": [_preview_item_json(item) for item in items]}


async def send_chase(
    payload: ChaseBatchBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ChaseService = Depends(get_chase_service),
    portfolio: Portfolio = Depends(_SEND),
) -> dict[str, object]:
    """Accepts the request for every unblocked client; the sweep e-mails within minutes. 200 with
    a per-client `skipped[]`, never a 4xx for one client's reason."""
    user_id = _user(request, tenant)
    try:
        outcome = await service.request(
            requested=payload.administration_ids,
            portfolio=portfolio.names(),
            user_id=user_id,
            acting_organization_id=tenant.organization_id,
            now=_now(),
        )
    except TooManyAdministrations as exc:
        raise _too_many(request) from exc
    return {
        "sent": outcome.sent,
        "skipped": [
            {"administration_id": str(s.administration_id), "reason": s.reason}
            for s in outcome.skipped
        ],
    }

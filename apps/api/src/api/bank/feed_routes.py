"""The live bank feed's HTTP surface (ADR-108).

    GET  /v1/administrations/{id}/bank-accounts/{account_id}/feed
    GET  /v1/administrations/{id}/bank-feed/institutions?country=NL
    POST /v1/administrations/{id}/bank-accounts/{account_id}/feed/connect
    POST /v1/administrations/{id}/bank-feed/connections/{connection_id}/complete
    POST /v1/administrations/{id}/bank-accounts/{account_id}/feed/sync
    POST /v1/administrations/{id}/bank-accounts/{account_id}/feed/disconnect

--- Permissions: none new ---

The authorization matrix already has "Connect / revoke bank consent" (`manage bank_consent`):
connect, complete and disconnect ride it. Sync imports lines, so it takes the statement import's
own permission (`reconcile bank_transaction`); the reads take `view bank_transaction`.

--- Outcomes are answers, not errors ---

A consent the bank refused, an account that does not match, an expired consent or a provider that
is down are recorded on the connection and returned with 200: the request's transaction has to
commit for that state to be kept. The screen reads `status` and `last_error`. Only refusals that
change nothing (no provider configured, nothing linked, an unknown connection) are 4xx.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.authz.dependencies import administration_from_path, require_permission
from api.authz.model import AuthorizationDecision
from api.bank.adapters import BankFeedProvider, Institution, build_bank_feed_provider
from api.bank.compose import build_bank_feed_service
from api.bank.feed import (
    BankFeedService,
    FeedConnectionNotFound,
    FeedError,
    FeedSettings,
    FeedStatus,
)
from api.bank.feed_repository import FeedConnection
from api.bank.model import BankAccountNotFound, BankError
from api.bank.routes import _import_json, get_bank_service
from api.bank.service import BankService
from api.config import settings
from api.db import get_db_session
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"
_ACCOUNT_FEED = f"{_BASE}/bank-accounts/{{bank_account_id}}/feed"
_FEED = f"{_BASE}/bank-feed"


def register(app: FastAPI) -> None:
    app.add_api_route(_ACCOUNT_FEED, get_feed, methods=["GET"], name="get_bank_feed")
    app.add_api_route(
        f"{_FEED}/institutions",
        list_institutions,
        methods=["GET"],
        name="list_bank_feed_institutions",
    )
    app.add_api_route(
        f"{_ACCOUNT_FEED}/connect", connect_feed, methods=["POST"], name="connect_bank_feed"
    )
    app.add_api_route(
        f"{_FEED}/connections/{{connection_id}}/complete",
        complete_feed,
        methods=["POST"],
        name="complete_bank_feed",
    )
    app.add_api_route(f"{_ACCOUNT_FEED}/sync", sync_feed, methods=["POST"], name="sync_bank_feed")
    app.add_api_route(
        f"{_ACCOUNT_FEED}/disconnect",
        disconnect_feed,
        methods=["POST"],
        name="disconnect_bank_feed",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def configured_provider() -> BankFeedProvider:
    # One instance per process, so the provider's access token is reused between requests.
    return build_bank_feed_provider(
        settings.bank_feed_provider,
        secret_id=settings.gocardless_secret_id,
        secret_key=settings.gocardless_secret_key,
        base_url=settings.gocardless_base_url,
        timeout_seconds=settings.bank_feed_timeout_seconds,
    )


def get_bank_feed_provider() -> BankFeedProvider:
    """A dependency, so a test can put a fake provider in its place."""
    return configured_provider()


def feed_settings() -> FeedSettings:
    return FeedSettings(
        # The bank sends the person back here; the provider appends ?ref=<connection id>.
        redirect_url=f"{settings.app_base_url.rstrip('/')}/bank/feed-return",
        history_days=settings.bank_feed_history_days,
        consent_days=settings.bank_feed_consent_days,
    )


async def get_bank_feed_service(
    session: AsyncSession = Depends(get_db_session),
    bank: BankService = Depends(get_bank_service),
    provider: BankFeedProvider = Depends(get_bank_feed_provider),
) -> BankFeedService:
    return build_bank_feed_service(session, bank=bank, provider=provider, settings=feed_settings())


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def connection_json(connection: FeedConnection) -> dict[str, object]:
    return {
        "id": str(connection.id),
        "bank_account_id": str(connection.bank_account_id),
        "provider": connection.provider,
        "institution_id": connection.institution_id,
        "institution_name": connection.institution_name,
        "status": connection.status.value,
        "consent_expires_at": (
            connection.consent_expires_at.isoformat() if connection.consent_expires_at else None
        ),
        "last_synced_at": (
            connection.last_synced_at.isoformat() if connection.last_synced_at else None
        ),
        "last_error": connection.last_error,
    }


def _status_json(status: FeedStatus) -> dict[str, object]:
    return {
        "configured": status.configured,
        "provider": status.provider,
        "connection": connection_json(status.connection) if status.connection else None,
    }


def _institution_json(institution: Institution) -> dict[str, object]:
    return {
        "id": institution.id,
        "name": institution.name,
        "bic": institution.bic,
        "logo": institution.logo,
    }


def _refuse(request: Request, exc: Exception) -> Exception:
    if isinstance(exc, BankAccountNotFound):
        return problem(
            request, 404, "errors.bank_account_not_found", reason="bank_account_not_found"
        )
    if isinstance(exc, FeedConnectionNotFound):
        return problem(request, 404, f"errors.{exc.reason}", reason=exc.reason)
    if isinstance(exc, FeedError):
        status = {
            "bank_feed_not_configured": 409,
            "bank_feed_already_linked": 409,
            "bank_feed_not_linked": 409,
            "bank_feed_provider_unavailable": 502,
            "bank_feed_consent_failed": 422,
        }.get(exc.reason, 422)
        return problem(request, status, f"errors.{exc.reason}", reason=exc.reason)
    if isinstance(exc, BankError):
        return problem(request, 422, "errors.bank_field_invalid", reason="bank_field_invalid")
    return exc


def _user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class ConnectBody(BaseModel):
    institution_id: str
    institution_name: str | None = None
    #: The language the bank's consent pages open in.
    language: str = "nl"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def get_feed(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    request: Request,
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    try:
        status = await service.status(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return _status_json(status)


async def list_institutions(
    administration_id: uuid.UUID,
    request: Request,
    country: str = "NL",
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    del administration_id
    if len(country) != 2 or not country.isalpha():
        raise problem(
            request, 422, "errors.bank_field_invalid", reason="bank_field_invalid", field="country"
        )
    try:
        institutions = await service.institutions(country=country.upper())
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return {"institutions": [_institution_json(item) for item in institutions]}


async def connect_feed(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    body: ConnectBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "bank_consent",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    if not body.institution_id.strip():
        raise problem(
            request,
            422,
            "errors.bank_field_invalid",
            reason="bank_field_invalid",
            field="institution_id",
        )
    try:
        started = await service.connect(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            institution_id=body.institution_id.strip(),
            institution_name=body.institution_name,
            language="en" if body.language == "en" else "nl",
            actor_user_id=user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return {"connection": connection_json(started.connection), "link": started.link}


async def complete_feed(
    administration_id: uuid.UUID,
    connection_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "bank_consent",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    try:
        connection = await service.complete(
            administration_id=administration_id,
            connection_id=connection_id,
            actor_user_id=user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return {"connection": connection_json(connection)}


async def sync_feed(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reconcile",
            "bank_transaction",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    try:
        result = await service.sync(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            actor_user_id=user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return {
        "connection": connection_json(result.connection),
        "imported": _import_json(result.imported) if result.imported else None,
    }


async def disconnect_feed(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankFeedService = Depends(get_bank_feed_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "bank_consent",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    try:
        connection = await service.disconnect(
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            actor_user_id=user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return {"connection": connection_json(connection)}

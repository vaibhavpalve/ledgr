"""Tenant context propagation from the HTTP edge to the database session.

Implements CLAUDE.md's third architectural non-negotiable: every request
carries tenant context from edge to database; a request without tenant
context fails closed.

TenantContextMiddleware is ASGI middleware, not a FastAPI dependency. That
distinction is the whole point: a dependency only runs for routes that
declare it, so a route someone forgets to annotate would be a silent gap.
Middleware wraps every request before FastAPI's routing even resolves which
handler applies, so there is no route-level opt-out — a request either
carries verified tenant context or it never reaches a handler at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from api.config import settings
from api.i18n.http import message

# Infrastructure endpoints only. Anything not listed here is required to
# carry verified tenant context before it can reach a route handler.
EXEMPT_PATHS = frozenset({"/health"})


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: uuid.UUID
    user_id: uuid.UUID | None = None
    # IAM-011: whether THIS request's principal already had a second
    # factor verified - read by api.mfa_middleware.MfaEnforcementMiddleware,
    # not enforced here. Defaults to False (fail closed): a token that
    # doesn't carry the claim at all is treated as MFA-unverified, never
    # as "trust it." This is the same placeholder-claim mechanism the rest
    # of this module already uses for org_id/sub - see the module
    # docstring and docs/decisions/ADR-008-mfa-policy.md for what a real
    # session integration would populate this from
    # (Session.mfa_verified_at).
    mfa_verified: bool = False
    # IAM-110: which session this request belongs to, so the administration
    # switcher can record its active administration on the right row. Read
    # from a `sid` claim, and None when the token carries none - the same
    # placeholder-claim mechanism as org_id/sub above. A route that needs it
    # (api.main's switch endpoint) fails closed rather than guessing which of
    # the user's sessions to write to.
    session_id: uuid.UUID | None = None
    # FR-FRM-000a: the client this session currently has open - the one whose
    # name and colour the header is showing. Read from an `adm` claim, the
    # same placeholder mechanism as mfa_verified/sid above; a real session
    # integration reads it from sessions.active_administration_id (0017),
    # which the switcher writes.
    #
    # None means "not inside any client" - a session at the switcher, or a
    # non-interactive API client - and constrains nothing. That is not a
    # bypass: the active-client guard is a wrong-client safety control for
    # the first-party UI, not an authorization control. Whether a caller may
    # touch an administration at all is already settled by authorize(), and
    # omitting the claim gains them nothing there.
    active_administration_id: uuid.UUID | None = None


class MissingTenantContextError(Exception):
    """Raised when a request has no valid, verified tenant context."""


def _extract_tenant_context(request: Request) -> TenantContext:
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise MissingTenantContextError("missing bearer token")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise MissingTenantContextError("empty bearer token")

    try:
        claims = jwt.decode(token, settings.jwt_signing_key, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise MissingTenantContextError(f"invalid token: {exc}") from exc

    org_claim = claims.get("org_id")
    if not org_claim:
        raise MissingTenantContextError("token carries no org_id claim")

    try:
        organization_id = uuid.UUID(str(org_claim))
    except ValueError as exc:
        raise MissingTenantContextError("org_id claim is not a valid UUID") from exc

    user_id: uuid.UUID | None = None
    user_claim = claims.get("sub")
    if user_claim:
        try:
            user_id = uuid.UUID(str(user_claim))
        except ValueError as exc:
            raise MissingTenantContextError("sub claim is not a valid UUID") from exc

    mfa_verified = claims.get("mfa_verified") is True

    session_id: uuid.UUID | None = None
    session_claim = claims.get("sid")
    if session_claim:
        try:
            session_id = uuid.UUID(str(session_claim))
        except ValueError as exc:
            raise MissingTenantContextError("sid claim is not a valid UUID") from exc

    active_administration_id: uuid.UUID | None = None
    active_claim = claims.get("adm")
    if active_claim:
        try:
            active_administration_id = uuid.UUID(str(active_claim))
        except ValueError as exc:
            raise MissingTenantContextError("adm claim is not a valid UUID") from exc

    return TenantContext(
        organization_id=organization_id,
        user_id=user_id,
        mfa_verified=mfa_verified,
        session_id=session_id,
        active_administration_id=active_administration_id,
    )


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Resolves and verifies tenant context for every non-exempt request.

    A request that fails verification gets a 401 directly from here and
    `call_next` is never invoked — the handler for that route does not run.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        try:
            tenant = _extract_tenant_context(request)
        except MissingTenantContextError as exc:
            # FR-UX-007: the sentence is in the caller's language, the reason
            # is not. `Accept-Language` is readable here even though nothing
            # has authenticated - which is the point, and the same reason
            # IAM-010g puts a language control on the login screen: knowing
            # which of two languages to answer in never required a token.
            #
            # This is the earliest gate in the chain, so it is the one a person
            # meets when their session runs out mid-task. `str(exc)` stays in
            # `reason` where a client and a log can use it, and out of the
            # sentence, where "token carries no org_id claim" would be exactly
            # the developer-facing string FR-UX-007 forbids.
            return JSONResponse(
                status_code=401,
                content={
                    "detail": message(request, "errors.tenant_context_required"),
                    "reason": str(exc),
                },
            )

        request.state.tenant_context = tenant
        return await call_next(request)


def get_tenant_context(request: Request) -> TenantContext:
    """FastAPI dependency exposing the tenant context TenantContextMiddleware
    already verified. This does not itself enforce anything — by the time a
    handler can run, the middleware has already rejected any request without
    a valid tenant. It exists so handlers and the DB session dependency have
    a typed way to read what the middleware resolved.
    """
    tenant = getattr(request.state, "tenant_context", None)
    if not isinstance(tenant, TenantContext):
        # Only reachable if a path is both in EXEMPT_PATHS and declares this
        # dependency — a routing misconfiguration, not a runtime gap in
        # tenant enforcement. Fail loudly rather than proceed with no tenant.
        raise MissingTenantContextError(
            "get_tenant_context used on a route with no tenant context "
            "(is this path incorrectly listed in EXEMPT_PATHS?)"
        )
    return tenant

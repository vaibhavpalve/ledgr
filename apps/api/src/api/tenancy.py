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

--- The token proves the caller; the session row says whether they are still in ---

A bearer JWT (api.auth.tokens) is signed by this server and names a user
(`sub`), an organization (`org_id`) and a session (`sid`). The signature is
what makes the first two trustworthy. The session's STATE - revoked, expired,
idle past IAM-016's timeout, MFA verified, which administration is open - is
deliberately not trusted from the token, because a token cannot be told
about anything that happened after it was minted. Every non-exempt request
therefore resolves `sid` against `sessions` (one primary-key read, one
last-active touch) and takes those facts from the row. A session revoked on
one device is refused on the very next request from any other; a client
switch (PUT /v1/switcher/{id}) is visible on the next request with no token
re-mint. See docs/decisions/ADR-060-session-backed-tenant-context.md, the
follow-up ADR-054 named when it shipped the restated-claims token.

The lookup is a SessionValidator (api.auth.session_validation) handed to the
middleware at construction, installed on `app.state.session_validator`, or
built from the shared engine when neither is present. The unit suite
installs an in-memory validator so it runs the real revocation/expiry/idle
logic without Postgres; the DB-backed suite runs the SQL one. A validator
that cannot be reached is an error, never a pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt
from fastapi import Request
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from api.auth.sessions import (
    SessionError,
    SessionExpiredError,
    SessionIdleTimeoutError,
    SessionNotFoundError,
    SessionRevokedError,
    SessionUserMismatchError,
)
from api.config import settings
from api.i18n.http import message

if TYPE_CHECKING:
    from api.auth.session_validation import SessionValidator

# Infrastructure endpoints, plus the sign-up/sign-in paths that ESTABLISH
# tenant context rather than presenting one - a request has to be able to
# reach `/v1/auth/signup` and `/v1/auth/login` with no bearer token at all,
# since proving who you are is what those endpoints exist to do. Defined
# here (the canonical "needs no tenant context" list) rather than imported
# from api.auth.routes, which imports FROM this module - api.auth.routes
# re-exports this same frozenset as NO_TENANT_PATHS rather than defining its
# own, so there is one list, not two that could drift.
#
# Anything not listed here is required to carry verified tenant context
# before it can reach a route handler.
#
#   /v1/auth/verify-email
#       IAM-010b. The link in the e-mail is opened wherever the mail is
#       read - a phone with no LEDGR session, a different browser - and the
#       single-use token in the body is the proof, not a bearer token. The
#       resend endpoint is NOT here: asking for a new link is something the
#       signed-in account holder does.
#   /v1/dev/outbox
#       Only registered when settings.expose_dev_outbox is true (never the
#       default - see api.mail.dev_outbox), and exempt only then, so the
#       exemption cannot outlive the route. Local development reads the
#       verification link off it instead of a mailbox.
EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/v1/auth/signup",
        "/v1/auth/signup/google",
        "/v1/auth/login",
        "/v1/auth/login/passkey/begin",
        "/v1/auth/login/passkey/finish",
        "/v1/auth/login/google/start",
        "/v1/auth/login/google/callback",
        "/v1/auth/verify-email",
    }
    | ({"/v1/dev/outbox"} if settings.expose_dev_outbox else set())
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: uuid.UUID
    user_id: uuid.UUID | None = None
    # IAM-011: whether THIS request's session has had a second factor
    # verified - read by api.mfa_middleware.MfaEnforcementMiddleware, not
    # enforced here. Taken from sessions.mfa_verified_at on every request,
    # never from the token: a step-up that succeeded a moment ago is visible
    # immediately, and a token claiming `mfa_verified: true` for a session
    # whose row says otherwise is not believed. Defaults to False (fail
    # closed) for a context built any other way.
    mfa_verified: bool = False
    # IAM-110: which session this request belongs to - the row the
    # administration switcher writes to, and the row everything above was
    # read from. Always set for a request that came through
    # TenantContextMiddleware: a token that names no session is refused
    # there. Optional on the dataclass only so a test can build a context
    # by hand; a route that needs it (api.main's switch endpoint) still
    # fails closed on None rather than guessing.
    session_id: uuid.UUID | None = None
    # FR-FRM-000a: the client this session currently has open - the one whose
    # name and colour the header is showing. Read from
    # sessions.active_administration_id (0017), which the switcher writes, on
    # every request - so a switch takes effect on the next request without a
    # new token, and no token claim can assert a client the session is not
    # actually in.
    #
    # None means "not inside any client" - a session at the switcher, or a
    # non-interactive API client - and constrains nothing. That is not a
    # bypass: the active-client guard is a wrong-client safety control for
    # the first-party UI, not an authorization control. Whether a caller may
    # touch an administration at all is already settled by authorize().
    active_administration_id: uuid.UUID | None = None


class MissingTenantContextError(Exception):
    """Raised when a request has no valid, verified tenant context."""


@dataclass(frozen=True, slots=True)
class _TokenClaims:
    """What the signed JWT vouches for. Everything else comes from the row."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID


def _decode_claims(request: Request) -> _TokenClaims:
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

    # `sub` and `sid` are both required. A token naming no user has nobody
    # to evaluate MFA or permissions for, and a token naming no session has
    # no row to be revoked from - either would be a request that outlives
    # every control this middleware exists to apply. Neither is a token
    # api.auth.tokens ever mints; refusing them costs nothing legitimate.
    user_claim = claims.get("sub")
    if not user_claim:
        raise MissingTenantContextError("token carries no sub claim")
    try:
        user_id = uuid.UUID(str(user_claim))
    except ValueError as exc:
        raise MissingTenantContextError("sub claim is not a valid UUID") from exc

    session_claim = claims.get("sid")
    if not session_claim:
        raise MissingTenantContextError("token carries no sid claim")
    try:
        session_id = uuid.UUID(str(session_claim))
    except ValueError as exc:
        raise MissingTenantContextError("sid claim is not a valid UUID") from exc

    return _TokenClaims(organization_id=organization_id, user_id=user_id, session_id=session_id)


# The `reason` a client or a log sees for each way a session can stop being
# live. Stable tokens, never translated (FR-UX-007); the sentence beside them
# is the same one for all of them, because what the person does next - sign
# in again - is the same too.
_SESSION_REASONS: dict[type[SessionError], str] = {
    SessionNotFoundError: "session_not_found",
    SessionRevokedError: "session_revoked",
    SessionExpiredError: "session_expired",
    SessionIdleTimeoutError: "session_idle_timeout",
    SessionUserMismatchError: "session_user_mismatch",
}


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Resolves and verifies tenant context for every non-exempt request.

    A request that fails verification gets a 401 directly from here and
    `call_next` is never invoked — the handler for that route does not run.
    """

    def __init__(
        self,
        app: ASGIApp | Starlette,
        *,
        session_validator: SessionValidator | None = None,
    ) -> None:
        super().__init__(app)
        # Injectable, the same way api.mfa_middleware takes its enrolment
        # checker: a test hands in tests/support/fake_session_validator's
        # in-memory validator and the middleware runs the real
        # revocation/expiry/idle logic against it. Production passes nothing
        # and gets the SQL-backed one, built once on first use (lazily, to
        # avoid the api.db <-> api.tenancy import cycle - see
        # api.auth.session_validation).
        self._session_validator = session_validator

    def _validator_for(self, request: Request) -> SessionValidator:
        # app.state wins over the constructor argument so a test against the
        # REAL api.main app - whose middleware stack is already built - can
        # still substitute the lookup without rebuilding the app. Nothing in
        # production sets it.
        installed = getattr(request.app.state, "session_validator", None)
        if installed is not None:
            validator: SessionValidator = installed
            return validator
        if self._session_validator is None:
            from api.auth.session_validation import build_session_validator

            self._session_validator = build_session_validator()
        return self._session_validator

    async def _resolve(self, request: Request) -> TenantContext:
        claims = _decode_claims(request)
        try:
            session = await self._validator_for(request).validate(
                claims.session_id, user_id=claims.user_id
            )
        except SessionError as exc:
            raise MissingTenantContextError(
                _SESSION_REASONS.get(type(exc), "session_invalid")
            ) from exc

        return TenantContext(
            organization_id=claims.organization_id,
            user_id=claims.user_id,
            mfa_verified=session.mfa_verified,
            session_id=session.id,
            active_administration_id=session.active_administration_id,
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        try:
            tenant = await self._resolve(request)
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

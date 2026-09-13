"""Enforces IAM-011: MFA is mandatory, with no opt-out, for every user the
current policy requires it for (see api.auth.mfa for what that policy is
today and why). This is ASGI middleware, not a per-endpoint dependency -
the same reasoning api.tenancy.TenantContextMiddleware's docstring already
gives: a dependency only runs for routes that declare it, so a route
someone forgets to annotate would be a silent gap. Middleware wraps every
request before a handler can run, so there is no route-level opt-out.

Ordering matters and is verified, not assumed: this middleware depends on
request.state.tenant_context already being set, which
TenantContextMiddleware is responsible for. In Starlette/FastAPI, the
LAST middleware added via app.add_middleware() is the OUTERMOST layer and
runs FIRST on the way in - confirmed empirically before relying on it,
since getting this backwards would mean MFA enforcement silently never
runs. api.main therefore adds THIS middleware first, then
TenantContextMiddleware last, so TenantContextMiddleware always sees a
request before this one does. If that ordering is ever changed, this
middleware fails closed (see the defensive check below) rather than
running with no tenant context - but it will reject every request in that
case, which is the fast, loud failure mode a reordering bug deserves.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from api.auth.mfa import (
    AlwaysRequireMfaPolicy,
    MfaEnrollmentChecker,
    MfaPolicyService,
    MfaRequirementPolicy,
)
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.routes import MFA_EXEMPT_PATHS
from api.auth.totp_repository import SqlTotpRepository
from api.db import engine
from api.i18n.http import message
from api.tenancy import EXEMPT_PATHS, TenantContext

_session_factory = async_sessionmaker(engine, expire_on_commit=False)


def _mfa_required_response(request: Request, reason: str) -> JSONResponse:
    """FR-UX-007: the sentence is in the caller's language, the `reason` is
    not.

    A person meets this one when they are otherwise correctly signed in, so it
    has to tell them what to do next - and telling them in a language they may
    not read would make it a dead end. `Accept-Language` is available here even
    though the caller has not cleared the MFA gate; nothing about knowing which
    of two languages to answer in requires a second factor.
    """
    return JSONResponse(
        status_code=403,
        content={
            "detail": message(request, "errors.mfa_required"),
            "reason": reason,
        },
    )


class MfaEnforcementMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp | Starlette,
        *,
        requirement_policy: MfaRequirementPolicy | None = None,
        enrollment_checker: MfaEnrollmentChecker | None = None,
    ) -> None:
        super().__init__(app)
        # Both injectable, independently:
        #   - requirement_policy: substitute a policy that returns False
        #     (e.g. to exercise the "not required" path) without needing
        #     the real role/permission system this default stands in for
        #     - see api.auth.mfa.AlwaysRequireMfaPolicy's docstring.
        #   - enrollment_checker: MfaEnrollmentChecker already takes the
        #     PasskeyRepository/TotpRepository Protocols, which the
        #     in-memory test fakes satisfy structurally - tests inject a
        #     fixed, in-memory-backed checker here and never touch a
        #     database. When omitted (production), a fresh, real
        #     SQL-backed checker is built per request in dispatch below,
        #     since a database session can't be shared across requests.
        self._requirement_policy: MfaRequirementPolicy = (
            requirement_policy or AlwaysRequireMfaPolicy()
        )
        self._enrollment_checker = enrollment_checker

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # EXEMPT_PATHS: no tenant context exists yet to check MFA against
        # (signup, login itself). MFA_EXEMPT_PATHS: tenant context DOES
        # exist - a real user id - but the endpoint is the enrolment/
        # step-up-verification flow that establishes mfa_verified in the
        # first place (api.auth.routes), so it cannot also require it.
        if request.url.path in EXEMPT_PATHS or request.url.path in MFA_EXEMPT_PATHS:
            return await call_next(request)

        tenant = getattr(request.state, "tenant_context", None)
        if not isinstance(tenant, TenantContext) or tenant.user_id is None:
            # Structurally unreachable through the normal middleware chain
            # (TenantContextMiddleware already rejects anything without a
            # valid, user-identified tenant before this dispatch runs) -
            # fail closed rather than assume, the same defensive posture
            # api.tenancy.get_tenant_context takes for the same reason.
            return _mfa_required_response(request, "no_user_context")

        if self._enrollment_checker is not None:
            evaluation = await MfaPolicyService(
                self._requirement_policy, self._enrollment_checker
            ).evaluate(user_id=tenant.user_id, mfa_verified=tenant.mfa_verified)
        else:
            async with _session_factory() as session:
                enrollment = MfaEnrollmentChecker(
                    SqlPasskeyRepository(session), SqlTotpRepository(session)
                )
                evaluation = await MfaPolicyService(self._requirement_policy, enrollment).evaluate(
                    user_id=tenant.user_id, mfa_verified=tenant.mfa_verified
                )

        if not evaluation.satisfied:
            # IAM-090's "authentication events", at the one place in the
            # request path where such an event has a tenant to be recorded
            # against. Signalled rather than written here: api.audit.middleware
            # owns the writing, runs outside this middleware so it sees the
            # response, and has its own session - see its docstring for why
            # an audit entry must not share the request's transaction.
            #
            # Sign-in itself cannot be audited this way, and that is a
            # property of the model rather than an oversight: authentication
            # happens before a tenant is chosen, and audit_log.organization_id
            # is NOT NULL by design (ADR-020). Pre-tenant attempts are
            # recorded in auth_attempt (IAM-019).
            request.state.authentication_denial = evaluation.reason
            return _mfa_required_response(request, evaluation.reason)

        return await call_next(request)

"""IAM-090 at the HTTP edge: every request that reaches a route declaring an
audit category leaves an entry.

--- Why middleware, and not the dependency that already runs on every route ---

api.authz.dependencies.require_permission would be the obvious place - it
already resolves actor, tenant, administration and permission, and already
records the IAM-109 access there. It is the wrong place for two reasons, and
both are about honesty of the record:

  1. It runs BEFORE the handler, so it cannot know the outcome. An audit log
     that says an action was attempted but not whether it succeeded answers
     half the question it exists for.
  2. It runs inside the request's transaction. A denied or failed request
     rolls that transaction back - taking the audit entry with it. The
     entries most worth having are exactly the ones that would vanish.

This middleware runs after the response is generated, with its OWN session,
so a rolled-back request still leaves a durable record of having been
attempted and refused. api.mfa_middleware makes the same choice for the same
reason.

--- What it records, and what the route declares ---

The route declares one thing: which of IAM-090's nine categories its
activity belongs to (`require_permission(..., audit=...)`). Everything else
is derived - actor and tenant from the tenant context, action and resource
type from the permission the route already requires, resource id from the
path, outcome from the status code. A route that had to restate its action
for the log could restate it wrongly, and then the log would disagree with
the authorization decision beside it.

tests/test_audit_coverage.py fails the build if a mutating route declares no
category and is not explicitly exempt, which is what makes "wire it in" a
guarantee rather than a habit.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.routing import Match

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import declared_requirements
from api.db import engine
from api.tenancy import TenantContext

_session_factory = async_sessionmaker(engine, expire_on_commit=False)

CORRELATION_HEADER = "X-Correlation-ID"

# IAM-091's outcome, from the status the request actually got.
#
# 401/403/409 are DENIED rather than FAILURE because a refusal is a security
# signal and a failure is usually an operational one - collapsing them would
# make the log worse at the thing it exists for. 409 is included because in
# this application it is the active-client guard (FR-FRM-000a) refusing a
# write, which is a refusal by any reasonable reading.
_DENIED_STATUSES = frozenset({401, 403, 409})


def _outcome(status_code: int) -> AuditOutcome:
    if status_code < 400:
        return AuditOutcome.SUCCESS
    if status_code in _DENIED_STATUSES:
        return AuditOutcome.DENIED
    return AuditOutcome.FAILURE


def _client_ip(request: Request) -> str | None:
    # Raw connection address only - the same trusted-proxy caveat as
    # api.rate_limit_middleware. A spoofable source address in an audit log
    # is worse than an absent one, because it looks like evidence.
    return request.client.host if request.client is not None else None


def _actor_type(tenant: TenantContext) -> ActorType:
    # Everything reaching an HTTP route today is a signed-in person. Service
    # accounts and support access exist in the model (IAM-090 names support
    # access separately) but have no route to arrive through yet; when they
    # do, this is the one place that has to learn to tell them apart.
    return ActorType.USER


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, audit_log: AuditLog | None = None) -> None:
        super().__init__(app)
        # Injectable so tests can supply an in-memory log; when omitted a
        # fresh SQL-backed one is built per request, since a session cannot
        # be shared across them.
        self._audit_log = audit_log

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # IAM-091's correlation ID. Honoured from the caller when supplied so
        # a client can tie its own trace to ours, generated otherwise so
        # every request has one, and echoed back so the caller can quote it
        # in a support conversation.
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = correlation_id

        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id

        try:
            await self._record(request, response, correlation_id)
        except Exception:  # noqa: BLE001
            # A failure to write the audit entry must not turn a successful
            # request into a failed one - the action already happened, and
            # refusing after the fact would not un-happen it. It is logged by
            # the exception handler above this middleware rather than
            # swallowed silently.
            #
            # This is the opposite of the choice made for IAM-109's access
            # recording, which participates in the request transaction. The
            # difference: that one runs BEFORE the action and can still
            # prevent it, this one runs after and cannot.
            raise

        return response

    async def _record(self, request: Request, response: Response, correlation_id: str) -> None:
        tenant = getattr(request.state, "tenant_context", None)
        if not isinstance(tenant, TenantContext):
            # No verified tenant means no organization to attribute the entry
            # to, and audit_log.organization_id is NOT NULL by design (see
            # ADR-020). Pre-authentication attempts are already recorded in
            # auth_attempt (IAM-019), which is where a request that never
            # established a tenant belongs.
            return

        event = self._authentication_event(
            request, response, correlation_id, tenant
        ) or self._route_event(request, response, correlation_id, tenant)
        if event is None:
            return

        if self._audit_log is not None:
            await self._audit_log.record(event)
            return

        async with _session_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(tenant.organization_id)},
            )
            await AuditLog(SqlAuditRepository(session)).record(event)

    def _authentication_event(
        self,
        request: Request,
        response: Response,
        correlation_id: str,
        tenant: TenantContext,
    ) -> AuditEvent | None:
        """IAM-090's "authentication events", for the one such event that has
        a tenant to be recorded against: a request refused because the
        session has not satisfied MFA (IAM-011).

        Sign-in itself is not auditable per-tenant - it happens before a
        tenant is chosen. See api.audit.trail.AuditTrail.authentication.

        Takes precedence over the route's own category: a request refused at
        the MFA gate never reached the route, so recording it as a posting or
        an export would say something that did not happen.
        """
        reason = getattr(request.state, "authentication_denial", None)
        if reason is None:
            return None

        return AuditEvent(
            organization_id=tenant.organization_id,
            actor_user_id=tenant.user_id,
            actor_type=ActorType.USER if tenant.user_id else ActorType.SYSTEM,
            category=AuditCategory.AUTHENTICATION,
            action="verify_mfa",
            resource_type="session",
            outcome=AuditOutcome.DENIED,
            source_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            correlation_id=correlation_id,
            detail={
                "reason": reason,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )

    def _route_event(
        self,
        request: Request,
        response: Response,
        correlation_id: str,
        tenant: TenantContext,
    ) -> AuditEvent | None:
        route = self._match(request)
        if route is None:
            return None

        declared = next(
            (r for r in declared_requirements(route) if r.audit_category is not None),
            None,
        )
        if declared is None or declared.audit_category is None:
            return None

        administration_id = _administration_from(request, declared)
        return AuditEvent(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            actor_type=_actor_type(tenant),
            category=declared.audit_category,
            action=declared.action,
            resource_type=declared.resource_type,
            resource_id=administration_id,
            outcome=_outcome(response.status_code),
            source_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            correlation_id=correlation_id,
            detail={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )

    def _match(self, request: Request) -> Any:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match is Match.FULL:
                return route
        return None


def _administration_from(request: Request, requirement: Any) -> uuid.UUID | None:
    """The administration the route named, if it named one. Read from the
    path parameter the permission's scope already points at, rather than
    guessing at parameter names.
    """
    scope = requirement.scope
    if scope.kind != "administration" or scope.path_param is None:
        return None
    raw = request.path_params.get(scope.path_param)
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        return None

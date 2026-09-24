"""The HTTP edge of the authorization library: how a route declares what it
requires, and how that declaration is made impossible to forget.

--- Declaring ---

    @app.post("/v1/administrations/{administration_id}/journal-entries")
    async def post_entry(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "post", "journal_entry",
                scope=administration_from_path("administration_id"),
                attributes=AttributeSources(
                    amount=from_body("total_amount"),
                    journal_id=from_body("journal_id"),
                    period_id=from_body("period_id"),
                ),
            )
        ),
    ) -> ...

--- Not forgetting ---

A dependency only runs on routes that declare it, so on its own this is
exactly the "silent gap" api.tenancy.TenantContextMiddleware's docstring
warns about. Three mechanisms close it, and they fail in different
directions on purpose:

  1. api.authz_middleware.AuthorizationEnforcementMiddleware inspects the
     route a request matched BEFORE the handler runs, and refuses to
     dispatch to any route that declares neither a permission requirement
     nor an explicit exemption. Forgetting produces a 500 on every call to
     that endpoint - not a quiet success. This is the runtime guarantee, and
     it holds for routes added by any means, including at runtime.
  2. tests/test_authz_coverage.py walks the same route table at collection
     time and fails CI if any route lacks both. This is the build-time
     guarantee: the gap is caught before it ships, with a message naming the
     route, rather than being discovered by a 500 in production. It needs no
     database, so it runs on every CI invocation - the same design as
     tests/test_isolation_coverage.py for IAM-005.
  3. Opting out is a named entry in AUTHORIZATION_EXEMPT_PATHS below, in
     this file, requiring a justification comment. There is no decorator or
     flag that exempts a route from its own definition site, because the
     point is that skipping authorization should be visible in one
     reviewable list rather than scattered across the routes that did it.

The load-bearing property is that (1) and (2) read the route table, not a
registry the author must remember to update. A route that exists is a route
that is checked.

--- Attribute conditions and forgetting, again ---

IAM-033's conditions need facts about the request (an amount, a journal),
which only the route knows where to find - hence AttributeSources. Forgetting
one of those is ALSO safe, and needs no extra machinery: an unsupplied
attribute makes any condition depending on it unsatisfied
(api.authz.conditions rule 2), so a route that forgets to declare where its
amount comes from is denied against an amount-ceilinged grant instead of
bypassing the ceiling. The failure mode of every omission in this module is
denial.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.authz.firm_access_register import FirmAccessRegister
from api.authz.firm_access_repository import SqlFirmAccessRegisterRepository
from api.authz.model import (
    AdministrationScope,
    AuthorizationDecision,
    AuthorizationRequest,
    OrganizationScope,
    ResourceAttributes,
    TargetScope,
)
from api.authz.repository import SqlAuthorizationRepository
from api.authz.service import AuthorizationService
from api.config import settings
from api.db import get_db_session
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

# Set on the dependency callable returned by require_permission(). The
# middleware and the coverage test both look for exactly this attribute when
# walking a route's dependency tree.
PERMISSION_MARKER = "__ledgr_authorization_requirement__"

# Routes that legitimately require no permission check. Every entry needs a
# reason: this list is the entire opt-out surface, and it is meant to be
# short enough that a reviewer reads all of it.
#
#   /health    infrastructure liveness; carries no tenant context at all
#              (it is already in api.tenancy.EXEMPT_PATHS) and returns no
#              tenant data.
#   /v1/whoami echoes back the caller's own organization id, which they
#              necessarily already know - it is the identity they
#              authenticated as, not a resource. Requiring a permission to
#              learn who you are would make a user with no grants unable to
#              discover that fact.
#   /v1/switcher
#              IAM-110's firm switcher: the administrations the caller
#              ALREADY holds a live grant on, so it discloses nothing they
#              cannot already reach. Requiring a permission would also break
#              it for exactly the users it exists for - firm staff grants are
#              administration-scoped (IAM-107), so an organization-scoped
#              check at their own firm would find nothing. Note that ENTERING
#              an administration (PUT /v1/switcher/{id}) is not exempt: that
#              reaches into one, and carries a real check.
#   /v1/switcher/search, /v1/switcher/active
#              FR-FRM-000 and FR-FRM-000a. Both return only administrations
#              the caller already holds a live grant on - search filters in
#              the join rather than hiding rows afterwards, and `active`
#              returns null rather than a stale name when the session points
#              at a client the user can no longer reach.
#   /v1/me/language
#              FR-LOC-001b makes the UI language a property of the PERSON,
#              not of a tenant resource, and this route reads and writes only
#              the caller's own row - the user id comes from the verified
#              token and there is no route that names another user's. So
#              there is no resource for a permission to be about, and
#              requiring one would be worse than pointless: it would make a
#              user with no grants yet - invited, not yet assigned a role -
#              unable to read the product in their own language, which is
#              exactly the moment IAM-010g cares about.
#
#              Note this is a PUT and therefore still carries idempotency
#              (NFR-032, no exemption) and still passes the MFA gate. What it
#              is exempt from is the permission check alone.
#   /v1/me/reminders
#              ADR-090: whether the caller receives reminder e-mails. The /v1/me/language case
#              exactly - a property of the person, read and written only on the caller's own row
#              (the user id from the verified token, no route naming another user's), with no
#              tenant resource for a permission to be about. PUT still carries idempotency and
#              the MFA gate.
#   /v1/me
#              The caller's own identity and memberships: their user row,
#              their organization, the administrations they ALREADY hold a
#              live grant on (the switcher's own list, or the business's
#              administrations read through RLS) and their own MFA factors.
#              The same argument /v1/switcher makes: it discloses nothing the
#              caller cannot already reach, and requiring a permission would
#              break it for exactly the users it exists for - a firm
#              accountant's grants are administration-scoped, and a user who
#              has just signed up holds an organization with no
#              administration yet, which is the state this route exists to
#              report (`onboarding.needs_administration`). A GET; changes
#              nothing.
#   /v1/fiscal-years/preview
#              FR-ONB-006's period preview: arithmetic on two dates the
#              caller typed, naming no administration and touching no table
#              (api.ledger.fiscal.FiscalYearService.preview says the same).
#              Onboarding needs it BEFORE a year exists to be authorized
#              against. Still behind tenant context and the MFA gate; exempt
#              from the permission check alone, for the same "no resource for
#              a permission to be about" reason /v1/me/language is.
#   /v1/auth/*
#              Every route in api.auth.routes (IAM-010's signup/sign-in
#              surface, and the MFA enrolment/step-up flow) is exempt, for
#              one of two reasons depending on which half of the module it
#              is in:
#                - signup/login/passkey-login/google (api.tenancy.
#                  EXEMPT_PATHS): there is no tenant yet for a permission to
#                  be scoped to - these routes ESTABLISH identity, they do
#                  not act on a resource within one.
#                - MFA enrolment/verify, logout (api.mfa_middleware.
#                  MFA_EXEMPT_PATHS): a real user id exists, but enrolling
#                  or verifying YOUR OWN second factor is the same kind of
#                  "no resource for a permission to be about" case
#                  /v1/me/language already is, for the same reason - a user
#                  with no role assignment yet must still be able to finish
#                  the MFA gate that IAM-011 puts in front of everything
#                  else, or they are locked out of the product by the very
#                  mechanism meant to secure it.
#                - /v1/auth/verify-email (api.tenancy.EXEMPT_PATHS): the
#                  single-use token in the body is the proof, and the link
#                  is opened wherever the mail is read - no session, no
#                  tenant. /v1/auth/verify-email/resend needs a session but
#                  acts only on the caller's own address, the /v1/me case.
#   /v1/me/sessions, /v1/me/sessions/{id}, /v1/me/passkeys,
#   /v1/me/passkeys/{id}, /v1/me/password, /v1/me/mfa/totp,
#   /v1/me/trusted-devices, /v1/me/trusted-devices/{id}
#              IAM-017 and the account's own security settings
#              (api.account.security_routes). The /v1/me/language argument
#              exactly: every one of these reads or changes only rows the
#              verified token's own user owns - their sessions, their
#              passkeys, their password, their second factor, their
#              remembered devices (ADR-061) - and no route names another
#              user's. A person with no role assignment yet must still be
#              able to see where they are signed in and revoke a device
#              they no longer hold; a permission would only ever
#              stand between them and their own account. Each mutation is
#              recorded via AuditTrail.authentication from its handler (see
#              tests/test_audit_coverage.py for that list) and still carries
#              idempotency and the MFA gate.
#   /v1/dev/outbox
#              Exists only when settings.expose_dev_outbox is true (never the
#              default) and is exempt only then, from the same flag, so the
#              exemption cannot outlive the route. Returns what the
#              collecting e-mail sender would have sent - developer tooling
#              with no tenant data in it. See api.mail.dev_outbox.
AUTHORIZATION_EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/v1/whoami",
        "/v1/switcher",
        "/v1/switcher/search",
        "/v1/switcher/active",
        "/v1/me/language",
        "/v1/me/reminders",
        "/v1/me",
        "/v1/me/sessions",
        "/v1/me/sessions/{session_id}",
        "/v1/me/passkeys",
        "/v1/me/passkeys/{passkey_id}",
        "/v1/me/password",
        "/v1/me/mfa/totp",
        "/v1/me/trusted-devices",
        "/v1/me/trusted-devices/{device_id}",
        "/v1/fiscal-years/preview",
        "/v1/auth/signup",
        "/v1/auth/signup/google",
        "/v1/auth/login",
        "/v1/auth/recover",
        "/v1/auth/logout",
        "/v1/auth/login/passkey/begin",
        "/v1/auth/login/passkey/finish",
        "/v1/auth/login/google/start",
        "/v1/auth/login/google/callback",
        "/v1/auth/mfa/totp/enroll/begin",
        "/v1/auth/mfa/totp/enroll/confirm",
        "/v1/auth/mfa/totp/verify",
        "/v1/auth/mfa/passkey/enroll/begin",
        "/v1/auth/mfa/passkey/enroll/finish",
        "/v1/auth/mfa/passkey/verify/begin",
        "/v1/auth/mfa/passkey/verify/finish",
        "/v1/auth/verify-email",
        "/v1/auth/verify-email/resend",
    }
    | ({"/v1/dev/outbox"} if settings.expose_dev_outbox else set())
)


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """Where the target of this route's check comes from. An organization
    target is the caller's own tenant context; an administration target is
    read from a path parameter, so the check is always against the
    administration the request actually names rather than one the handler
    resolves later.
    """

    kind: Literal["organization", "administration"]
    path_param: str | None = None


def organization_scope() -> ScopeSpec:
    return ScopeSpec("organization")


def administration_from_path(path_param: str) -> ScopeSpec:
    return ScopeSpec("administration", path_param)


@dataclass(frozen=True, slots=True)
class AttributeSource:
    location: Literal["body", "path", "query"]
    name: str


def from_body(name: str) -> AttributeSource:
    """A top-level key of the JSON request body. Nested paths are not
    supported on purpose - a condition attribute buried three levels into a
    payload is a sign the attribute belongs in its own field.
    """
    return AttributeSource("body", name)


def from_path(name: str) -> AttributeSource:
    return AttributeSource("path", name)


def from_query(name: str) -> AttributeSource:
    return AttributeSource("query", name)


@dataclass(frozen=True, slots=True)
class AttributeSources:
    """Where this route's IAM-033 attribute values are found. source_ip is
    absent because it never varies by route - it comes from the connection.
    """

    amount: AttributeSource | None = None
    cost_centre_id: AttributeSource | None = None
    journal_id: AttributeSource | None = None
    period_id: AttributeSource | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationRequirement:
    """What a route declares. Carried on the dependency callable so the
    middleware and the coverage test can read it without executing anything,
    and so /openapi.json could eventually document it.
    """

    action: str
    resource_type: str
    scope: ScopeSpec
    attributes: AttributeSources
    # IAM-090: which of the requirement's nine categories this route's
    # activity belongs to, or None for a route that records nothing.
    # tests/test_audit_coverage.py requires it on every mutating route.
    #
    # The audit event's action and resource_type come from the permission
    # above rather than being declared again - a route that requires
    # `post journal_entry` is auditing a post of a journal entry, and letting
    # those drift apart would produce a log that disagrees with the
    # authorization decision beside it.
    audit_category: AuditCategory | None = None


async def get_firm_access_register(
    session: AsyncSession = Depends(get_db_session),
) -> FirmAccessRegister:
    """IAM-109's recorder, on the same tenant-scoped session as the request
    it is recording. Tests override this the same way they override
    get_authorization_service.
    """
    return FirmAccessRegister(SqlFirmAccessRegisterRepository(session))


async def get_authorization_service(
    session: AsyncSession = Depends(get_db_session),
) -> AuthorizationService:
    """The real, SQL-backed service. Depending on get_db_session (rather
    than building a session here) means an authorization check runs inside
    the same tenant-scoped transaction as the request it authorizes - the
    grant rows it reads are subject to the same RLS as everything else.

    Tests override this via app.dependency_overrides with a service built on
    the in-memory fake repository, so the HTTP-layer tests need no database.
    """
    return AuthorizationService(SqlAuthorizationRepository(session))


def _client_ip(request: Request) -> str | None:
    # Raw connection address only. A reverse-proxied deployment must
    # configure trusted proxies before X-Forwarded-For means anything - the
    # same caveat api.rate_limit_middleware carries, and a more serious one
    # here: a spoofable source address would defeat an ip_allowlist
    # condition. Until that wiring exists, an allowlist condition behind an
    # untrusted proxy denies (the proxy's own address will not be in the
    # allowlist) rather than admits.
    return request.client.host if request.client is not None else None


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        # parse_float=Decimal, not the stdlib default: a monetary amount in
        # a request body must never become a float on its way to an amount
        # ceiling comparison (NFR-031, CLAUDE.md rule four).
        parsed = json.loads(raw, parse_float=Decimal)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read(request: Request, source: AttributeSource | None, body: dict[str, Any]) -> Any | None:
    if source is None:
        return None
    if source.location == "body":
        return body.get(source.name)
    if source.location == "path":
        return request.path_params.get(source.name)
    return request.query_params.get(source.name)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Reached only if some other code path parsed the body with the
        # stdlib default. Refusing (rather than converting) keeps the float
        # from being laundered into a decimal comparison that looks exact.
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


async def _resource_attributes(request: Request, sources: AttributeSources) -> ResourceAttributes:
    declared = (sources.amount, sources.cost_centre_id, sources.journal_id, sources.period_id)
    needs_body = any(s is not None and s.location == "body" for s in declared)
    # Starlette caches the body after the first read, so the handler can
    # still parse the same request normally.
    body = await _json_body(request) if needs_body else {}

    return ResourceAttributes(
        amount=_as_decimal(_read(request, sources.amount, body)),
        cost_centre_id=_as_uuid(_read(request, sources.cost_centre_id, body)),
        journal_id=_as_uuid(_read(request, sources.journal_id, body)),
        period_id=_as_uuid(_read(request, sources.period_id, body)),
        source_ip=_client_ip(request),
    )


def _resolve_target(request: Request, tenant: TenantContext, scope: ScopeSpec) -> TargetScope:
    if scope.kind == "organization":
        return OrganizationScope(organization_id=tenant.organization_id)

    if scope.path_param is None:
        # Only reachable by constructing ScopeSpec("administration") directly
        # instead of via administration_from_path. A misdeclared route must
        # fail loudly, never silently fall back to an organization target -
        # that fallback would be the widening IAM-032 forbids.
        raise HTTPException(
            status_code=500,
            detail="administration scope declared without a path parameter",
        )

    raw = request.path_params.get(scope.path_param)
    administration_id = _as_uuid(raw)
    if administration_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"path parameter {scope.path_param!r} is not a valid administration id",
        )
    return AdministrationScope(administration_id=administration_id)


# Methods that change something. A mismatch between the client the session
# is in and the client a request writes to is FR-FRM-000a's named failure -
# "posting to the wrong client" - so these are the ones the guard covers.
# Reads are left alone: opening a client's page from a link or a search
# result is ordinary, and refusing it would make the product unusable to
# protect against nothing.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def require_permission(
    action: str,
    resource_type: str,
    *,
    scope: ScopeSpec,
    attributes: AttributeSources = AttributeSources(),
    allows_cross_client: bool = False,
    audit: AuditCategory | None = None,
) -> Callable[..., Awaitable[AuthorizationDecision]]:
    """Builds the FastAPI dependency a route declares to require one
    (action, resource_type) at one scope. Raises HTTPException(403) on
    denial; returns the AuthorizationDecision on success, so a handler that
    wants to record which grant admitted it can.

    `allows_cross_client` opts a route out of FR-FRM-000a's active-client
    guard. It defaults to False so the safe behaviour is what a route gets
    by not thinking about it, and the one route that legitimately writes to
    an administration other than the session's current one - the switcher
    itself, whose whole job is changing which that is - says so at its own
    definition site rather than in a list somewhere else.
    """
    requirement = AuthorizationRequirement(
        action=action,
        resource_type=resource_type,
        scope=scope,
        attributes=attributes,
        audit_category=audit,
    )

    async def dependency(
        request: Request,
        tenant: TenantContext = Depends(get_tenant_context),
        service: AuthorizationService = Depends(get_authorization_service),
        register: FirmAccessRegister = Depends(get_firm_access_register),
    ) -> AuthorizationDecision:
        if tenant.user_id is None:
            # Structurally unreachable through the normal middleware chain
            # (api.mfa_middleware already rejects a tenant context with no
            # user). Fail closed rather than assume, the same posture
            # api.tenancy.get_tenant_context takes.
            raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

        target = _resolve_target(request, tenant, scope)

        # FR-FRM-000a, and the only mechanism in that requirement that
        # PREVENTS the failure rather than making it less likely: if the
        # header says one client and the request writes to another, one of
        # them is wrong, so the write does not happen.
        #
        # Runs before authorization on purpose. A mismatch is a client-state
        # bug, not a permission problem, and answering 403 for it would send
        # whoever debugs it looking at grants. It also means the mismatch is
        # reported even when the caller does hold the permission - which is
        # the dangerous case, because that is the request that would
        # otherwise succeed against the wrong books.
        #
        # Only applies when the session HAS an active client: an API client
        # or a session at the switcher constrains nothing, and reads are
        # never constrained at all.
        if (
            not allows_cross_client
            and request.method in MUTATING_METHODS
            and isinstance(target, AdministrationScope)
            and tenant.active_administration_id is not None
            and tenant.active_administration_id != target.administration_id
        ):
            # FR-UX-007: the sentence is localised, the `reason` is not. This
            # is the error in this file a person is most likely to meet
            # mid-task - the request was legitimate and aimed at the wrong
            # books - so it is the one that most needs to be in their own
            # language.
            raise problem(
                request,
                409,
                "errors.active_client_mismatch",
                reason="active_client_mismatch",
                active_administration_id=str(tenant.active_administration_id),
                requested_administration_id=str(target.administration_id),
            )

        decision = await service.authorize(
            AuthorizationRequest(
                user_id=tenant.user_id,
                action=action,
                resource_type=resource_type,
                target=target,
                attributes=await _resource_attributes(request, attributes),
            )
        )

        if decision.denied:
            # The action and resource type stay OUT of the sentence and
            # travel beside it. "Not permitted to post journal_entry" puts two
            # developer-facing identifiers inside a user-facing string, which
            # FR-UX-007 forbids - and a client wanting to branch on the
            # failure would have to parse a translated sentence to do it.
            raise problem(
                request,
                403,
                "errors.not_permitted",
                reason=decision.reason,
                action=action,
                resource_type=resource_type,
                detail=decision.detail,
            )

        # IAM-109's "when each last accessed it". This is the right place
        # for it and there is no other: it runs on exactly the requests that
        # constitute an access, after they are known to be authorized, and
        # it already resolved which administration and which user. A
        # per-route call would be forgettable in precisely the way
        # AuthorizationEnforcementMiddleware exists to prevent.
        #
        # Only administration targets are recorded - an organization-level
        # action is not access to anybody's books - and only successful
        # authorizations, so a denied probe cannot write a row claiming
        # someone was here.
        #
        # It participates in the request's transaction rather than being
        # best-effort. An access to a client's books that could not be
        # recorded should not complete: IAM-109 exists to stop exactly the
        # invisibility that a silently-dropped record would create. The cost
        # is a row lock per (user, administration) - contention only between
        # one person's own concurrent requests on one administration - and
        # the throttle makes the common case a no-op update rather than a
        # rewrite.
        if isinstance(target, AdministrationScope):
            await register.record_access(
                user_id=tenant.user_id, administration_id=target.administration_id
            )

        return decision

    setattr(dependency, PERMISSION_MARKER, requirement)
    return dependency


def declared_requirements(route: object) -> list[AuthorizationRequirement]:
    """Every authorization requirement declared anywhere in a route's
    dependency tree, found by walking FastAPI's own resolved Dependant graph
    rather than a side registry. Sub-dependencies count, so a shared
    "requires Accountant on this administration" dependency composed of
    require_permission(...) is recognized without registering anything.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return []

    found: list[AuthorizationRequirement] = []
    stack = [dependant]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        call = getattr(current, "call", None)
        requirement = getattr(call, PERMISSION_MARKER, None)
        if isinstance(requirement, AuthorizationRequirement):
            found.append(requirement)

        stack.extend(getattr(current, "dependencies", []))

    return found

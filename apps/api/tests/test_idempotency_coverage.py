"""NFR-032: every mutating endpoint is covered, and the opt-out list is guarded.

The fourth coverage check in this suite, alongside tests/test_isolation_coverage.py
(IAM-005), tests/test_authz_coverage.py (CLAUDE.md rule three) and
tests/test_audit_coverage.py (IAM-090). All four walk the FastAPI route table
at collection time; none needs a database, so all four run on every CI
invocation.

**This one is shaped differently from the other three, and the difference is
the point.** Those check that each route DECLARES something, because only the
route knows which permission or audit category applies. Idempotency has
nothing route-specific to declare - every mutating endpoint wants identical
behaviour - so it is applied by middleware to every POST/PUT/PATCH/DELETE.

That inverts what can go wrong. A new endpoint cannot be forgotten; it is
covered the moment it is registered. What CAN go wrong is:

  * the middleware being removed from the app, silently uncovering everything
  * a path being added to the exempt list without a good reason
  * an exemption outliving the route it was written for

so those are what this file checks.
"""

from __future__ import annotations

from api.idempotency import MUTATING_METHODS as DOMAIN_MUTATING_METHODS
from api.idempotency_middleware import (
    FRAMEWORK_PATHS,
    IDEMPOTENCY_EXEMPT_PATHS,
    IdempotencyMiddleware,
)
from api.main import app

# Mirrors api.idempotency.MUTATING_METHODS. Restated rather than imported for
# the reason tests/test_audit_coverage.py restates its own copy: the two
# constants exist for different reasons, and a future change to one should not
# silently move the other. test_the_two_definitions_agree below is what makes
# the duplication safe rather than a latent bug.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _mutating_routes() -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None or path in FRAMEWORK_PATHS:
            continue
        for method in sorted(set(methods) & MUTATING_METHODS):
            routes.append((method, path))
    return routes


def test_the_check_actually_has_routes_to_check() -> None:
    """Without this, every assertion below passes vacuously if
    `_mutating_routes` ever stops finding anything. A coverage check that
    silently checks nothing is worse than no check, because it reads as a
    guarantee.
    """
    found = set(_mutating_routes())

    assert found, "no mutating routes found - the coverage check is inert"
    assert ("PUT", "/v1/switcher/{administration_id}") in found


def test_the_idempotency_middleware_is_installed() -> None:
    """The single point of failure for NFR-032.

    Every mutating endpoint is protected because this middleware is in the
    stack. Remove it and nothing else in the suite notices: the endpoints
    still work, the tests still pass, and every retry double-posts.
    """
    installed = [middleware.cls for middleware in app.user_middleware]

    assert IdempotencyMiddleware in installed, (
        "IdempotencyMiddleware is not installed. Every mutating endpoint is "
        "unprotected against retries (NFR-032)."
    )


def test_idempotency_runs_inside_mfa_and_outside_authorization() -> None:
    """Order is load-bearing, and it is not obvious from reading main.py -
    Starlette runs the LAST-added middleware first.

      * inside MFA, so an unauthenticated request never claims a key. A key
        burned by a request refused at the door would make the caller's
        legitimate retry look like a duplicate.
      * outside authorization, so a 403 is stored and replayed - a denial is a
        deterministic answer to this exact request.
    """
    from api.authz_middleware import AuthorizationEnforcementMiddleware
    from api.mfa_middleware import MfaEnforcementMiddleware

    order = [middleware.cls for middleware in app.user_middleware]
    # user_middleware is outermost-first.
    assert order.index(MfaEnforcementMiddleware) < order.index(IdempotencyMiddleware), (
        "idempotency must run inside MFA enforcement"
    )
    assert order.index(IdempotencyMiddleware) < order.index(AuthorizationEnforcementMiddleware), (
        "idempotency must run outside authorization enforcement"
    )


def test_no_mutating_route_is_exempt_without_a_reason() -> None:
    """NFR-032 says "all mutating API endpoints", so an entry in
    IDEMPOTENCY_EXEMPT_PATHS is a deviation from the requirement.

    The list is empty today. This test exists so that adding to it is a
    visible, deliberate act rather than a quiet one - the assertion names
    every exempt path in its failure message, so a reviewer reads them.
    """
    exempt = sorted(IDEMPOTENCY_EXEMPT_PATHS)

    assert exempt == [], (
        "These mutating routes are exempt from NFR-032 and each needs a "
        f"documented reason in api.idempotency_middleware: {exempt}. The bar is "
        "not 'this endpoint is naturally idempotent' - that is a property of "
        "today's handler, not of the endpoint, and the exemption would outlive "
        "the reasoning."
    )


def test_every_exempt_path_actually_exists() -> None:
    """An exemption for a route that has since been renamed or removed is dead
    weight that quietly widens the opt-out list.
    """
    registered = {getattr(route, "path", None) for route in app.routes}
    stale = sorted(path for path in IDEMPOTENCY_EXEMPT_PATHS if path not in registered)

    assert not stale, f"IDEMPOTENCY_EXEMPT_PATHS names routes that no longer exist: {stale}"


def test_the_two_definitions_of_mutating_agree() -> None:
    """This file and api.idempotency each define the set. They are allowed to
    diverge deliberately; they are not allowed to diverge by accident.
    """
    assert MUTATING_METHODS == DOMAIN_MUTATING_METHODS


def test_the_middleware_would_actually_cover_a_new_route() -> None:
    """The check is only worth having if the property it asserts is real.

    Registers a brand-new mutating route on a probe app carrying the
    middleware, sends no key, and asserts it is refused - proving coverage is
    a consequence of registration rather than of anything the route declares.
    """
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.idempotency import HEADER, IdempotencyStore
    from api.tenancy import TenantContext
    from tests.support.fake_idempotency_repository import (
        InMemoryIdempotencyRepository,
    )

    organization_id, user_id = uuid.uuid4(), uuid.uuid4()
    probe = FastAPI()

    @probe.post("/v1/brand-new-endpoint")
    def brand_new() -> dict[str, str]:
        return {"status": "executed"}

    class _Tenant:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self.inner = inner

        async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
            if scope["type"] == "http":
                scope.setdefault("state", {})
                scope["state"]["tenant_context"] = TenantContext(
                    organization_id=organization_id, user_id=user_id
                )
            await self.inner(scope, receive, send)

    probe.add_middleware(
        IdempotencyMiddleware,
        store=IdempotencyStore(InMemoryIdempotencyRepository()),
    )
    probe.add_middleware(_Tenant)

    client = TestClient(probe)
    without_key = client.post("/v1/brand-new-endpoint", json={})
    with_key = client.post("/v1/brand-new-endpoint", json={}, headers={HEADER: "req-1"})

    assert without_key.status_code == 400, (
        "a newly registered mutating route was not covered by the middleware"
    )
    assert with_key.status_code == 200

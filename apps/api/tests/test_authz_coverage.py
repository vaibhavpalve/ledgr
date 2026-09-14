"""CLAUDE.md rule three: "One authorization library, used everywhere."

This is the build-time half of making that unforgettable. It walks the same
FastAPI route table the application serves and fails if any route declares
neither an authorization requirement nor an explicit exemption - so the gap
is caught in CI, naming the route, before it can ever be reached by a
request.

The runtime half is api.authz_middleware.AuthorizationEnforcementMiddleware,
tested in test_authz_middleware.py. Both exist because they fail at
different moments: this test catches a route CI never exercises, and the
middleware catches a route registered after startup, which this test cannot
see. Neither needs a database, so both run on every CI invocation - the same
design as test_isolation_coverage.py for IAM-005.
"""

from __future__ import annotations

from api.authz.dependencies import AUTHORIZATION_EXEMPT_PATHS, declared_requirements
from api.authz_middleware import FRAMEWORK_PATHS
from api.main import app

IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def test_every_route_declares_an_authorization_requirement() -> None:
    missing: list[str] = []

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        if path in AUTHORIZATION_EXEMPT_PATHS or path in FRAMEWORK_PATHS:
            continue
        if declared_requirements(route):
            continue
        for method in sorted(set(methods) - IGNORED_METHODS):
            missing.append(f"  {method} {path}")

    assert not missing, (
        "These routes declare no authorization requirement (CLAUDE.md rule three: every "
        "authorization decision goes through the single library). Add "
        "Depends(require_permission(...)) to each, or add its path to "
        "api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS with a reason:\n" + "\n".join(missing)
    )


def test_every_exempt_path_actually_exists() -> None:
    """An exemption for a route that has since been renamed or removed is
    dead weight that quietly widens the opt-out list. Keeping this list
    honest is what makes it reviewable.
    """
    registered = {getattr(route, "path", None) for route in app.routes}
    stale = sorted(path for path in AUTHORIZATION_EXEMPT_PATHS if path not in registered)

    assert not stale, (
        f"AUTHORIZATION_EXEMPT_PATHS names routes that no longer exist; remove them: {stale}"
    )


def test_a_declared_requirement_is_readable_without_running_the_route() -> None:
    """The mechanism both this test and the middleware depend on: a route's
    requirement is discoverable by introspecting FastAPI's own resolved
    dependency graph, with nothing executed and no side registry to keep in
    step.
    """
    # By method as well as path: PATCH /v1/administrations/{administration_id}
    # (api.onboarding.routes) shares the path and declares a different
    # permission, and the route table's order is not this test's business.
    route = next(
        r
        for r in app.routes
        if getattr(r, "path", None) == "/v1/administrations/{administration_id}"
        and "GET" in (getattr(r, "methods", None) or set())
    )

    requirements = declared_requirements(route)

    assert [(r.action, r.resource_type) for r in requirements] == [("view", "administration")]
    assert requirements[0].scope.kind == "administration"
    assert requirements[0].scope.path_param == "administration_id"

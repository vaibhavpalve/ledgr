"""The reason a developer writing a new endpoint cannot forget to call the
authorization library.

api.authz.dependencies.require_permission is how a route DECLARES what it
needs. This middleware is what makes the declaration mandatory: before
dispatching, it resolves which route the request matched and checks that the
route's dependency tree contains at least one authorization requirement. A
route that declares none, and is not in the explicit exemption list, is
refused - the handler never runs.

--- Why the check is possible at all ---

FastAPI resolves each route's dependency graph at registration time into a
Dependant tree hanging off APIRoute.dependant. That tree is readable without
executing anything, so "does this route check authorization?" is a question
answerable by introspection rather than by trusting a registry the author
must remember to update. Route matching uses Starlette's own
route.matches(scope), the same call the router makes a moment later, so this
middleware and the router always agree on which route a request belongs to.

--- Why 500 and not 403 ---

A missing declaration is a bug in the application, not a denied request. A
403 would be indistinguishable from ordinary "you lack this permission" and
could sit unnoticed in a dashboard for weeks while an endpoint quietly
refused everyone. A 500 is unmissable in exactly the way a wiring mistake
should be, and the response body names the route and what to add.

--- Why this is not the only mechanism ---

This is the runtime backstop; it catches the mistake on the first call. It
is deliberately paired with tests/test_authz_coverage.py, which walks the
same route table at collection time and fails CI before the code ever runs.
Two mechanisms because they fail at different moments: a route that is never
exercised in CI is still caught by the coverage test, and a route registered
dynamically after startup (which the coverage test cannot see) is still
caught here.

--- Ordering ---

Added FIRST in api.main, which makes it the INNERMOST layer - it runs last,
immediately before the router. That is deliberate: the outer layers
establish who the caller is (TenantContextMiddleware) and that they have
satisfied MFA (MfaEnforcementMiddleware), and this one asks whether the
endpoint they are about to reach has an opinion about what they may do. See
api.mfa_middleware's docstring for the add_middleware ordering rule this
relies on (last added = outermost = runs first), verified empirically there.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

from api.authz.dependencies import AUTHORIZATION_EXEMPT_PATHS, declared_requirements

# Framework-served paths, which have no dependency tree of ours to inspect.
# Kept in step with tests.support.isolation.FRAMEWORK_PATHS, which excludes
# the same set from the IAM-005 coverage check for the same reason.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def _misconfigured(method: str, path: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"{method} {path} declares no authorization requirement. Add "
                "Depends(require_permission(...)) to the route, or add its path to "
                "api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS with a reason "
                "(CLAUDE.md rule three: one authorization library, used everywhere)."
            ),
            "reason": "route_declares_no_authorization",
        },
    )


class AuthorizationEnforcementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in AUTHORIZATION_EXEMPT_PATHS or path in FRAMEWORK_PATHS:
            return await call_next(request)

        matched = None
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match is Match.FULL:
                matched = route
                break

        if matched is None:
            # No route at all (404), or a path that matched with the wrong
            # method (405, which Starlette reports as Match.PARTIAL). Both
            # are the router's answer to give, and neither reaches a
            # handler, so there is nothing here to authorize.
            return await call_next(request)

        # The exemption list names routes by their TEMPLATE
        # (`/v1/me/sessions/{session_id}`), which is what
        # tests/test_authz_coverage.py checks against - so a parameterised
        # exemption is honoured here by the same name, not only when the
        # concrete path happens to equal it. Before this, a templated entry
        # passed the coverage test and 500'd every real request.
        template = getattr(matched, "path", path)
        if template in AUTHORIZATION_EXEMPT_PATHS:
            return await call_next(request)

        if not declared_requirements(matched):
            return _misconfigured(request.method, template)

        return await call_next(request)

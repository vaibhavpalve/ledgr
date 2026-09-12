"""SEC-004: anti-CSRF on state-changing requests.

CSRF is a same-origin-policy gap specific to credentials the BROWSER attaches
automatically — cookies. This codebase's only live authentication mechanism
today is a bearer token an explicit client reads and sets in an
`Authorization` header (api.tenancy) — nothing forges that from a cross-site
page, because nothing forces a victim's browser to attach it. Session
cookies are the mechanism this control exists for.

So this middleware's rule is: an unsafe-method request that carries the
session cookie (api.security.cookies.SESSION_COOKIE_NAME) is the only kind
this check ever touches — that is the only kind CSRF can reach. A
bearer-only request (no session cookie present) is not subject to this check
at all, and that is not a gap: a forged cross-site request cannot produce a
Bearer header a browser did not decide to send on its own.

Double-submit cookie pattern: a second, non-HttpOnly cookie
(api.security.cookies.CSRF_COOKIE_NAME) carries a token only same-site
JavaScript can read. The client echoes it back as the X-CSRF-Token header on
every unsafe-method request; a cross-site attacker can trigger the request
but cannot read the cookie to put its value in the header, because reading a
cookie back (as opposed to having the browser send it) requires same-origin
script access. Compared with `hmac.compare_digest` so the check itself is
not a timing oracle.

No route sets the session cookie yet (see api.security.cookies' module
docstring), so this middleware is currently a no-op on every real request in
this codebase — exactly like api.rate_limit_middleware.RateLimitingMiddleware
before it. It is wired into api.main now so a future cookie-based login
cannot ship without already passing through it.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from api.i18n.http import message
from api.security.cookies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSRF_HEADER_NAME = "x-csrf-token"


class CsrfProtectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)

        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if session_cookie is None:
            # Bearer-token request: no ambient credential for a cross-site
            # request to ride on. See the module docstring.
            return await call_next(request)

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)

        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": message(request, "errors.csrf_token_invalid"),
                    "reason": "csrf_token_missing_or_mismatched",
                },
            )

        return await call_next(request)

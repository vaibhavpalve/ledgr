"""SEC-003 / SEC-004 at the HTTP edge: a Content-Security-Policy with a
per-request nonce and no `unsafe-inline`/`unsafe-eval`, plus
Strict-Transport-Security with preload — on every response, including error
responses other middleware and exception handlers produce.

Added as the OUTERMOST middleware (see api.main's ordering comment) so it
wraps every other layer: a 401 from TenantContextMiddleware, a 403 from
AuthorizationEnforcementMiddleware or CsrfProtectionMiddleware, and an
ordinary 200 all pass back through this middleware's `dispatch` on their way
out, and all get the same headers. A middleware added closer to the handler
would miss responses short-circuited by something outside it.

The nonce is generated once per request and exposed as
`request.state.csp_nonce` for any future HTML response (a server-rendered
error page, a docs page) that needs to reference it in a <script> tag's
`nonce` attribute — nothing in this API renders HTML today, but the policy
is written for when it might, rather than only for the JSON responses that
exist now.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

#: 2 years, in seconds — the minimum https://hstspreload.org/#requirements
#: asks for (max-age >= 31536000 seconds / 1 year; twice that leaves no
#: rounding-down risk), together with the includeSubDomains and preload
#: directives the preload list also requires.
_HSTS_MAX_AGE_SECONDS = 63072000

#: 128 bits — enough that a nonce cannot be guessed or reused across requests.
_NONCE_BYTES = 16


def _content_security_policy(nonce: str) -> str:
    """SEC-003: no `unsafe-inline`, no `unsafe-eval`, anywhere in the policy.

    `default-src 'none'` is the closed default every other directive narrows
    from, rather than a broad default individual directives try to
    restrict. `'strict-dynamic'` alongside the nonce lets a nonce'd script
    load further scripts it inserts itself — the standard nonce-based CSP
    pattern — without ever needing a host allowlist a compromised CDN could
    abuse.
    """
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src 'nonce-{nonce}' 'strict-dynamic'",
            "style-src 'self'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "font-src 'self'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        )
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        response.headers["Content-Security-Policy"] = _content_security_policy(nonce)
        response.headers["Strict-Transport-Security"] = (
            f"max-age={_HSTS_MAX_AGE_SECONDS}; includeSubDomains; preload"
        )
        return response

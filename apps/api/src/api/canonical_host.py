"""Sends the secondary domains (boeklite.com, www.*) to the one canonical origin.

ADR-063 fixes one origin: session cookies are host-only (SEC-004) and a
passkey is bound to one relying-party id, so a visitor on boeklite.com could
never sign in to an app whose cookies and passkeys belong to boeklite.nl.
Serving the app on both hosts would look like it worked and fail at login;
redirecting is the only arrangement that does not.

Only hosts named in `settings.redirect_hosts` are redirected, so the Railway
service URL and the health check's own host keep answering. The target is
`settings.app_base_url`, the same value e-mail links already use, so there is
no second place the canonical origin is written down. `/health` is never
redirected: a platform probe must see 200 from wherever it connects.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from api.config import settings


def _redirect_hosts() -> frozenset[str]:
    return frozenset(h.strip().lower() for h in settings.redirect_hosts.split(",") if h.strip())


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        hosts = _redirect_hosts()
        if hosts and request.url.path != "/health":
            host = (request.headers.get("host") or "").split(":")[0].lower()
            if host in hosts:
                target = settings.app_base_url.rstrip("/") + request.url.path
                if request.url.query:
                    target += "?" + request.url.query
                # 308 keeps the method, so a stray POST is not silently turned
                # into a GET against the canonical host.
                return RedirectResponse(target, status_code=308)
        return await call_next(request)

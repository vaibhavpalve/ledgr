"""IAM-019 at the HTTP edge: rate limiting and progressive lockout on
authentication endpoints. Wraps api.auth.rate_limiting.AuthRateLimiter as
ASGI middleware so the check-before/record-after cycle happens uniformly,
without every future auth route re-implementing it.

Configured with an explicit INCLUDE map of (method, path) -> endpoint
config - the opposite shape from api.tenancy.EXEMPT_PATHS/
api.mfa_middleware's blanket coverage. Rate limiting only makes sense for
a deliberately chosen set of authentication actions (login, password
recovery, TOTP verification), never the whole application by default;
listing them explicitly means a new protected route is a conscious
addition to this map, not something that silently starts or stops being
covered.

Not wired into api.main: there are no real authentication HTTP endpoints
in this codebase yet (login, password recovery, etc. are all still
service-layer only - api.auth.service, api.auth.account_recovery - per
every prior auth ADR's stated boundary). This middleware is ready to
protect them, configured with their (method, path), the moment they exist.

Account-key extraction reads the request's JSON body (Starlette caches it
after the first read, so a downstream handler can still parse the same
body normally). Client IP is read from the ASGI connection's own address
(request.client.host) only - trusting a proxy-supplied header like
X-Forwarded-For requires knowing which proxies are trustworthy, a
deployment-specific decision not made here; a reverse-proxied deployment
needs that wiring separately before this middleware's source_ip is
meaningful for credential-stuffing detection.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from api.auth.rate_limiting import AttemptOutcome, AuthRateLimiter
from api.i18n.http import message


@dataclass(frozen=True, slots=True)
class ProtectedEndpoint:
    endpoint: str
    account_key_field: str = "email"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


async def _extract_account_key(request: Request, field: str) -> str | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get(field)
    if not value:
        return None
    return str(value).strip().lower()


class RateLimitingMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        limiter: AuthRateLimiter,
        protected_paths: dict[tuple[str, str], ProtectedEndpoint],
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = limiter
        self._protected_paths = protected_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        config = self._protected_paths.get((request.method, request.url.path))
        if config is None:
            return await call_next(request)

        account_key = await _extract_account_key(request, config.account_key_field)
        if account_key is None:
            # Nothing to rate-limit against - let the request proceed to
            # the handler, which will reject a malformed body on its own
            # terms. This middleware's job is per-account protection, not
            # request validation.
            return await call_next(request)

        source_ip = _client_ip(request)

        decision = await self._limiter.check(endpoint=config.endpoint, account_key=account_key)
        if not decision.allowed:
            headers = (
                {"Retry-After": str(decision.retry_after_seconds)}
                if decision.retry_after_seconds is not None
                else {}
            )
            # FR-UX-007: this is met on the SIGN-IN screen, before any account
            # is established, by somebody who is very likely mistyping a
            # password. It is the last refusal in the product that should be
            # in a language they cannot read - and it needs no token to be
            # localised, which is the same reason IAM-010g puts a language
            # control on that screen.
            return JSONResponse(
                status_code=429,
                headers=headers,
                content={
                    "detail": message(request, "errors.rate_limited"),
                    "reason": decision.reason,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
            )

        response = await call_next(request)

        outcome: AttemptOutcome = "success" if response.status_code < 400 else "failure"
        await self._limiter.record_attempt(
            endpoint=config.endpoint, account_key=account_key, source_ip=source_ip, outcome=outcome
        )

        return response

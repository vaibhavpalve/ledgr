"""NFR-032 at the HTTP edge: every mutating request carries a key, and a
retry cannot execute twice.

--- Why every mutating method, rather than a per-route declaration ---

The other guarantees here are opt-in per route: a route names its permission,
and names an audit category. That is right when the route knows something the
middleware cannot.

Idempotency is not like that. Every mutating endpoint wants identical
behaviour, so a per-route declaration would be ceremony - and a new endpoint
would be unprotected until someone performed it. Applying it to every
POST/PUT/PATCH/DELETE means a new endpoint is covered the moment it is
registered, and *opting out* is what takes a deliberate act.
tests/test_idempotency_coverage.py guards the opt-out list.

--- Where it sits in the chain ---

Inside MfaEnforcementMiddleware, outside AuthorizationEnforcementMiddleware:

  * inside MFA, so an unauthenticated or unverified request never claims a
    key. A key burned by a request that was refused at the door would make
    the caller's legitimate retry look like a duplicate.
  * outside authorization, so a 403 IS stored and replayed. That is
    deliberate: a denial is a deterministic answer to this exact request, and
    a retry would compute the same one.

--- What it does NOT do ---

It does not make the handler transactional. If a handler commits and then the
response fails to serialize, the key is released and a retry re-executes. The
domain-level key on journal_entry (0020) is what makes that safe for postings;
nothing makes it safe for an endpoint that has no such key, which is a reason
to give future mutating endpoints one.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

from api.db import engine
from api.idempotency import (
    HEADER,
    MUTATING_METHODS,
    REPLAY_HEADER,
    REPLAYABLE_HEADERS,
    IdempotencyError,
    IdempotencyStore,
    Outcome,
    RequestIdentity,
    StoredResponse,
    is_retryable_failure,
    validate_key,
)
from api.idempotency_repository import SqlIdempotencyRepository
from api.tenancy import TenantContext

#: Paths the framework serves that are not application endpoints.
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: Mutating routes that legitimately need no key. Every entry needs a reason,
#: and the bar is high: NFR-032 says "all mutating API endpoints", so an entry
#: here is a deviation from the requirement and should read as one.
#:
#:   (none)
#:
#: A route that changes tenant data does not belong here, however naturally
#: idempotent it looks. `PUT /v1/switcher/{id}` sets a session column and
#: re-sending it is harmless - and it is still NOT exempt, because "harmless
#: today" is a property of the current handler, not of the endpoint, and the
#: exemption would outlive the reasoning.
IDEMPOTENCY_EXEMPT_PATHS: frozenset[str] = frozenset()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, store: IdempotencyStore | None = None) -> None:
        super().__init__(app)
        # Injectable so tests can supply an in-memory store. When omitted, a
        # SQL-backed one over the shared engine - it opens its OWN sessions,
        # deliberately, so a claim is visible to concurrent requests before
        # the handler runs.
        self._store = store or IdempotencyStore(SqlIdempotencyRepository(engine))

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in MUTATING_METHODS:
            return await call_next(request)

        path_template = self._path_template(request)
        if path_template is None or path_template in FRAMEWORK_PATHS:
            return await call_next(request)
        if path_template in IDEMPOTENCY_EXEMPT_PATHS:
            return await call_next(request)

        tenant = getattr(request.state, "tenant_context", None)
        if not isinstance(tenant, TenantContext) or tenant.user_id is None:
            # No verified caller: there is nobody to scope the key to, and the
            # request is about to be refused by the gate that established
            # that. Passing it through means the refusal comes from the layer
            # that actually knows why.
            return await call_next(request)

        try:
            key = validate_key(request.headers.get(HEADER))
        except IdempotencyError as exc:
            return _problem(exc)

        # The body must be read here and made re-readable: it is half the
        # fingerprint, and Starlette's stream can only be consumed once.
        body = await request.body()
        identity = RequestIdentity(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            method=request.method,
            path_template=path_template,
            key=key,
            concrete_path=request.url.path,
            query_string=request.url.query,
            body=body,
        )

        claim = await self._store.claim(identity)

        if claim.outcome is Outcome.REPLAY and claim.response is not None:
            return _replay(claim.response)
        if claim.outcome is Outcome.IN_FLIGHT:
            return _conflict(key)
        if claim.outcome is Outcome.MISMATCH:
            return _mismatch(key)

        try:
            response = await call_next(request)
        except Exception:
            # The handler blew up. Release the claim so the caller's retry is
            # a retry rather than a permanent duplicate - the request's own
            # transaction has rolled back, so nothing was committed that a
            # second attempt could duplicate.
            await self._store.release(identity)
            raise

        stored = await _buffer(response)
        if is_retryable_failure(stored.status_code):
            await self._store.release(identity)
        else:
            await self._store.complete(identity, stored)

        return Response(
            content=stored.body,
            status_code=stored.status_code,
            # content-length is dropped so Response recomputes it from the
            # buffered body rather than carrying a header for a stream that
            # has already been consumed.
            headers={
                name: value
                for name, value in response.headers.items()
                if name.lower() != "content-length"
            },
            media_type=response.media_type,
        )

    def _path_template(self, request: Request) -> str | None:
        """The route's template, not the concrete path.

        `/v1/entries/{id}` rather than `/v1/entries/7`, so the key namespace
        is per endpoint. The concrete path is in the fingerprint instead,
        where reusing a key against a different id is reported as a mismatch
        rather than quietly becoming a different key.
        """
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match is Match.FULL:
                path = getattr(route, "path", None)
                return str(path) if path is not None else None
        return None


async def _buffer(response: Response) -> StoredResponse:
    """Materialise a streaming response so it can be stored and replayed.

    Unavoidable: a response that has been streamed to the client cannot also
    be written to the store. It bounds what this middleware can wrap - a large
    download would be held in memory - which is a reason mutating endpoints
    should not stream large bodies, and is why only mutating methods reach
    here at all.
    """
    chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
    body = b"".join(
        chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8") for chunk in chunks
    )
    return StoredResponse(
        status_code=response.status_code,
        body=body,
        headers={
            name.lower(): value
            for name, value in response.headers.items()
            if name.lower() in REPLAYABLE_HEADERS
        },
    )


def _replay(stored: StoredResponse) -> Response:
    headers = dict(stored.headers)
    headers[REPLAY_HEADER] = "true"
    return Response(content=stored.body, status_code=stored.status_code, headers=headers)


def _problem(exc: IdempotencyError) -> JSONResponse:
    # 400 for a missing or malformed key: the request cannot be processed as
    # sent, and the fix is entirely in the caller's hands.
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _conflict(key: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                f"a request with {HEADER} {key!r} is currently executing. Retry "
                "shortly; the original will have completed."
            )
        },
    )


def _mismatch(key: str) -> JSONResponse:
    # 422, per draft-ietf-httpapi-idempotency-key-header. Not 409: the request
    # does not conflict with server state, it contradicts an earlier request
    # that claimed the same key. Serving the first response instead would be
    # silently wrong in the most expensive way available - the caller would
    # believe their second, different operation had succeeded.
    return JSONResponse(
        status_code=422,
        content={
            "detail": (
                f"{HEADER} {key!r} was already used for a different request. An "
                "idempotency key identifies one operation; reuse it only when "
                "retrying that exact request."
            )
        },
    )

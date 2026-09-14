"""IAM-005 tenant-isolation coverage tracking and assertion helpers.

Two things live here:

1. registered_routes() / ISOLATION_COVERAGE_KEY: the coverage mechanism.
   Every FastAPI route that isn't explicitly tenant-free (api.tenancy.
   EXEMPT_PATHS) or a framework path (docs/openapi) is required to have a
   test marked @pytest.mark.isolation(method, path). Coverage is computed
   from markers at collection time (see conftest.py's
   pytest_collection_modifyitems), not from whether a DB-backed test body
   actually ran — that's deliberate. A route missing the marker fails CI
   even without a live database available, because "an isolation test
   exists for this route" and "the isolation test passed" are two
   different, both-necessary guarantees, and the first one is checkable for
   free on every CI run.

2. make_token / assert_tenant_isolated / assert_cannot_fetch_foreign_record:
   the actual assertion helpers isolation tests call against a live
   Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any

import jwt
import pytest
from httpx import AsyncClient, Response

from api.config import settings
from api.main import app
from api.tenancy import EXEMPT_PATHS
from tests.support.seed import SESSION_FOR_USER

FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

ISOLATION_COVERAGE_KEY = pytest.StashKey[set[tuple[str, str]]]()


def registered_routes() -> set[tuple[str, str]]:
    """Every (method, path) pair a client could actually call, minus the
    endpoints that carry no tenant context by design.
    """
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        if path in EXEMPT_PATHS or path in FRAMEWORK_PATHS:
            continue
        for method in methods:
            if method in IGNORED_METHODS:
                continue
            routes.add((method, path))
    return routes


def make_token(
    org_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    mfa_verified: bool = True,
    session_id: uuid.UUID | None = None,
) -> str:
    """A bearer token for tests, carrying everything the middleware chain
    needs to let a request reach a handler: org_id and sub
    (TenantContextMiddleware), and a `sid` naming a live session row - since
    ADR-060 the middleware resolves that row on every request and takes
    revocation, expiry and MFA state from it, so a token naming no session
    is refused at the door.

    When `session_id` is not given, the session tests.support.seed.seed_user
    created for this user is used (SESSION_FOR_USER). That is the REAL
    lookup, against a real row, on every one of these tests - nothing is
    skipped; the helper only spares each call site from restating which
    session a user it just seeded is signed in on. A user seeded another
    way, with no session, gets a token with no `sid` and is refused, which
    is the correct answer for such a token.

    `mfa_verified` is written into the token for the same reason
    api.auth.tokens writes it - the client reads it - but the server does
    not: the ROW's mfa_verified_at decides (seed_session sets it, by
    default). These are ISOLATION tests: they exist to prove a request that
    IS fully authenticated still cannot see another tenant's rows, and a
    token refused at the MFA gate would pass every leak assertion for the
    wrong reason. Tests specifically about the MFA gate live in
    tests/test_mfa_middleware.py.
    """
    claims: dict[str, object] = {"org_id": str(org_id), "mfa_verified": mfa_verified}
    if user_id is not None:
        claims["sub"] = str(user_id)
        if session_id is None:
            session_id = SESSION_FOR_USER.get(user_id)
    if session_id is not None:
        claims["sid"] = str(session_id)
    return jwt.encode(claims, settings.jwt_signing_key, algorithm="HS256")


async def assert_tenant_isolated(
    client: AsyncClient,
    method: str,
    path: str,
    *,
    as_org: uuid.UUID,
    as_user: uuid.UUID | None = None,
    foreign_record_ids: list[uuid.UUID | str],
    **request_kwargs: Any,
) -> Response:
    """Calls `method path` authenticated as `as_org` and asserts that none
    of `foreign_record_ids` — records seeded under a *different*
    organization — appear anywhere in the response body.

    This is deliberately shape-agnostic (a raw substring scan of the
    response text, not a parsed-JSON field check), so it works unmodified
    against a list endpoint, a single-object endpoint, or a nested one — the
    same helper is meant to cover "any endpoint," per IAM-005, without a
    bespoke assertion per response shape. Endpoints for which "not present
    anywhere" isn't precise enough (e.g. a get-by-id that should 404 rather
    than merely omit the record) should additionally use
    assert_cannot_fetch_foreign_record.
    """
    token = make_token(as_org, user_id=as_user)
    headers = {"Authorization": f"Bearer {token}", **request_kwargs.pop("headers", {})}
    response = await client.request(method, path, headers=headers, **request_kwargs)

    assert response.status_code < 500, (
        f"{method} {path} errored while asserting isolation: {response.status_code} {response.text}"
    )

    body_text = response.text
    leaked = [str(rid) for rid in foreign_record_ids if str(rid) in body_text]
    assert not leaked, (
        f"{method} {path} leaked records belonging to another tenant: {leaked}\n"
        f"response body: {body_text}"
    )
    return response


async def assert_cannot_fetch_foreign_record(
    client: AsyncClient,
    path_template: str,
    *,
    foreign_id: uuid.UUID,
    as_org: uuid.UUID,
    as_user: uuid.UUID | None = None,
    expected_status: int = 404,
) -> None:
    """For get-by-id style routes: fetching a record that belongs to another
    tenant must not return it, and must not let a caller distinguish "exists
    but not yours" from "does not exist."

    expected_status defaults to 404 — the RLS answer, where the row is
    filtered out of the query and the handler genuinely cannot tell the
    difference. A route carrying an administration-scoped authorization
    check (IAM-030) answers 403 slightly earlier instead, because the
    permission check runs before the query does; that is still
    indistinguishable between "another tenant's record" and "a record of
    yours you lack permission on," which is the property that matters. Tests
    pass the status their route actually produces so this stays an
    assertion rather than a shrug.
    """
    token = make_token(as_org, user_id=as_user)
    path = path_template.format(id=foreign_id)
    response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == expected_status, (
        f"expected {expected_status} fetching another tenant's record at {path}, got "
        f"{response.status_code}: {response.text}"
    )

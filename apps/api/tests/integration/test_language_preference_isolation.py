"""IAM-005 isolation tests for /v1/me/language, run against a real Postgres.

--- Why these look different from the other isolation tests ---

Every other route in this suite is tenant-scoped, and its isolation test asks
"can org A see org B's rows". `users` carries no tenant column and no RLS at
all - users are global to the platform (0003), because one person can hold
grants in several organizations and a firm accountant necessarily does.

So the boundary this route has to hold is not the tenant one. It is narrower:
a caller reads and writes THEIR OWN row and no other, and the user id comes
from the verified token rather than from anything the request can name. These
tests assert that boundary, which is the one that actually exists here - an
isolation test that asserted the tenant boundary on a global table would pass
without proving anything.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants, seed_session


async def _stored_language(user_id: uuid.UUID) -> str | None:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("SELECT language FROM users WHERE id = :id"), {"id": str(user_id)}
        )
        row = result.first()
        return None if row is None else row.language


@pytest.mark.isolation("GET", "/v1/me/language")
async def test_reading_a_language_returns_only_the_callers_own(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Give org B's owner an explicit language, so "org A sees null" is a
        # real answer rather than the answer everybody gets before anyone has
        # chosen.
        async with app_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET language = 'en' WHERE id = :id"),
                {"id": str(two_organizations.owner_b)},
            )

        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/me/language",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.owner_b, two_organizations.org_b],
        )

        body = response.json()
        # FR-LOC-001b: null is a real state - this person has never chosen -
        # and is distinct from org B's owner having chosen English.
        assert body["language"] is None
        assert body["supported"] == ["en", "nl"]
        assert await _stored_language(two_organizations.owner_b) == "en"


@pytest.mark.isolation("PUT", "/v1/me/language")
async def test_setting_a_language_writes_only_the_callers_own_row(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            "/v1/me/language",
            json={"language": "en"},
            headers={
                "Authorization": f"Bearer {token}",
                # NFR-032: the middleware covers every mutating route, so this
                # header is required rather than optional.
                "Idempotency-Key": f"language-{uuid.uuid4()}",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"language": "en"}

        assert await _stored_language(two_organizations.owner_a) == "en"
        # The other organization's owner is untouched. There is no route that
        # names another user, so this is asserting that the route wrote where
        # the token said rather than anywhere the request could influence.
        assert await _stored_language(two_organizations.owner_b) is None


async def test_changing_language_does_not_disturb_the_session(
    two_organizations: SeededTenants,
) -> None:
    """FR-LOC-001a: "taking effect immediately without reload or
    re-authentication."

    "Without re-authentication" is a claim about what this endpoint does NOT
    do, and the only place it can be checked is here: the client half can show
    that no sign-in request is made, but only the database can show that the
    session this request arrived on is exactly as it was.

    The row is compared field by field rather than by "did it still exist".
    An implementation that rotated the token, moved `expires_at`, or set
    `revoked_at` would leave a session that exists and no longer works, and a
    person would meet that as being logged out for changing language.

    `last_active_at` is deliberately NOT in the comparison: since ADR-060
    every authenticated request touches it (it is IAM-016's idle-timeout
    heartbeat, and this request is activity). Moving it forward is the
    opposite of re-authentication - it is what keeps the session alive.
    """
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)

    async def session_row() -> tuple[object, ...]:
        async with app_engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT token_hash, expires_at, revoked_at, "
                    "       last_reauthenticated_at, active_administration_id "
                    "FROM sessions WHERE id = :id"
                ),
                {"id": str(session_id)},
            )
            return tuple(result.one())

    before = await session_row()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(
            two_organizations.org_a,
            user_id=two_organizations.owner_a,
            session_id=session_id,
        )
        response = await client.put(
            "/v1/me/language",
            json={"language": "en"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"language-{uuid.uuid4()}",
            },
        )

    assert response.status_code == 200
    assert await session_row() == before
    assert await _stored_language(two_organizations.owner_a) == "en"


async def test_an_unsupported_language_is_refused_without_writing(
    two_organizations: SeededTenants,
) -> None:
    """FR-LOC-001: the supported set is closed. A code outside it is a
    client bug or a probe, and either way must not leave a `users.language`
    the catalogue has no strings for - which would be a row that renders as a
    crash on every screen.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.put(
            "/v1/me/language",
            json={"language": "de"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"language-{uuid.uuid4()}",
                "Accept-Language": "en",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["reason"] == "unsupported_language"
        # FR-UX-007: the sentence names what IS available, in the caller's own
        # language, rather than telling them what they sent.
        assert "en, nl" in response.json()["detail"]["message"]
        assert await _stored_language(two_organizations.owner_a) is None


async def test_the_database_refuses_a_language_the_api_would_not_write(
    two_organizations: SeededTenants,
) -> None:
    """The CHECK in migration 0030, asserted independently of the route.

    The API validates before writing, and that is the first line rather than
    the only one - the same posture api.ledger.model takes about the ledger's
    invariants. A background job, a support script or a future endpoint
    writing this column directly is refused by the database.
    """
    with pytest.raises(Exception, match="users_language"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET language = 'de' WHERE id = :id"),
                {"id": str(two_organizations.owner_a)},
            )

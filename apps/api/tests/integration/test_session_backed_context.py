"""ADR-060 against a real Postgres: api.tenancy resolves every token's `sid`
through api.auth.session_validation.SqlSessionValidator, so what the
`sessions` row says is what the request gets - revocation on the very next
request, the switcher's write visible without a re-mint, a step-up visible
to the old token, and a spliced token refused. tests/test_tenant_context.py
proves the same logic over the in-memory validator; this file is the proof
that the SQL one is wired and behaves identically.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import time
import uuid

import pyotp
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants, grant_role, seed_session


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"test-{uuid.uuid4().hex}"}


async def _signup(client: AsyncClient) -> tuple[str, str, str]:
    email = f"session-ctx-{uuid.uuid4().hex}@example.com"
    password = "correct horse battery staple 9"  # noqa: S106 - test fixture
    response = await client.post(
        "/v1/auth/signup",
        json={
            "account_model": "self_managed",
            "organization_name": f"Session Ctx {uuid.uuid4().hex[:6]}",
            "kvk_number": "34281907",
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    return email, password, response.json()["access_token"]


async def test_a_seeded_users_token_resolves_its_real_session_row(
    two_organizations: SeededTenants,
) -> None:
    """The lookup every isolation test in this directory now goes through:
    make_token names the session seed_user created, and the middleware finds
    that row. A token for a session that does not exist is refused.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        real = await client.get(
            "/v1/whoami",
            headers=_headers(
                make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
            ),
        )
        assert real.status_code == 200

        phantom = await client.get(
            "/v1/whoami",
            headers=_headers(
                make_token(
                    two_organizations.org_a,
                    user_id=two_organizations.owner_a,
                    session_id=uuid.uuid4(),
                )
            ),
        )
    assert phantom.status_code == 401
    assert phantom.json()["reason"] == "session_not_found"


async def test_revoke_then_request_is_refused_immediately(
    two_organizations: SeededTenants,
) -> None:
    """The gap ADR-054 named. Nothing about the token changes between the
    two calls; only the row does.
    """
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/v1/whoami", headers=_headers(token))).status_code == 200

        async with app_engine.begin() as conn:
            await conn.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE id = :id"),
                {"id": str(session_id)},
            )

        refused = await client.get("/v1/whoami", headers=_headers(token))

    assert refused.status_code == 401
    assert refused.json()["reason"] == "session_revoked"


async def test_logout_ends_the_token_on_the_next_request() -> None:
    """The same property through the product's own door: sign up, use the
    token, log out, and the token no longer opens anything - not even the
    MFA-exempt paths that it could reach a moment before.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        _, _, token = await _signup(client)
        begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
        assert begin.status_code == 200

        logout = await client.post("/v1/auth/logout", headers=_headers(token))
        assert logout.status_code == 200

        after = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))

    assert after.status_code == 401
    assert after.json()["reason"] == "session_revoked"


async def test_a_step_up_is_visible_to_the_old_token_without_a_re_mint() -> None:
    """mfa_verified is read from the row. The TOTP endpoints still hand back
    a fresh token for the client's convenience; the ORIGINAL token, minted
    with mfa_verified=false, works on a protected route the moment the row
    says the factor was verified.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        _, _, token = await _signup(client)

        blocked = await client.get("/v1/switcher", headers=_headers(token))
        assert blocked.status_code == 403
        assert blocked.json()["reason"] == "not_enrolled"

        begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
        secret = begin.json()["secret"]
        confirm = await client.post(
            "/v1/auth/mfa/totp/enroll/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).at(time.time())},
            headers=_headers(token),
        )
        assert confirm.status_code == 200, confirm.text

        # Same token as before - not confirm.json()["access_token"].
        allowed = await client.get("/v1/switcher", headers=_headers(token))

    assert allowed.status_code == 200, allowed.text


async def test_a_switch_takes_effect_on_the_next_request_with_the_same_token(
    two_organizations: SeededTenants,
) -> None:
    """FR-FRM-000a / IAM-110: PUT /v1/switcher/{id} writes the row, and the
    header's GET /v1/switcher/active reads it back on the next request with
    no new token in between.
    """
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        before = await client.get("/v1/switcher/active", headers=_headers(token))
        assert before.status_code == 200 and before.json() is None

        switched = await client.put(
            f"/v1/switcher/{two_organizations.admin_a}", headers=_headers(token)
        )
        assert switched.status_code == 200, switched.text

        after = await client.get("/v1/switcher/active", headers=_headers(token))

    assert after.status_code == 200
    assert after.json() is not None
    assert after.json()["administration_id"] == str(two_organizations.admin_a)


async def test_a_token_naming_another_users_session_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """Owner A's `sub` with owner B's `sid`: a spliced token, refused as if
    the session did not exist and before any handler runs.
    """
    session_b = await seed_session(app_engine, user_id=two_organizations.owner_b)
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_b
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/whoami", headers=_headers(token))

    assert response.status_code == 401
    assert response.json()["reason"] == "session_user_mismatch"


async def test_every_request_touches_the_rows_last_active_at(
    two_organizations: SeededTenants,
) -> None:
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET last_active_at = now() - interval '10 minutes' WHERE id = :id"
            ),
            {"id": str(session_id)},
        )
    token = make_token(
        two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_id
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/v1/whoami", headers=_headers(token))).status_code == 200

    async with app_engine.begin() as conn:
        age = (
            await conn.execute(
                text(
                    "SELECT extract(epoch from now() - last_active_at) FROM sessions WHERE id = :id"
                ),
                {"id": str(session_id)},
            )
        ).scalar_one()
    assert float(age) < 60


async def test_an_idle_session_is_refused_and_an_expired_one_too(
    two_organizations: SeededTenants,
) -> None:
    """IAM-016 on real rows: thirty idle minutes for a privileged session
    (every session today - ADR-005), twelve hours absolute.
    """
    idle = await seed_session(app_engine, user_id=two_organizations.owner_a)
    expired = await seed_session(app_engine, user_id=two_organizations.owner_a)
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET last_active_at = now() - interval '31 minutes' WHERE id = :id"
            ),
            {"id": str(idle)},
        )
        await conn.execute(
            text(
                "UPDATE sessions SET created_at = now() - interval '13 hours', "
                "expires_at = now() - interval '1 hour', last_active_at = now() WHERE id = :id"
            ),
            {"id": str(expired)},
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        idle_response = await client.get(
            "/v1/whoami",
            headers=_headers(
                make_token(
                    two_organizations.org_a, user_id=two_organizations.owner_a, session_id=idle
                )
            ),
        )
        expired_response = await client.get(
            "/v1/whoami",
            headers=_headers(
                make_token(
                    two_organizations.org_a, user_id=two_organizations.owner_a, session_id=expired
                )
            ),
        )

    assert idle_response.status_code == 401
    assert idle_response.json()["reason"] == "session_idle_timeout"
    assert expired_response.status_code == 401
    assert expired_response.json()["reason"] == "session_expired"

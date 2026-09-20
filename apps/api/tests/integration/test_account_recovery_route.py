"""`POST /v1/auth/recover` (IAM-018, IAM-019) end to end against a real Postgres.

The recovery service was fully unit-tested behind fake repositories but had no
route. Like `/v1/auth/login`, the route runs on the bootstrap session (no
`app.current_org_id`), where a row-level-security policy can silently hide rows
a fake repository would happily return, so the only honest proof is the whole
path: signup, TOTP enrolment, recover, then sign in with the new password and
confirm the old one and the old session are dead.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import time
import uuid

import pyotp
from httpx import ASGITransport, AsyncClient

from api.main import app

OLD_PASSWORD = "correct horse battery staple 9"  # noqa: S105 - test fixture
NEW_PASSWORD = "a different long passphrase 42"  # noqa: S105 - test fixture


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Idempotency-Key": f"test-{uuid.uuid4().hex}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _enrolled_account(client: AsyncClient, email: str) -> tuple[str, str]:
    """Sign up and enrol TOTP. Returns (secret, first access token)."""
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "account_model": "self_managed",
            "organization_name": f"Recovery Test Org {uuid.uuid4().hex[:8]}",
            "kvk_number": "34281907",
            "email": email,
            "password": OLD_PASSWORD,
        },
        headers=_headers(),
    )
    assert signup.status_code == 200, signup.text
    token = signup.json()["access_token"]
    begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
    secret = begin.json()["secret"]
    confirm = await client.post(
        "/v1/auth/mfa/totp/enroll/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=_headers(token),
    )
    assert confirm.status_code == 200, confirm.text
    return secret, token


def _next_code(secret: str) -> str:
    # One step ahead of the enrolment code, so the replay guard does not refuse it.
    return pyotp.TOTP(secret).at(time.time() + 30)


async def test_recovery_sets_a_new_password_and_kills_the_old_one() -> None:
    email = f"recover-{uuid.uuid4().hex}@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        secret, _ = await _enrolled_account(client, email)

        recovered = await client.post(
            "/v1/auth/recover",
            json={"email": email, "code": _next_code(secret), "new_password": NEW_PASSWORD},
            headers=_headers(),
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json() == {"status": "recovered"}

        old = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": OLD_PASSWORD},
            headers=_headers(),
        )
        assert old.status_code == 401

        new = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": NEW_PASSWORD},
            headers=_headers(),
        )
        assert new.status_code == 200, new.text


async def test_recovery_revokes_sessions_that_existed_before_it() -> None:
    email = f"recover-sessions-{uuid.uuid4().hex}@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        secret, old_token = await _enrolled_account(client, email)
        assert (await client.get("/v1/me", headers=_headers(old_token))).status_code in (200, 403)

        recovered = await client.post(
            "/v1/auth/recover",
            json={"email": email, "code": _next_code(secret), "new_password": NEW_PASSWORD},
            headers=_headers(),
        )
        assert recovered.status_code == 200, recovered.text

        after = await client.get("/v1/me", headers={"Authorization": f"Bearer {old_token}"})
        assert after.status_code == 401


async def test_a_wrong_code_and_an_unknown_email_get_the_same_refusal() -> None:
    email = f"recover-wrong-{uuid.uuid4().hex}@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await _enrolled_account(client, email)

        wrong = await client.post(
            "/v1/auth/recover",
            json={"email": email, "code": "000000", "new_password": NEW_PASSWORD},
            headers=_headers(),
        )
        unknown = await client.post(
            "/v1/auth/recover",
            json={
                "email": f"nobody-{uuid.uuid4().hex}@example.com",
                "code": "000000",
                "new_password": NEW_PASSWORD,
            },
            headers=_headers(),
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["detail"]["reason"] == unknown.json()["detail"]["reason"]

        # And the password did not change.
        login = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": OLD_PASSWORD},
            headers=_headers(),
        )
        assert login.status_code == 200, login.text


async def test_a_weak_new_password_is_refused_after_the_factor_is_proven() -> None:
    email = f"recover-weak-{uuid.uuid4().hex}@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        secret, _ = await _enrolled_account(client, email)
        response = await client.post(
            "/v1/auth/recover",
            json={"email": email, "code": _next_code(secret), "new_password": "short"},
            headers=_headers(),
        )
        assert response.status_code == 422
        assert response.json()["detail"]["reason"] == "weak_password"


async def test_repeated_wrong_codes_are_rate_limited() -> None:
    email = f"recover-limit-{uuid.uuid4().hex}@example.com"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await _enrolled_account(client, email)
        statuses = []
        for _ in range(12):
            response = await client.post(
                "/v1/auth/recover",
                json={"email": email, "code": "000000", "new_password": NEW_PASSWORD},
                headers=_headers(),
            )
            statuses.append(response.status_code)
        assert 429 in statuses, statuses

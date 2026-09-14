"""Regression coverage for two bugs in the returning-user login path, both
found by running `/v1/auth/login` end-to-end against a real Postgres for the
first time (2026-09-14) rather than the fake repositories `tests/auth/`
drives:

1. `_home_organization_id` (api.auth.routes) ran a plain SELECT on
   `role_assignment` through `get_bootstrap_db_session` - deliberately the
   session with no `app.current_org_id` set. `role_assignment_select`'s RLS
   policy (0009_authorization.sql) reads `scope_id = app.current_org_id()`,
   which is `scope_id = NULL` on that session and therefore false for every
   row, unconditionally. Every returning-user login refused with
   `errors.no_organization`, for every account, regardless of how genuinely
   provisioned it was - see 0048_login_home_organization_lookup.sql for the
   SECURITY DEFINER fix.

2. Once (1) was fixed, `login`'s success path (mfa_verified still False)
   became reachable for the first time and immediately hit a second,
   independent bug: it called `session.commit()` and then ran another query
   (`_enrollment_status`) on the same session, which `get_bootstrap_db_session`
   opened as `session.begin()` - failing with SQLAlchemy's
   "Can't operate on closed transaction inside context manager." Fixed by
   moving that read before the commit. `login_google_callback` had the
   identical pattern for the identical reason and was fixed the same way;
   it is not separately covered here since the fix is mechanically the same
   one-line reorder proven correct below, on the shared helper both routes
   call.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import time
import uuid

import pyotp
from httpx import ASGITransport, AsyncClient

from api.main import app


def _idempotency_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Idempotency-Key": f"test-{uuid.uuid4().hex}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _signup(client: AsyncClient, *, email: str, password: str) -> dict[str, object]:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "account_model": "self_managed",
            "organization_name": f"Regression Test Org {uuid.uuid4().hex[:8]}",
            "kvk_number": "34281907",
            "email": email,
            "password": password,
        },
        headers=_idempotency_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fresh_totp_code(secret: str) -> str:
    """A code guaranteed not to collide with one already consumed a moment
    ago in the same test: `verify_code`'s replay guard (totp.py's
    `after_step`) correctly rejects a code from a step already used, and a
    fast-running test can otherwise compute the identical 30-second-window
    code twice. Forcing the timestamp one interval ahead gets a genuinely
    later step without an actual sleep - the same trick this bug's own
    manual reproduction script needed for the same reason.
    """
    return pyotp.TOTP(secret).at(time.time() + 30)


async def test_login_after_totp_enrollment_finds_the_organization_and_reports_it() -> None:
    """The primary reproduction: signup, enroll TOTP, sign OUT (conceptually
    - a fresh /v1/auth/login call is a new session either way), sign back IN
    with a password. Before the fix this 403'd with errors.no_organization
    on every attempt, for every account - this is not an edge case being
    guarded against, it is the ordinary "close the laptop, come back
    tomorrow" flow.
    """
    email = f"login-regress-{uuid.uuid4().hex}@example.com"
    password = "correct horse battery staple 9"  # noqa: S106 - test fixture, not a secret

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, email=email, password=password)
        first_token = signup["access_token"]
        assert signup["mfa_verified"] is False

        begin = await client.post(
            "/v1/auth/mfa/totp/enroll/begin", headers=_idempotency_headers(first_token)
        )
        assert begin.status_code == 200, begin.text
        secret = begin.json()["secret"]
        confirm = await client.post(
            "/v1/auth/mfa/totp/enroll/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=_idempotency_headers(first_token),
        )
        assert confirm.status_code == 200, confirm.text

        # The actual regression: this used to 403 unconditionally.
        relogin = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": password},
            headers=_idempotency_headers(),
        )
        assert relogin.status_code == 200, relogin.text
        body = relogin.json()
        assert body["mfa_verified"] is False
        # And the SPECIFIC content matters, not just "didn't crash": the
        # enrollment status has to correctly reflect the TOTP factor just
        # enrolled, which is exactly the read that used to run after the
        # now-closed commit.
        assert body["mfa"] == {"has_passkey": False, "has_totp": True}

        step_up = await client.post(
            "/v1/auth/mfa/totp/verify",
            json={"code": _fresh_totp_code(secret)},
            headers=_idempotency_headers(body["access_token"]),
        )
        assert step_up.status_code == 200, step_up.text
        assert step_up.json()["mfa_verified"] is True

        # The fully-verified token actually works against a real protected
        # route - proof this is a genuine session, not just a 200 status.
        verified_token = step_up.json()["access_token"]
        switcher = await client.get(
            "/v1/switcher", headers={"Authorization": f"Bearer {verified_token}"}
        )
        assert switcher.status_code == 200, switcher.text


async def test_login_still_refuses_a_wrong_password() -> None:
    """The fix touches the SUCCESS path only; a genuinely wrong password
    must still be refused, and with the same message a nonexistent email
    gets (asserted in the next test) - the login route must not become an
    account-existence oracle as a side effect of fixing the organization
    lookup.
    """
    email = f"wrongpass-{uuid.uuid4().hex}@example.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=email, password="the-real-password-123")  # noqa: S106

        response = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": "not-the-real-password"},
            headers=_idempotency_headers(),
        )
        assert response.status_code == 401
        assert response.json()["detail"]["reason"] == "invalid_credentials"


async def test_login_refuses_a_nonexistent_email_with_the_same_message() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/login",
            json={"email": f"nobody-{uuid.uuid4().hex}@example.com", "password": "whatever12345"},
            headers=_idempotency_headers(),
        )
        assert response.status_code == 401
        assert response.json()["detail"]["reason"] == "invalid_credentials"


async def test_a_wrong_totp_code_is_rejected_on_the_relogin_step_up() -> None:
    """The step-up path the fix unblocked also has to correctly REJECT a bad
    code, not merely accept a good one - a fix aimed at the happy path is
    exactly the kind of change that can silently loosen the failure path.
    """
    email = f"badcode-{uuid.uuid4().hex}@example.com"
    password = "correct horse battery staple 9"  # noqa: S106

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, email=email, password=password)
        token = signup["access_token"]

        begin = await client.post(
            "/v1/auth/mfa/totp/enroll/begin", headers=_idempotency_headers(token)
        )
        secret = begin.json()["secret"]
        await client.post(
            "/v1/auth/mfa/totp/enroll/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=_idempotency_headers(token),
        )

        relogin = await client.post(
            "/v1/auth/login",
            json={"email": email, "password": password},
            headers=_idempotency_headers(),
        )
        unverified_token = relogin.json()["access_token"]

        bad = await client.post(
            "/v1/auth/mfa/totp/verify",
            json={"code": "000000"},
            headers=_idempotency_headers(unverified_token),
        )
        assert bad.status_code == 422
        assert bad.json()["detail"]["reason"] == "invalid_code"

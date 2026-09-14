"""IAM-010 / IAM-012, end to end over HTTP against a real Postgres: a person
signs up with a password, enrols a passkey through the MFA routes, signs out,
signs back in with the passkey alone (login/passkey/begin + finish) and lands
with mfa_verified=true in one step, then manages the passkey from the
account settings (api.account.security_routes). Every WebAuthn byte is real
- tests/auth/webauthn_helpers.FakeAuthenticator holds a genuine EC key and
signs the genuine challenge - so the `webauthn` library's verification path
runs for real on every leg.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import json
import time
import uuid

import pyotp
from httpx import ASGITransport, AsyncClient
from webauthn.helpers import base64url_to_bytes

from api.main import app
from tests.auth.webauthn_helpers import (
    FakeAuthenticator,
    authentication_credential_json,
    registration_credential_json,
)

_RP_ID = "localhost"
_ORIGIN = "http://localhost:5173"
_PASSWORD = "correct horse battery staple 9"  # noqa: S106 - test fixture


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Idempotency-Key": f"test-{uuid.uuid4().hex}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _challenge(options_json: str) -> bytes:
    return base64url_to_bytes(json.loads(options_json)["challenge"])


async def test_enrol_sign_out_sign_in_with_the_passkey_then_list_and_remove_it() -> None:
    email = f"passkey-e2e-{uuid.uuid4().hex}@example.com"
    authenticator = FakeAuthenticator()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Sign up: a real session, MFA not yet satisfied.
        signup = await client.post(
            "/v1/auth/signup",
            json={
                "account_model": "self_managed",
                "organization_name": f"Passkey E2E {uuid.uuid4().hex[:6]}",
                "kvk_number": "34281907",
                "email": email,
                "password": _PASSWORD,
            },
            headers=_headers(),
        )
        assert signup.status_code == 200, signup.text
        first_token = signup.json()["access_token"]

        # 2. Enrol the passkey through the MFA routes.
        begin = await client.post(
            "/v1/auth/mfa/passkey/enroll/begin", headers=_headers(first_token)
        )
        assert begin.status_code == 200, begin.text
        registration = authenticator.build_registration_credential(
            rp_id=_RP_ID, origin=_ORIGIN, challenge=_challenge(begin.json()["options"])
        )
        finish = await client.post(
            "/v1/auth/mfa/passkey/enroll/finish",
            json={
                "ceremony_id": begin.json()["ceremony_id"],
                "name": "Windows Hello",
                "credential": registration_credential_json(registration),
            },
            headers=_headers(first_token),
        )
        assert finish.status_code == 200, finish.text
        assert finish.json()["mfa_verified"] is True

        # 3. Sign out. The token is dead on the next request (ADR-060).
        logout = await client.post("/v1/auth/logout", headers=_headers(first_token))
        assert logout.status_code == 200, logout.text
        dead = await client.get("/v1/me/passkeys", headers=_headers(first_token))
        assert dead.status_code == 401
        assert dead.json()["reason"] == "session_revoked"

        # 4. Sign in with the passkey alone: no email typed, no password, no
        #    separate MFA step - IAM-012 counts the passkey as the second
        #    factor, so the session lands mfa_verified=true.
        login_begin = await client.post("/v1/auth/login/passkey/begin", headers=_headers())
        assert login_begin.status_code == 200, login_begin.text
        assertion = authenticator.build_authentication_credential(
            rp_id=_RP_ID, origin=_ORIGIN, challenge=_challenge(login_begin.json()["options"])
        )
        login_finish = await client.post(
            "/v1/auth/login/passkey/finish",
            json={
                "ceremony_id": login_begin.json()["ceremony_id"],
                "credential": authentication_credential_json(assertion),
            },
            headers=_headers(),
        )
        assert login_finish.status_code == 200, login_finish.text
        assert login_finish.json()["mfa_verified"] is True
        token = login_finish.json()["access_token"]

        # ...and a protected route agrees, reading the ROW's mfa_verified_at.
        me = await client.get("/v1/me", headers=_headers(token))
        assert me.status_code == 200, me.text
        assert me.json()["mfa"] == {"has_totp": False, "has_passkey": True}

        # 5. List it.
        listed = await client.get("/v1/me/passkeys", headers=_headers(token))
        assert listed.status_code == 200, listed.text
        passkeys = listed.json()["passkeys"]
        assert [p["name"] for p in passkeys] == ["Windows Hello"]
        assert passkeys[0]["last_used_at"] is not None  # the sign-in just now
        passkey_id = passkeys[0]["id"]

        # 6. It is the only second factor (IAM-011): enrol an authenticator
        #    app first, then remove the passkey.
        only = await client.delete(f"/v1/me/passkeys/{passkey_id}", headers=_headers(token))
        assert only.status_code == 409
        assert only.json()["detail"]["reason"] == "last_mfa_factor"

        totp_begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
        secret = totp_begin.json()["secret"]
        totp_confirm = await client.post(
            "/v1/auth/mfa/totp/enroll/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).at(time.time())},
            headers=_headers(token),
        )
        assert totp_confirm.status_code == 200, totp_confirm.text

        removed = await client.delete(f"/v1/me/passkeys/{passkey_id}", headers=_headers(token))
        assert removed.status_code == 200, removed.text
        assert (await client.get("/v1/me/passkeys", headers=_headers(token))).json() == {
            "passkeys": []
        }

        # 7. The removed passkey can no longer sign in - IAM-010's
        #    "individually revocable", enforced.
        retry_begin = await client.post("/v1/auth/login/passkey/begin", headers=_headers())
        retry = await client.post(
            "/v1/auth/login/passkey/finish",
            json={
                "ceremony_id": retry_begin.json()["ceremony_id"],
                "credential": authentication_credential_json(
                    authenticator.build_authentication_credential(
                        rp_id=_RP_ID,
                        origin=_ORIGIN,
                        challenge=_challenge(retry_begin.json()["options"]),
                    )
                ),
            },
            headers=_headers(),
        )
        assert retry.status_code == 401
        assert retry.json()["detail"]["reason"] == "passkey_authentication_failed"

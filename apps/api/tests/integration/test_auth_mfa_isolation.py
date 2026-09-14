"""IAM-005 isolation tests for the part of api.auth.routes that
tests/support/isolation.registered_routes still requires coverage for:
logout and the MFA enrolment/step-up-verification endpoints. Signup, login,
passkey sign-in and Google sign-in are NOT here - they run before any tenant
exists (api.tenancy.EXEMPT_PATHS), so IAM-005's tenant-isolation question
does not apply to them the way it does to every other route.

--- Why these look like tests/integration/test_language_preference_isolation.py ---

user_totp_credential, user_passkey and sessions all carry no organization_id
and no RLS, for the same reason `users` doesn't (0003/0005/0006's own
comments). The boundary that actually exists is narrower than the tenant
one: a caller acts on THEIR OWN MFA factors and session, identified from the
verified token, never from anything the request names. Two real people at
two different, otherwise identical-looking organizations are used anyway
(via `two_organizations`) so the assertions are "owner B's rows are
untouched", not merely "no rows exist to leak."
"""

from __future__ import annotations

import json
import uuid

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from webauthn.helpers import base64url_to_bytes

from api.db import engine as app_engine
from api.main import app
from tests.auth.webauthn_helpers import (
    FakeAuthenticator,
    authentication_credential_json,
    registration_credential_json,
)
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants, seed_session


def _headers(token: str) -> dict[str, str]:
    # NFR-032: every mutating route requires this - these routes are
    # tenant-scoped (unlike signup/login), so api.idempotency_middleware
    # does not exempt them.
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"test-{uuid.uuid4().hex}"}


async def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """A token bound to a REAL session row, not just make_token's bare
    org_id/sub claims: api.auth.routes._require_user (every handler in this
    file's target, via MFA_EXEMPT_PATHS) additionally requires a `sid` claim
    - `tenant.session_id is None` alone is enough to fail it with
    errors.not_authenticated, regardless of user_id being present. Every
    endpoint under test here needs this, not only logout.
    """
    session_id = await seed_session(app_engine, user_id=user_id)
    return make_token(org_id, user_id=user_id, session_id=session_id)


async def _totp_credential_count(user_id: uuid.UUID) -> int:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM user_totp_credential WHERE user_id = :id"),
            {"id": str(user_id)},
        )
        return result.scalar_one()


async def _passkey_row(user_id: uuid.UUID) -> tuple[object, ...] | None:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT credential_id, sign_count, revoked_at FROM user_passkey WHERE user_id = :id"
            ),
            {"id": str(user_id)},
        )
        row = result.first()
        return None if row is None else tuple(row)


def _challenge_from(options_json: str) -> bytes:
    """What a real client does before calling navigator.credentials.create()
    /get(): decode the base64url "challenge" field out of the WebAuthn JSON
    options this endpoint returned. FakeAuthenticator needs the raw bytes to
    sign over.
    """
    return base64url_to_bytes(json.loads(options_json)["challenge"])


async def _enroll_totp(client: AsyncClient, token: str) -> tuple[str, str]:
    """Enrolls a confirmed TOTP factor and returns (secret, the code that
    confirmed it) - the caller needs the code back because re-submitting it
    to /verify is the only way to exercise replay protection (IAM-012)
    deterministically: a genuinely fresh code requires waiting for the next
    30-second step, which a test suite should not do.
    """
    begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
    assert begin.status_code == 200
    secret = begin.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = await client.post(
        "/v1/auth/mfa/totp/enroll/confirm",
        json={"secret": secret, "code": code},
        headers=_headers(token),
    )
    assert confirm.status_code == 200, confirm.text
    return secret, code


async def _enroll_passkey(client: AsyncClient, token: str) -> FakeAuthenticator:
    begin = await client.post("/v1/auth/mfa/passkey/enroll/begin", headers=_headers(token))
    assert begin.status_code == 200
    body = begin.json()
    authenticator = FakeAuthenticator()
    credential = authenticator.build_registration_credential(
        rp_id="localhost",
        origin="http://localhost:5173",
        challenge=_challenge_from(body["options"]),
    )
    finish = await client.post(
        "/v1/auth/mfa/passkey/enroll/finish",
        json={
            "ceremony_id": body["ceremony_id"],
            "name": "Test Device",
            "credential": registration_credential_json(credential),
        },
        headers=_headers(token),
    )
    assert finish.status_code == 200, finish.text
    return authenticator


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/auth/logout")
async def test_logout_revokes_only_the_callers_own_session(
    two_organizations: SeededTenants,
) -> None:
    session_a = await seed_session(app_engine, user_id=two_organizations.owner_a)
    session_b = await seed_session(app_engine, user_id=two_organizations.owner_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(
            two_organizations.org_a, user_id=two_organizations.owner_a, session_id=session_a
        )
        response = await client.post("/v1/auth/logout", headers=_headers(token))
        assert response.status_code == 200

    async def _revoked_at(session_id: uuid.UUID) -> object:
        async with app_engine.begin() as conn:
            result = await conn.execute(
                text("SELECT revoked_at FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
            return result.scalar_one()

    assert await _revoked_at(session_a) is not None
    assert await _revoked_at(session_b) is None


# ---------------------------------------------------------------------------
# TOTP enrolment and verification
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/auth/mfa/totp/enroll/begin")
async def test_totp_enroll_begin_leaks_nothing_about_another_tenant(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "POST",
            "/v1/auth/mfa/totp/enroll/begin",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.owner_b, two_organizations.org_b],
            headers=_headers(await _token(two_organizations.org_a, two_organizations.owner_a)),
        )
        body = response.json()
        assert "secret" in body
        assert "provisioning_uri" in body


@pytest.mark.isolation("POST", "/v1/auth/mfa/totp/enroll/confirm")
async def test_totp_enroll_confirm_only_creates_a_credential_for_the_caller(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Owner B already has a real, confirmed TOTP credential of their
        # own - the "look-alike data" discipline from the language
        # isolation tests: the assertion below has to distinguish "untouched"
        # from "never existed."
        token_b = await _token(two_organizations.org_b, two_organizations.owner_b)
        await _enroll_totp(client, token_b)
        assert await _totp_credential_count(two_organizations.owner_b) == 1

        token_a = await _token(two_organizations.org_a, two_organizations.owner_a)
        begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token_a))
        secret = begin.json()["secret"]
        response = await client.post(
            "/v1/auth/mfa/totp/enroll/confirm",
            json={"secret": secret, "code": pyotp.TOTP(secret).now()},
            headers=_headers(token_a),
        )
        assert response.status_code == 200
        assert response.json()["mfa_verified"] is True

    assert await _totp_credential_count(two_organizations.owner_a) == 1
    assert await _totp_credential_count(two_organizations.owner_b) == 1


async def _last_used_step(user_id: uuid.UUID) -> object:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("SELECT last_used_step FROM user_totp_credential WHERE user_id = :id"),
            {"id": str(user_id)},
        )
        return result.scalar_one()


@pytest.mark.isolation("POST", "/v1/auth/mfa/totp/verify")
async def test_totp_verify_does_not_touch_another_users_credential(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_b = await _token(two_organizations.org_b, two_organizations.owner_b)
        await _enroll_totp(client, token_b)
        before_b = await _last_used_step(two_organizations.owner_b)

        token_a = await _token(two_organizations.org_a, two_organizations.owner_a)
        _secret_a, code_a = await _enroll_totp(client, token_a)

        # Re-submitting the code that just confirmed enrolment is a replay
        # (IAM-012's protection - see api.auth.totp._find_matching_step's
        # after_step guard), so this deterministically returns 422 without
        # depending on wall-clock timing the way a "generate one more valid
        # code" test would.
        response = await client.post(
            "/v1/auth/mfa/totp/verify",
            json={"code": code_a},
            headers=_headers(token_a),
        )
        assert response.status_code == 422

        assert await _last_used_step(two_organizations.owner_b) == before_b


# ---------------------------------------------------------------------------
# Passkey enrolment and step-up verification
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/auth/mfa/passkey/enroll/begin")
async def test_passkey_enroll_begin_leaks_nothing_about_another_tenant(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "POST",
            "/v1/auth/mfa/passkey/enroll/begin",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.owner_b, two_organizations.org_b],
            headers=_headers(await _token(two_organizations.org_a, two_organizations.owner_a)),
        )
        body = response.json()
        assert "ceremony_id" in body
        assert "options" in body


@pytest.mark.isolation("POST", "/v1/auth/mfa/passkey/enroll/finish")
async def test_passkey_enroll_finish_only_registers_a_credential_for_the_caller(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_b = await _token(two_organizations.org_b, two_organizations.owner_b)
        await _enroll_passkey(client, token_b)
        existing_b = await _passkey_row(two_organizations.owner_b)
        assert existing_b is not None

        token_a = await _token(two_organizations.org_a, two_organizations.owner_a)
        await _enroll_passkey(client, token_a)

    assert await _passkey_row(two_organizations.owner_a) is not None
    assert await _passkey_row(two_organizations.owner_b) == existing_b


@pytest.mark.isolation("POST", "/v1/auth/mfa/passkey/verify/begin")
async def test_passkey_verify_begin_leaks_nothing_about_another_tenant(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_a = await _token(two_organizations.org_a, two_organizations.owner_a)
        await _enroll_passkey(client, token_a)

        response = await assert_tenant_isolated(
            client,
            "POST",
            "/v1/auth/mfa/passkey/verify/begin",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.owner_b, two_organizations.org_b],
            headers=_headers(token_a),
        )
        body = response.json()
        assert "ceremony_id" in body
        assert "options" in body


@pytest.mark.isolation("POST", "/v1/auth/mfa/passkey/verify/finish")
async def test_passkey_verify_finish_cannot_be_completed_with_another_users_ceremony(
    two_organizations: SeededTenants,
) -> None:
    """The isolation property that actually matters here: a ceremony begun
    by A is bound to A's user id (SqlCeremonyRepository.create_passkey_ceremony),
    so B can never complete a verification using it - even if B could
    somehow obtain the ceremony id and a credential of their own.

    B's rejected attempt must not burn the ceremony for A either: the
    ownership check runs inside the same request transaction as the
    consuming UPDATE, with no explicit commit on the 403 path, so a hijack
    attempt rolls back rather than leaving the real owner locked out of
    their own in-flight verification - asserted below by A completing the
    SAME ceremony afterward.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_a = await _token(two_organizations.org_a, two_organizations.owner_a)
        authenticator_a = await _enroll_passkey(client, token_a)

        token_b = await _token(two_organizations.org_b, two_organizations.owner_b)
        await _enroll_passkey(client, token_b)
        existing_b = await _passkey_row(two_organizations.owner_b)

        begin = await client.post("/v1/auth/mfa/passkey/verify/begin", headers=_headers(token_a))
        ceremony_id = begin.json()["ceremony_id"]
        credential = authenticator_a.build_authentication_credential(
            rp_id="localhost",
            origin="http://localhost:5173",
            challenge=_challenge_from(begin.json()["options"]),
        )
        body = {
            "ceremony_id": ceremony_id,
            "credential": authentication_credential_json(credential),
        }

        # B attempts to finish A's ceremony with A's own credential.
        hijack = await client.post(
            "/v1/auth/mfa/passkey/verify/finish", json=body, headers=_headers(token_b)
        )
        assert hijack.status_code == 403

        # The legitimate owner can still complete the SAME ceremony
        # afterward - proving the 403 above rolled back rather than
        # consuming it.
        finish = await client.post(
            "/v1/auth/mfa/passkey/verify/finish", json=body, headers=_headers(token_a)
        )
        assert finish.status_code == 200, finish.text
        assert finish.json()["mfa_verified"] is True

        # Neither attempt touched owner B's own passkey.
        assert await _passkey_row(two_organizations.owner_b) == existing_b

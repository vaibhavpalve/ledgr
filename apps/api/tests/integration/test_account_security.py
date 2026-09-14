"""IAM-005 isolation coverage and behaviour for the account security
settings (api.account.security_routes - docs/founder-review-2026-09-14.md
§4.4) and IAM-010b's e-mail verification (api.auth.routes.verify_email,
resend_verification_email; api.auth.email_verification).

--- Why these look like tests/integration/test_auth_mfa_isolation.py ---

sessions, user_passkey, user_totp_credential and user_password_credential
carry no organization_id and no RLS (ADR-005): the boundary is narrower than
the tenant one - a caller acts on THEIR OWN rows, identified from the
verified token, never from anything the request names. Two real people at
two look-alike organizations are used anyway so the assertions are "owner
B's rows are untouched", not merely "no rows exist to leak."

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from webauthn.helpers import base64url_to_bytes

from api.db import engine as app_engine
from api.mail.outbox import clear_collected_messages, collected_messages
from api.main import app
from tests.auth.webauthn_helpers import FakeAuthenticator, registration_credential_json
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_administration,
    seed_session,
    seed_user,
    signup_organization,
)

_PASSWORD = "correct horse battery staple 9"  # noqa: S106 - test fixture


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"test-{uuid.uuid4().hex}"}


async def _signup(client: AsyncClient, *, language: str = "nl") -> dict[str, Any]:
    email = f"security-{uuid.uuid4().hex}@example.com"
    response = await client.post(
        "/v1/auth/signup",
        json={
            "account_model": "self_managed",
            "organization_name": f"Security Test {uuid.uuid4().hex[:6]}",
            "kvk_number": "34281907",
            "email": email,
            "password": _PASSWORD,
        },
        headers={"Accept-Language": language},
    )
    assert response.status_code == 200, response.text
    return {"email": email, "token": response.json()["access_token"]}


async def _enroll_totp(client: AsyncClient, token: str) -> tuple[str, str]:
    begin = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(token))
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    confirm = await client.post(
        "/v1/auth/mfa/totp/enroll/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).at(time.time())},
        headers=_headers(token),
    )
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["access_token"]


async def _enroll_passkey(client: AsyncClient, token: str, *, name: str) -> FakeAuthenticator:
    begin = await client.post("/v1/auth/mfa/passkey/enroll/begin", headers=_headers(token))
    assert begin.status_code == 200, begin.text
    body = begin.json()
    authenticator = FakeAuthenticator()
    credential = authenticator.build_registration_credential(
        rp_id="localhost",
        origin="http://localhost:5173",
        challenge=base64url_to_bytes(json.loads(body["options"])["challenge"]),
    )
    finish = await client.post(
        "/v1/auth/mfa/passkey/enroll/finish",
        json={
            "ceremony_id": body["ceremony_id"],
            "name": name,
            "credential": registration_credential_json(credential),
        },
        headers=_headers(token),
    )
    assert finish.status_code == 200, finish.text
    return authenticator


async def _live_session_ids(user_id: uuid.UUID) -> set[uuid.UUID]:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("SELECT id FROM sessions WHERE user_id = :id AND revoked_at IS NULL"),
            {"id": str(user_id)},
        )
        return {row.id for row in result}


async def _audit_actions(organization_id: uuid.UUID) -> list[str]:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text(
                "SELECT action FROM audit_log WHERE organization_id = :org "
                "AND category = 'authentication' ORDER BY sequence_number"
            ),
            {"org": str(organization_id)},
        )
        return [row.action for row in result]


async def _organization_of(token: str) -> uuid.UUID:
    import jwt

    from api.config import settings

    return uuid.UUID(jwt.decode(token, settings.jwt_signing_key, algorithms=["HS256"])["org_id"])


# ---------------------------------------------------------------------------
# Sessions - IAM-017
# ---------------------------------------------------------------------------


@pytest.mark.isolation("GET", "/v1/me/sessions")
async def test_listing_sessions_shows_only_the_callers_own(
    two_organizations: SeededTenants,
) -> None:
    extra_a = await seed_session(app_engine, user_id=two_organizations.owner_a)
    extra_b = await seed_session(app_engine, user_id=two_organizations.owner_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/me/sessions",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[extra_b, two_organizations.owner_b],
        )

    body = response.json()
    ids = {entry["id"] for entry in body["sessions"]}
    assert str(extra_a) in ids
    current = [entry for entry in body["sessions"] if entry["is_current"]]
    assert len(current) == 1
    assert {"id", "created_at", "last_active_at", "expires_at", "mfa_verified", "location"} <= set(
        current[0]
    )


@pytest.mark.isolation("DELETE", "/v1/me/sessions/{session_id}")
async def test_revoking_a_session_ends_it_and_cannot_reach_another_users(
    two_organizations: SeededTenants,
) -> None:
    other_device_a = await seed_session(app_engine, user_id=two_organizations.owner_a)
    device_b = await seed_session(app_engine, user_id=two_organizations.owner_b)
    token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Owner A cannot end owner B's session - 404, indistinguishable from
        # a session that never existed.
        foreign = await client.delete(f"/v1/me/sessions/{device_b}", headers=_headers(token_a))
        assert foreign.status_code == 404
        assert foreign.json()["detail"]["reason"] == "session_not_found"

        # Owner A ends their own other device...
        revoked = await client.delete(
            f"/v1/me/sessions/{other_device_a}", headers=_headers(token_a)
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["was_current"] is False

        # ...and that device's token is refused on its very next request.
        stale = make_token(
            two_organizations.org_a, user_id=two_organizations.owner_a, session_id=other_device_a
        )
        assert (await client.get("/v1/whoami", headers=_headers(stale))).status_code == 401

    assert device_b in await _live_session_ids(two_organizations.owner_b)
    assert "session_revoke" in await _audit_actions(two_organizations.org_a)


# ---------------------------------------------------------------------------
# Passkeys - IAM-010, IAM-010f, IAM-011
# ---------------------------------------------------------------------------


@pytest.mark.isolation("GET", "/v1/me/passkeys")
async def test_listing_passkeys_shows_only_the_callers_own(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        await _enroll_passkey(client, token_b, name="Owner B laptop")
        token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        await _enroll_passkey(client, token_a, name="Owner A phone")

        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/me/passkeys",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.owner_b, "Owner B laptop"],
        )

    names = [entry["name"] for entry in response.json()["passkeys"]]
    assert names == ["Owner A phone"]


@pytest.mark.isolation("DELETE", "/v1/me/passkeys/{passkey_id}")
async def test_removing_a_passkey_respects_the_two_guards_and_never_touches_anothers(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        await _enroll_passkey(client, token_b, name="Owner B laptop")
        passkey_b = (await client.get("/v1/me/passkeys", headers=_headers(token_b))).json()[
            "passkeys"
        ][0]["id"]

        token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        await _enroll_passkey(client, token_a, name="Owner A phone")
        passkey_a = (await client.get("/v1/me/passkeys", headers=_headers(token_a))).json()[
            "passkeys"
        ][0]["id"]

        # Somebody else's passkey: 404, as for one that never existed.
        foreign = await client.delete(f"/v1/me/passkeys/{passkey_b}", headers=_headers(token_a))
        assert foreign.status_code == 404

        # Owner A (seeded with no password, no TOTP) holds one passkey: it is
        # both their only way in and their only second factor. IAM-010f's
        # instruction comes first.
        refused = await client.delete(f"/v1/me/passkeys/{passkey_a}", headers=_headers(token_a))
        assert refused.status_code == 409
        assert refused.json()["detail"]["reason"] == "last_sign_in_method"

        # A second passkey clears the sign-in guard, and is a second factor
        # too, so removal of the first is now allowed.
        await _enroll_passkey(client, token_a, name="Owner A key")
        removed = await client.delete(f"/v1/me/passkeys/{passkey_a}", headers=_headers(token_a))
        assert removed.status_code == 200, removed.text

        remaining = (await client.get("/v1/me/passkeys", headers=_headers(token_a))).json()
        assert [p["name"] for p in remaining["passkeys"]] == ["Owner A key"]

        untouched = (await client.get("/v1/me/passkeys", headers=_headers(token_b))).json()
        assert [p["id"] for p in untouched["passkeys"]] == [passkey_b]

    assert "passkey_revoke" in await _audit_actions(two_organizations.org_a)


async def test_the_last_second_factor_cannot_be_removed_when_a_password_exists() -> None:
    """IAM-011 for a password account: the passkey is not the only way in,
    but it IS the only second factor, and MFA has no opt-out.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _signup(client)
        await _enroll_passkey(client, account["token"], name="Phone")
        passkey_id = (
            await client.get("/v1/me/passkeys", headers=_headers(account["token"]))
        ).json()["passkeys"][0]["id"]

        refused = await client.delete(
            f"/v1/me/passkeys/{passkey_id}", headers=_headers(account["token"])
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["reason"] == "last_mfa_factor"

        await _enroll_totp(client, account["token"])
        allowed = await client.delete(
            f"/v1/me/passkeys/{passkey_id}", headers=_headers(account["token"])
        )
        assert allowed.status_code == 200, allowed.text


# ---------------------------------------------------------------------------
# TOTP - IAM-011
# ---------------------------------------------------------------------------


@pytest.mark.isolation("DELETE", "/v1/me/mfa/totp")
async def test_removing_totp_respects_iam_011_and_never_touches_anothers(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        await _enroll_totp(client, token_b)
        token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)

        nothing = await client.delete("/v1/me/mfa/totp", headers=_headers(token_a))
        assert nothing.status_code == 404
        assert nothing.json()["detail"]["reason"] == "not_enrolled"

        await _enroll_totp(client, token_a)
        only_factor = await client.delete("/v1/me/mfa/totp", headers=_headers(token_a))
        assert only_factor.status_code == 409
        assert only_factor.json()["detail"]["reason"] == "last_mfa_factor"

        await _enroll_passkey(client, token_a, name="Owner A phone")
        removed = await client.delete("/v1/me/mfa/totp", headers=_headers(token_a))
        assert removed.status_code == 200, removed.text

    async with app_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT user_id, revoked_at IS NULL AS active FROM user_totp_credential "
                    "WHERE user_id = ANY(cast(:ids as uuid[]))"
                ),
                {"ids": [str(two_organizations.owner_a), str(two_organizations.owner_b)]},
            )
        ).all()
    active = {row.user_id: row.active for row in rows}
    assert active[two_organizations.owner_a] is False
    assert active[two_organizations.owner_b] is True
    assert "totp_revoke" in await _audit_actions(two_organizations.org_a)


# ---------------------------------------------------------------------------
# Password - IAM-013, IAM-016
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/me/password")
async def test_changing_the_password_reauthenticates_and_ends_other_sessions(
    two_organizations: SeededTenants,
) -> None:
    """Covers IAM-005 as well: the change writes only the caller's own
    credential row, and owner B (seeded with no password) still has none.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _signup(client)
        _, verified_token = await _enroll_totp(client, account["token"])
        organization_id = await _organization_of(verified_token)

        # A second device, signed in with the password.
        relogin = await client.post(
            "/v1/auth/login",
            json={"email": account["email"], "password": _PASSWORD},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert relogin.status_code == 200, relogin.text
        other_device = relogin.json()["access_token"]

        wrong = await client.post(
            "/v1/me/password",
            json={"current_password": "not the password", "new_password": "a whole new phrase 42"},
            headers=_headers(verified_token),
        )
        assert wrong.status_code == 401
        assert wrong.json()["detail"]["reason"] == "invalid_current_password"

        weak = await client.post(
            "/v1/me/password",
            json={"current_password": _PASSWORD, "new_password": "short"},
            headers=_headers(verified_token),
        )
        assert weak.status_code == 422
        assert weak.json()["detail"]["reason"] == "weak_password"

        changed = await client.post(
            "/v1/me/password",
            json={"current_password": _PASSWORD, "new_password": "a whole new phrase 42"},
            headers=_headers(verified_token),
        )
        assert changed.status_code == 200, changed.text
        assert changed.json() == {"status": "password_changed", "other_sessions_revoked": 1}

        # The current session survives; the other device is signed out.
        assert (await client.get("/v1/whoami", headers=_headers(verified_token))).status_code == 200
        gone = await client.post("/v1/auth/mfa/totp/enroll/begin", headers=_headers(other_device))
        assert gone.status_code == 401

        # The new password works and the old one does not.
        old = await client.post(
            "/v1/auth/login",
            json={"email": account["email"], "password": _PASSWORD},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert old.status_code == 401
        new = await client.post(
            "/v1/auth/login",
            json={"email": account["email"], "password": "a whole new phrase 42"},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert new.status_code == 200, new.text

    actions = await _audit_actions(organization_id)
    assert "password_change" in actions

    async with app_engine.begin() as conn:
        b_credential = (
            await conn.execute(
                text("SELECT count(*) FROM user_password_credential WHERE user_id = :id"),
                {"id": str(two_organizations.owner_b)},
            )
        ).scalar_one()
    assert b_credential == 0


async def test_an_account_without_a_password_sets_its_first_one_without_a_current_one(
    two_organizations: SeededTenants,
) -> None:
    """IAM-010f's prompt, honoured: a Google-only (here: passkey-only) account
    adds a password from its MFA-verified session.
    """
    token_a = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/me/password",
            json={"new_password": "first password ever set 7"},
            headers=_headers(token_a),
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "password_set"


# ---------------------------------------------------------------------------
# E-mail verification - IAM-010b
# ---------------------------------------------------------------------------


def _link_token_for(email: str) -> uuid.UUID:
    """What a person does with the mail: opens the link. Here: the newest
    collected message to this address, and the token in its link.
    """
    message = next(m for m in reversed(collected_messages()) if m.to == email)
    line = next(line for line in message.body.splitlines() if "/verify-email?token=" in line)
    return uuid.UUID(line.rsplit("token=", 1)[1])


async def test_signup_sends_the_link_and_verifying_it_flips_the_account() -> None:
    clear_collected_messages()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _signup(client, language="en")
        _, verified_token = await _enroll_totp(client, account["token"])

        me = await client.get("/v1/me", headers=_headers(verified_token))
        assert me.status_code == 200, me.text
        assert me.json()["user"]["email_verified"] is False

        mail = next(m for m in collected_messages() if m.to == account["email"])
        assert "Confirm your email address" in mail.subject  # the signup screen's language
        token = _link_token_for(account["email"])

        # No bearer token: the link is opened wherever the mail is read.
        verified = await client.post(
            "/v1/auth/verify-email",
            json={"token": str(token)},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json() == {"status": "verified"}

        again = await client.post(
            "/v1/auth/verify-email",
            json={"token": str(token)},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert again.status_code == 410
        assert again.json()["detail"]["reason"] == "verification_link_invalid"

        me = await client.get("/v1/me", headers=_headers(verified_token))
        assert me.json()["user"]["email_verified"] is True

    assert "email_verify" in await _audit_actions(await _organization_of(verified_token))


@pytest.mark.isolation("POST", "/v1/auth/verify-email/resend")
async def test_resend_issues_a_new_link_for_the_callers_own_address_only(
    two_organizations: SeededTenants,
) -> None:
    clear_collected_messages()
    unverified = await seed_user(
        app_engine, email=f"unverified-{uuid.uuid4().hex[:8]}@example.com", email_verified=False
    )
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=unverified,
        role_name="Owner",
        scope_type="organization",
        scope_id=two_organizations.org_a,
    )
    token = make_token(two_organizations.org_a, user_id=unverified)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/v1/auth/verify-email/resend", headers=_headers(token))
        assert first.status_code == 200, first.text
        first_token = _link_token_for(first.json()["email"])

        second = await client.post("/v1/auth/verify-email/resend", headers=_headers(token))
        assert second.status_code == 200
        second_token = _link_token_for(second.json()["email"])
        assert second_token != first_token

        # The retired link no longer works; the newest does.
        stale = await client.post(
            "/v1/auth/verify-email",
            json={"token": str(first_token)},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert stale.status_code == 410
        fresh = await client.post(
            "/v1/auth/verify-email",
            json={"token": str(second_token)},
            headers={"Idempotency-Key": f"test-{uuid.uuid4().hex}"},
        )
        assert fresh.status_code == 200

        # Already verified: nothing to resend. And owner B (verified by
        # seed_user) was never mailed.
        done = await client.post("/v1/auth/verify-email/resend", headers=_headers(token))
        assert done.status_code == 409
        assert done.json()["detail"]["reason"] == "email_already_verified"

    recipients = {m.to for m in collected_messages()}
    assert all(r != "owner+b" for r in recipients)
    assert not any(r.startswith("owner+b") for r in recipients)


async def test_posting_to_the_ledger_requires_a_verified_address(
    two_organizations: SeededTenants,
) -> None:
    """The gate on a real posting route: an unverified Owner is refused with
    the instruction, before the handler looks for the expense at all.
    """
    unverified = await seed_user(
        app_engine, email=f"unverified-{uuid.uuid4().hex[:8]}@example.com", email_verified=False
    )
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=unverified,
        role_name="Owner",
        scope_type="organization",
        scope_id=two_organizations.org_a,
    )
    token = make_token(two_organizations.org_a, user_id=unverified)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_a}/expenses/{uuid.uuid4()}/posting",
            headers={**_headers(token), "Accept-Language": "nl"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "email_not_verified"
    assert "Bevestig eerst uw e-mailadres" in response.json()["detail"]["message"]


async def test_a_google_created_account_is_verified_at_creation() -> None:
    """IAM-010b's exemption, as data: GoogleSignInService.sign_in stamps the
    row because the identity provider already proved the address.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from api.auth.breach_check import build_breach_checker
    from api.auth.google_oidc import GoogleIdentity
    from api.auth.google_repository import SqlGoogleIdentityRepository
    from api.auth.google_signin import GoogleSignInService
    from api.auth.repository import SqlUserRepository
    from api.auth.service import AuthenticationService

    email = f"google-{uuid.uuid4().hex[:8]}@example.com"
    async with async_sessionmaker(app_engine, expire_on_commit=False)() as session, session.begin():
        users = SqlUserRepository(session)
        service = GoogleSignInService(
            users,
            SqlGoogleIdentityRepository(session),
            AuthenticationService(users, build_breach_checker("local")),
        )
        outcome = await service.sign_in(
            GoogleIdentity(subject=f"sub-{uuid.uuid4().hex}", email=email, name=None, picture=None)
        )
        assert not isinstance(outcome, tuple)
        created = await users.get_by_id(outcome.id)  # type: ignore[union-attr]

    assert created is not None and created.email_verified


async def test_a_firm_owner_can_manage_sessions_with_no_administration_grant_at_all() -> None:
    """The reason these routes are permission-exempt: a person with no grant
    on any administration (a fresh firm, an invitee) can still see and end
    their own sessions.
    """
    org = await signup_organization(app_engine, name="Fresh Firm", kvk="99999999")
    await seed_administration(app_engine, org_id=org, legal_name="Fresh Firm", legal_form="BV")
    user = await seed_user(app_engine, email=f"fresh-{uuid.uuid4().hex[:8]}@example.com")
    token = make_token(org, user_id=user)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/me/sessions", headers=_headers(token))

    assert response.status_code == 200, response.text
    assert len(response.json()["sessions"]) == 1

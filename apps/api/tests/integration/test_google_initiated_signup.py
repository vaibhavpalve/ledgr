"""FR-MDL-001/IAM-010: a Google identity that matches no existing account
can now finish signup, instead of being told to sign up with a password
first - api.auth.routes.signup_google, the login_google_callback branch
that hands it a one-time ticket, and migration 0047's widened
auth_ceremony.kind check. See docs/decisions/ADR-054-signup-and-login.md's
addendum.

Two levels of test:

  - The ticket + signup_google endpoint, exercised directly against a
    seeded bare user + google_signup ceremony row (the exact shape
    GoogleSignInService.sign_in + SqlCeremonyRepository.
    create_google_signup_ceremony would have produced) - this is the
    genuinely new storage/provisioning logic, and the fastest way to cover
    its edge cases (single-use, wrong ceremony kind, unknown ticket).
  - One full round trip through login_google_start -> login_google_callback
    -> signup_google, with a real RS256-signed ID token and a faked HTTP
    transport for the token endpoint (mirroring
    tests/auth/test_google_oidc.py's own technique) and Google's real JWKS
    endpoint replaced by an injected SigningKeyResolver - proving the
    callback's new signup_required branch is actually wired to that ticket
    correctly, not just the ticket-consuming half in isolation.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

import api.auth.routes as auth_routes
from api.auth.google_oidc import GoogleOidcClient
from api.db import engine as app_engine
from api.main import app

_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
_REDIRECT_URI = "https://ledgr.test/auth/google/callback"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")


class _StaticSigningKeyResolver:
    def __init__(self, key: object) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> object:
        return SimpleNamespace(key=self._key)


def _make_id_token(*, subject: str, email: str, nonce: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": _CLIENT_ID,
        "sub": subject,
        "email": email,
        "email_verified": True,
        "name": "Test User",
        "nonce": nonce,
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256")


def _install_fake_google(
    monkeypatch: pytest.MonkeyPatch, *, subject: str, email: str
) -> dict[str, str]:
    """Patches api.auth.routes.build_google_oidc_client (what the route
    handlers actually call) to a client whose token-endpoint transport and
    signing-key resolution are faked, the same seam
    tests/auth/test_google_oidc.py uses. The nonce Google's ID token must
    carry is only known once login_google_start has actually run (it is
    generated fresh per attempt), so the handler reads it from the returned
    mutable dict rather than a value baked in ahead of time - the test sets
    `pending["nonce"]` after parsing it out of the start response's
    authorization_url, before calling the callback.
    """
    pending: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        id_token = _make_id_token(subject=subject, email=email, nonce=pending["nonce"])
        return httpx.Response(200, json={"id_token": id_token, "token_type": "Bearer"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fake_client = GoogleOidcClient(
        client_id=_CLIENT_ID,
        client_secret="test-client-secret",
        redirect_uri=_REDIRECT_URI,
        http_client=http_client,
        signing_key_resolver=_StaticSigningKeyResolver(_PUBLIC_KEY),
    )
    monkeypatch.setattr(auth_routes, "build_google_oidc_client", lambda: fake_client)
    return pending


def _unique_email(local_part: str) -> str:
    """A fresh address per call, the same reason
    tests/support/seed.py's own helpers suffix with a random hex fragment
    rather than a fixed string: these tests run against a real, persistent
    Postgres (not a schema reset per test), so a fixed email collides with
    users.email's UNIQUE constraint on a second run - locally, or if this
    file is ever re-run without a fresh bootstrap between runs.
    """
    return f"{local_part}+{uuid.uuid4().hex[:8]}@example.com"


async def _seed_bare_google_user(*, email: str) -> uuid.UUID:
    """The exact row shape GoogleSignInService.sign_in's third branch
    produces for a first-seen identity (a bare `users` row, no
    role_assignment, no organization) - seeded directly so the
    signup_google tests below don't need to also drive a full OAuth round
    trip just to get to this starting state.
    """
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO users (email) VALUES (:email) RETURNING id"), {"email": email}
        )
        user_id: uuid.UUID = result.scalar_one()
        return user_id


async def _seed_google_signup_ceremony(*, user_id: uuid.UUID) -> uuid.UUID:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO auth_ceremony (kind, user_id, expires_at) "
                "VALUES ('google_signup', :user_id, now() + interval '5 minutes') "
                "RETURNING id"
            ),
            {"user_id": str(user_id)},
        )
        ticket_id: uuid.UUID = result.scalar_one()
        return ticket_id


@contextlib.asynccontextmanager
async def _admin_conn() -> AsyncIterator[AsyncConnection]:
    """A connection as the postgres superuser, for reads that need to cross
    tenant boundaries before this module even knows which tenant it created:
    role_assignment's own SELECT policy only shows a row whose
    scope_id = app.current_org_id() (migration 0009), and which organization
    that even is is the very thing these reads exist to discover, so there
    is no tenant context to set first.

    `SET ROLE ledgr_ops` (the pattern test_ledger_immutability.py and
    test_idempotency.py use for their own cross-tenant reads) does not work
    here: ledgr_ops's BYPASSRLS only exempts it from row-level security
    POLICIES, not from table-level GRANTs, and 0009 grants
    select/insert/update on role_assignment to ledgr_app alone - nothing
    ever grants ledgr_ops SELECT on this particular table (unlike
    journal_entry/idempotency_key, which it does hold). Connecting as the
    postgres superuser via TEST_DATABASE_ADMIN_URL (the same admin_engine
    pattern test_effective_dated_rules.py and
    test_referenced_table_privileges.py use) bypasses both layers at once,
    test-only.
    """
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not set")
    admin_engine = create_async_engine(
        _ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    try:
        async with admin_engine.begin() as conn:
            yield conn
    finally:
        await admin_engine.dispose()


async def _has_organization(user_id: uuid.UUID) -> bool:
    async with _admin_conn() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM role_assignment "
                "WHERE user_id = :user_id AND scope_type = 'organization'"
            ),
            {"user_id": str(user_id)},
        )
        return bool(result.scalar_one())


# ---------------------------------------------------------------------------
# The ticket + signup_google endpoint, seeded directly
# ---------------------------------------------------------------------------


async def test_signup_google_creates_organization_and_grants_founding_owner() -> None:
    user_id = await _seed_bare_google_user(email=_unique_email("new.google.user"))
    ticket = await _seed_google_signup_ceremony(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": str(ticket),
                "account_model": "self_managed",
                "organization_name": "Bakker Consultancy",
                "kvk_number": None,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mfa_verified"] is False
    assert body["mfa"] == {"has_passkey": False, "has_totp": False}
    assert "access_token" in body
    assert await _has_organization(user_id)


async def test_signup_google_with_firm_account_model_and_kvk_number() -> None:
    user_id = await _seed_bare_google_user(email=_unique_email("new.google.user"))
    ticket = await _seed_google_signup_ceremony(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": str(ticket),
                "account_model": "firm",
                "organization_name": "Bakker Accountants",
                "kvk_number": "87654321",
            },
        )

    assert response.status_code == 200, response.text

    async with _admin_conn() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT o.kind, o.kvk_number FROM organization o "
                    "JOIN role_assignment ra ON ra.scope_id = o.id "
                    "WHERE ra.user_id = :user_id AND ra.scope_type = 'organization'"
                ),
                {"user_id": str(user_id)},
            )
        ).one()
    assert row.kind == "firm"
    assert row.kvk_number == "87654321"


async def test_signup_google_ticket_is_single_use() -> None:
    user_id = await _seed_bare_google_user(email=_unique_email("new.google.user"))
    ticket = await _seed_google_signup_ceremony(user_id=user_id)
    body = {
        "ticket": str(ticket),
        "account_model": "self_managed",
        "organization_name": "Bakker",
        "kvk_number": None,
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/v1/auth/signup/google", json=body)
        assert first.status_code == 200

        second = await client.post("/v1/auth/signup/google", json=body)

    assert second.status_code == 410
    assert second.json()["detail"]["reason"] == "ceremony_not_found"


async def test_signup_google_rejects_an_unknown_ticket() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": str(uuid.uuid4()),
                "account_model": "self_managed",
                "organization_name": "Bakker",
                "kvk_number": None,
            },
        )

    assert response.status_code == 410
    assert response.json()["detail"]["reason"] == "ceremony_not_found"


async def test_signup_google_rejects_a_ceremony_of_the_wrong_kind() -> None:
    """A passkey ceremony's id must not double as a signup ticket just
    because both live in the same table - consume_google_signup_ceremony
    filters by kind, so this is refused the same way an unknown id is.
    """
    user_id = await _seed_bare_google_user(email=_unique_email("new.google.user"))
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO auth_ceremony (kind, webauthn_challenge, user_id, expires_at) "
                "VALUES "
                "('passkey_registration', :challenge, :user_id, now() + interval '5 minutes') "
                "RETURNING id"
            ),
            {"challenge": b"not-a-real-challenge", "user_id": str(user_id)},
        )
        wrong_kind_id = result.scalar_one()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": str(wrong_kind_id),
                "account_model": "self_managed",
                "organization_name": "Bakker",
                "kvk_number": None,
            },
        )

    assert response.status_code == 410
    assert response.json()["detail"]["reason"] == "ceremony_not_found"


async def test_signup_google_does_not_touch_another_pending_users_ticket() -> None:
    """Two Google identities finishing signup concurrently must not be able
    to consume each other's ticket, and completing one must not create an
    organization for the other.
    """
    user_a = await _seed_bare_google_user(email=_unique_email("new.google.user.a"))
    user_b = await _seed_bare_google_user(email=_unique_email("new.google.user.b"))
    ticket_a = await _seed_google_signup_ceremony(user_id=user_a)
    await _seed_google_signup_ceremony(user_id=user_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": str(ticket_a),
                "account_model": "self_managed",
                "organization_name": "Bakker A",
                "kvk_number": None,
            },
        )

    assert response.status_code == 200
    assert await _has_organization(user_a)
    assert not await _has_organization(user_b)


# ---------------------------------------------------------------------------
# Full round trip: login_google_start -> login_google_callback -> signup_google
# ---------------------------------------------------------------------------


async def test_full_round_trip_from_google_redirect_to_a_provisioned_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = f"google-sub-{uuid.uuid4().hex[:8]}"
    email = _unique_email("brand.new")
    pending = _install_fake_google(monkeypatch, subject=subject, email=email)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        start = await client.post("/v1/auth/login/google/start")
        assert start.status_code == 200
        query = parse_qs(urlparse(start.json()["authorization_url"]).query)
        pending["nonce"] = query["nonce"][0]

        callback = await client.post(
            "/v1/auth/login/google/callback",
            json={"code": "fake-authorization-code", "state": query["state"][0]},
        )
        assert callback.status_code == 200, callback.text
        callback_body = callback.json()
        assert callback_body["status"] == "signup_required"
        assert callback_body["email"] == email

        finish = await client.post(
            "/v1/auth/signup/google",
            json={
                "ticket": callback_body["ticket"],
                "account_model": "self_managed",
                "organization_name": "Brand New BV",
                "kvk_number": None,
            },
        )

    assert finish.status_code == 200, finish.text
    finish_body = finish.json()
    assert finish_body["mfa_verified"] is False
    assert "access_token" in finish_body

    async with app_engine.begin() as conn:
        user_id = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": email},
            )
        ).scalar_one()
        linked = (
            await conn.execute(
                text("SELECT google_subject FROM user_google_identity WHERE user_id = :user_id"),
                {"user_id": str(user_id)},
            )
        ).scalar_one()
    assert linked == subject
    assert await _has_organization(user_id)

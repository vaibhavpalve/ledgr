"""IAM-010c: a Google sign-in whose verified email matches an account created
with a password can now be linked to it - `login_google_callback`'s
`link_required` branch issues a `google_link` ticket (migration 0076) and
`POST /v1/auth/login/google/link` takes that ticket plus the existing
account's password. See docs/decisions/ADR-105-google-sign-in-links-an-
existing-account.md.

What these pin down, in the order an attacker or a tired user would hit them:

  - the happy path links the identity and signs in as the existing account;
  - a wrong password links nothing and keeps the ticket usable;
  - a ticket is single-use, and a ticket of another kind or an unknown id is
    refused the same way;
  - a ticket names one existing account: proving a DIFFERENT account's
    password does not link this Google identity to either of them (the
    account-takeover case IAM-010c exists for);
  - wrong passwords here spend the same login lockout as /v1/auth/login.

Uses the same fake-Google seam as test_google_initiated_signup.py: a real
RS256-signed ID token, a faked token endpoint, an injected signing key.
Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.integration.test_google_initiated_signup import _install_fake_google
from tests.integration.test_login_home_organization import _signup

_PASSWORD = "correct horse battery staple 9"  # noqa: S105 - test fixture, not a secret


def _email(local_part: str) -> str:
    return f"{local_part}+{uuid.uuid4().hex[:8]}@example.com"


async def _google_round_trip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, *, subject: str, email: str
) -> dict[str, object]:
    """start -> callback for a Google identity, returning the callback body."""
    pending = _install_fake_google(monkeypatch, subject=subject, email=email)
    start = await client.post("/v1/auth/login/google/start")
    assert start.status_code == 200, start.text
    query = parse_qs(urlparse(start.json()["authorization_url"]).query)
    pending["nonce"] = query["nonce"][0]
    callback = await client.post(
        "/v1/auth/login/google/callback",
        json={"code": "fake-authorization-code", "state": query["state"][0]},
    )
    assert callback.status_code == 200, callback.text
    body: dict[str, object] = callback.json()
    return body


async def _linked_subject(email: str) -> str | None:
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT g.google_subject FROM user_google_identity g "
                    "JOIN users u ON u.id = g.user_id WHERE u.email = :email"
                ),
                {"email": email},
            )
        ).scalar_one_or_none()


async def test_link_required_carries_a_ticket_and_links_with_the_right_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = _email("existing.owner")
    subject = f"google-sub-{uuid.uuid4().hex[:8]}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=email, password=_PASSWORD)

        callback = await _google_round_trip(client, monkeypatch, subject=subject, email=email)
        assert callback["status"] == "link_required"
        assert callback["email"] == email
        assert isinstance(callback["ticket"], str)
        assert await _linked_subject(email) is None  # nothing linked yet

        link = await client.post(
            "/v1/auth/login/google/link",
            json={"ticket": callback["ticket"], "password": _PASSWORD},
        )
        assert link.status_code == 200, link.text
        body = link.json()
        assert "access_token" in body
        # Google rarely asserts a second factor (IAM-010e): the ordinary MFA gate follows.
        assert body["mfa_verified"] is False
        assert await _linked_subject(email) == subject

        # From now on the same Google identity signs straight in, no link step.
        again = await _google_round_trip(client, monkeypatch, subject=subject, email=email)
        assert "access_token" in again
        assert "status" not in again


async def test_a_wrong_password_links_nothing_and_keeps_the_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = _email("typo.owner")
    subject = f"google-sub-{uuid.uuid4().hex[:8]}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=email, password=_PASSWORD)
        ticket = (await _google_round_trip(client, monkeypatch, subject=subject, email=email))[
            "ticket"
        ]

        wrong = await client.post(
            "/v1/auth/login/google/link", json={"ticket": ticket, "password": "not-it-at-all-1"}
        )
        assert wrong.status_code == 401
        assert wrong.json()["detail"]["reason"] == "invalid_credentials"
        assert await _linked_subject(email) is None

        right = await client.post(
            "/v1/auth/login/google/link", json={"ticket": ticket, "password": _PASSWORD}
        )
        assert right.status_code == 200, right.text
        assert await _linked_subject(email) == subject


async def test_the_ticket_is_single_use(monkeypatch: pytest.MonkeyPatch) -> None:
    email = _email("once.owner")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=email, password=_PASSWORD)
        ticket = (
            await _google_round_trip(
                client, monkeypatch, subject=f"google-sub-{uuid.uuid4().hex[:8]}", email=email
            )
        )["ticket"]
        body = {"ticket": ticket, "password": _PASSWORD}
        first = await client.post("/v1/auth/login/google/link", json=body)
        assert first.status_code == 200, first.text
        second = await client.post("/v1/auth/login/google/link", json=body)

    assert second.status_code == 410
    assert second.json()["detail"]["reason"] == "ceremony_not_found"


async def test_unknown_and_wrong_kind_tickets_are_refused_alike() -> None:
    async with app_engine.begin() as conn:
        user_id = (
            await conn.execute(
                text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
                {"email": _email("wrong.kind")},
            )
        ).scalar_one()
        signup_ticket = (
            await conn.execute(
                text(
                    "INSERT INTO auth_ceremony (kind, user_id, expires_at) "
                    "VALUES ('google_signup', :user_id, now() + interval '5 minutes') "
                    "RETURNING id"
                ),
                {"user_id": str(user_id)},
            )
        ).scalar_one()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for ticket in (uuid.uuid4(), signup_ticket):
            response = await client.post(
                "/v1/auth/login/google/link",
                json={"ticket": str(ticket), "password": _PASSWORD},
            )
            assert response.status_code == 410
            assert response.json()["detail"]["reason"] == "ceremony_not_found"


async def test_another_accounts_password_links_neither_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The takeover case: the attacker controls a Google identity verified for
    the VICTIM's email and owns a second Boeklite account of their own. The
    ticket names the victim's account, so the attacker's own (correct)
    password for THEIR account proves nothing about the victim's, and no link
    is made to either.
    """
    victim = _email("victim")
    attacker = _email("attacker")
    attacker_password = "attacker owns this one 77"  # noqa: S105 - test fixture
    subject = f"google-sub-{uuid.uuid4().hex[:8]}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=victim, password=_PASSWORD)
        await _signup(client, email=attacker, password=attacker_password)
        ticket = (await _google_round_trip(client, monkeypatch, subject=subject, email=victim))[
            "ticket"
        ]

        response = await client.post(
            "/v1/auth/login/google/link",
            json={"ticket": ticket, "password": attacker_password},
        )

    assert response.status_code == 401
    assert await _linked_subject(victim) is None
    assert await _linked_subject(attacker) is None


async def test_wrong_passwords_here_spend_the_login_lockout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IAM-019: the link leg is not a second, separate budget for guessing the
    existing account's password - its failures lock /v1/auth/login too."""
    email = _email("lockout.owner")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, email=email, password=_PASSWORD)
        ticket = (
            await _google_round_trip(
                client, monkeypatch, subject=f"google-sub-{uuid.uuid4().hex[:8]}", email=email
            )
        )["ticket"]

        statuses = []
        for _ in range(8):
            response = await client.post(
                "/v1/auth/login/google/link", json={"ticket": ticket, "password": "nope-nope-1"}
            )
            statuses.append(response.status_code)
        assert 429 in statuses, statuses

        login = await client.post("/v1/auth/login", json={"email": email, "password": _PASSWORD})

    assert login.status_code == 429
    assert await _linked_subject(email) is None

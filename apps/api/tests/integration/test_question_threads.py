"""FR-FRM-005's question threads against a real Postgres (migration 0080, ADR-111).

The cast:
  * client A   - org_a owns admin_a; owner_a holds Owner at org_a (organization-scoped).
  * the firm   - org_b, turned into a firm with an ACTIVE engagement on admin_a; `accountant`
                 holds Accountant on admin_a, granted from the firm's tenant (IAM-107).
  * client C   - another business with its own administration and Owner.
  * firm D     - another firm with no engagement on anything of client A's.

What this proves that the unit tests cannot: RLS and the grant join keep every route and the inbox
inside the caller's own reach (IAM-005), author_side really comes from the session, the append-only
grants hold, and the notification sweep mails the client's Owner - whose organization-scoped grant
the firm's own session cannot even see - exactly once.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest). Needs migration
0080 applied; the sweep test also needs OPS_DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from api.db import engine as app_engine
from api.mail.sender import CollectingEmailSender
from api.main import app
from api.questions.notifications import run_question_notifications
from tests.support.isolation import make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_administration,
    seed_user,
    signup_firm_organization,
    signup_organization,
)

_BASE = "/v1/administrations/{administration_id}/questions"
_THREAD = f"{_BASE}/{{thread_id}}"
_INBOX = "/v1/firm/inbox"


@dataclass(frozen=True)
class World:
    client_org: uuid.UUID
    admin: uuid.UUID
    owner: uuid.UUID
    firm_org: uuid.UUID
    accountant: uuid.UUID
    other_client_org: uuid.UUID
    other_admin: uuid.UUID
    other_owner: uuid.UUID
    other_firm_org: uuid.UUID
    other_firm_user: uuid.UUID


async def _as_org(org: uuid.UUID, sql: str, params: dict[str, Any]) -> list[Any]:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        result = await conn.execute(text(sql), params)
        return list(result) if result.returns_rows else []


async def _world(tenants: SeededTenants) -> World:
    # org_b becomes a firm engaged on admin_a: proposed by the firm, accepted by the client.
    await _as_org(
        tenants.org_b,
        "UPDATE organization SET kind = 'firm' WHERE id = :id",
        {"id": str(tenants.org_b)},
    )
    await _as_org(
        tenants.org_b,
        "INSERT INTO firm_engagement "
        "(firm_organization_id, administration_id, status, initiated_by) "
        "VALUES (:firm, :admin, 'pending', 'firm')",
        {"firm": str(tenants.org_b), "admin": str(tenants.admin_a)},
    )
    await _as_org(
        tenants.org_a,
        "UPDATE firm_engagement SET status = 'active' "
        "WHERE firm_organization_id = :firm AND administration_id = :admin",
        {"firm": str(tenants.org_b), "admin": str(tenants.admin_a)},
    )
    suffix = uuid.uuid4().hex[:8]
    accountant = await seed_user(app_engine, email=f"accountant+{suffix}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=tenants.org_b,
        user_id=accountant,
        role_name="Accountant",
        scope_type="administration",
        scope_id=tenants.admin_a,
        granted_by=tenants.owner_b,
    )

    other_client = await signup_organization(app_engine, name="De Vries Bouw", kvk="44444444")
    other_admin = await seed_administration(
        app_engine, org_id=other_client, legal_name="De Vries Bouw", legal_form="BV"
    )
    other_owner = await seed_user(app_engine, email=f"devries+{suffix}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=other_client,
        user_id=other_owner,
        role_name="Owner",
        scope_type="organization",
        scope_id=other_client,
    )

    other_firm = await signup_firm_organization(app_engine, name="Kantoor Elders", kvk="55555555")
    other_firm_user = await seed_user(app_engine, email=f"elders+{suffix}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=other_firm,
        user_id=other_firm_user,
        role_name="Owner",
        scope_type="organization",
        scope_id=other_firm,
    )
    return World(
        client_org=tenants.org_a,
        admin=tenants.admin_a,
        owner=tenants.owner_a,
        firm_org=tenants.org_b,
        accountant=accountant,
        other_client_org=other_client,
        other_admin=other_admin,
        other_owner=other_owner,
        other_firm_org=other_firm,
        other_firm_user=other_firm_user,
    )


async def _call(
    org: uuid.UUID, user: uuid.UUID, method: str, path: str, json: object = None
) -> Response:
    token = make_token(org, user_id=user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


def _url(template: str, **ids: uuid.UUID | str) -> str:
    return template.format(**{k: str(v) for k, v in ids.items()})


async def _firm_asks(w: World, subject: str = "Betaling KPN") -> dict[str, Any]:
    created = await _call(
        w.firm_org,
        w.accountant,
        "POST",
        _url(_BASE, administration_id=w.admin),
        {"subject": subject, "body": "Waar is deze betaling voor?", "author_side": "client"},
    )
    assert created.status_code == 200, created.text
    return dict(created.json())


@pytest.mark.isolation("POST", _BASE)
async def test_a_firm_question_is_the_firms_whatever_the_body_says(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    assert thread["awaiting"] == "client"
    assert thread["messages"][0]["author_side"] == "firm"  # "author_side": "client" was ignored

    # Another firm, and another client, cannot open a thread on these books.
    for org, user in ((w.other_firm_org, w.other_firm_user), (w.other_client_org, w.other_owner)):
        refused = await _call(
            org, user, "POST", _url(_BASE, administration_id=w.admin), {"subject": "s", "body": "b"}
        )
        assert refused.status_code in {403, 404}, refused.text

    # A record from nowhere is refused.
    dangling = await _call(
        w.firm_org,
        w.accountant,
        "POST",
        _url(_BASE, administration_id=w.admin),
        {
            "subject": "s",
            "body": "b",
            "resource_type": "bank_transaction",
            "resource_id": str(uuid.uuid4()),
        },
    )
    assert dangling.status_code == 422
    assert dangling.json()["detail"]["reason"] == "question_resource_not_found"


@pytest.mark.isolation("GET", _BASE)
async def test_threads_are_listed_only_to_the_administrations_participants(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)

    for org, user in ((w.client_org, w.owner), (w.firm_org, w.accountant)):
        listed = await _call(org, user, "GET", _url(_BASE, administration_id=w.admin))
        assert listed.status_code == 200, listed.text
        assert [t["id"] for t in listed.json()["threads"]] == [thread["id"]]

    # The client sees it unread; the firm that wrote it does not.
    client_view = await _call(w.client_org, w.owner, "GET", _url(_BASE, administration_id=w.admin))
    assert client_view.json()["threads"][0]["unread"] is True

    for org, user in ((w.other_firm_org, w.other_firm_user), (w.other_client_org, w.other_owner)):
        refused = await _call(org, user, "GET", _url(_BASE, administration_id=w.admin))
        assert refused.status_code in {403, 404}
        assert thread["id"] not in refused.text

    # Another client's own list never shows it either.
    theirs = await _call(
        w.other_client_org, w.other_owner, "GET", _url(_BASE, administration_id=w.other_admin)
    )
    assert theirs.status_code == 200
    assert thread["id"] not in theirs.text


@pytest.mark.isolation("GET", _THREAD)
async def test_opening_a_thread_reads_it_and_nobody_else_can(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)

    opened = await _call(
        w.client_org,
        w.owner,
        "GET",
        _url(_THREAD, administration_id=w.admin, thread_id=thread["id"]),
    )
    assert opened.status_code == 200, opened.text
    assert [m["body"] for m in opened.json()["messages"]] == ["Waar is deze betaling voor?"]
    listed = await _call(w.client_org, w.owner, "GET", _url(_BASE, administration_id=w.admin))
    assert listed.json()["threads"][0]["unread"] is False

    for org, user in ((w.other_firm_org, w.other_firm_user), (w.other_client_org, w.other_owner)):
        refused = await _call(
            org, user, "GET", _url(_THREAD, administration_id=w.admin, thread_id=thread["id"])
        )
        assert refused.status_code in {403, 404}
        assert "Waar is deze betaling" not in refused.text
    # Nor through the other client's own administration.
    sideways = await _call(
        w.other_client_org,
        w.other_owner,
        "GET",
        _url(_THREAD, administration_id=w.other_admin, thread_id=thread["id"]),
    )
    assert sideways.status_code == 404


@pytest.mark.isolation("POST", f"{_THREAD}/messages")
async def test_a_client_reply_flips_the_move_and_strangers_cannot_post(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    path = _url(f"{_THREAD}/messages", administration_id=w.admin, thread_id=thread["id"])

    reply = await _call(w.client_org, w.owner, "POST", path, {"body": "Het abonnement."})
    assert reply.status_code == 200, reply.text
    assert reply.json()["author_side"] == "client"
    assert reply.json()["thread"]["awaiting"] == "firm"

    for org, user in ((w.other_firm_org, w.other_firm_user), (w.other_client_org, w.other_owner)):
        refused = await _call(org, user, "POST", path, {"body": "ingebroken"})
        assert refused.status_code in {403, 404}
    sideways = await _call(
        w.other_client_org,
        w.other_owner,
        "POST",
        _url(f"{_THREAD}/messages", administration_id=w.other_admin, thread_id=thread["id"]),
        {"body": "ingebroken"},
    )
    assert sideways.status_code == 404

    opened = await _call(
        w.firm_org,
        w.accountant,
        "GET",
        _url(_THREAD, administration_id=w.admin, thread_id=thread["id"]),
    )
    assert [m["body"] for m in opened.json()["messages"]] == [
        "Waar is deze betaling voor?",
        "Het abonnement.",
    ]


@pytest.mark.isolation("POST", f"{_THREAD}/resolve")
async def test_resolving_closes_the_thread_for_participants_only(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    path = _url(f"{_THREAD}/resolve", administration_id=w.admin, thread_id=thread["id"])

    for org, user in ((w.other_firm_org, w.other_firm_user), (w.other_client_org, w.other_owner)):
        refused = await _call(org, user, "POST", path)
        assert refused.status_code in {403, 404}

    resolved = await _call(w.firm_org, w.accountant, "POST", path)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"

    late = await _call(
        w.client_org,
        w.owner,
        "POST",
        _url(f"{_THREAD}/messages", administration_id=w.admin, thread_id=thread["id"]),
        {"body": "nog iets"},
    )
    assert late.status_code == 409
    assert late.json()["detail"]["reason"] == "question_thread_resolved"


@pytest.mark.isolation("GET", _INBOX)
async def test_the_inbox_lists_only_granted_administrations(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    await _call(
        w.client_org,
        w.owner,
        "POST",
        _url(f"{_THREAD}/messages", administration_id=w.admin, thread_id=thread["id"]),
        {"body": "Het abonnement van de telefoon."},
    )
    # A thread on another client the firm has no engagement with.
    other = await _call(
        w.other_client_org,
        w.other_owner,
        "POST",
        _url(_BASE, administration_id=w.other_admin),
        {"subject": "Eigen vraag", "body": "Alleen voor ons"},
    )
    assert other.status_code == 200, other.text

    inbox = await _call(w.firm_org, w.accountant, "GET", f"{_INBOX}?unread=true&limit=20")
    assert inbox.status_code == 200, inbox.text
    body = inbox.json()
    assert [i["thread_id"] for i in body["items"]] == [thread["id"]]
    assert body["unread_count"] == 1
    assert body["items"][0]["excerpt"] == "Het abonnement van de telefoon."
    assert other.json()["id"] not in inbox.text

    # The client replied, so the thread awaits the firm: a reply, not a question still out.
    replies = (await _call(w.firm_org, w.accountant, "GET", f"{_INBOX}?awaiting=firm")).json()
    assert [i["thread_id"] for i in replies["items"]] == [thread["id"]]
    out = (await _call(w.firm_org, w.accountant, "GET", f"{_INBOX}?awaiting=client")).json()
    assert out == {"unread_count": 0, "items": []}

    # Another firm sees nothing of either client.
    stranger = await _call(w.other_firm_org, w.other_firm_user, "GET", _INBOX)
    assert stranger.status_code == 200
    assert stranger.json() == {"unread_count": 0, "items": []}

    # A client's own inbox spans its own books only.
    client = await _call(w.other_client_org, w.other_owner, "GET", _INBOX)
    assert [i["thread_id"] for i in client.json()["items"]] == [other.json()["id"]]
    assert thread["id"] not in client.text

    # Revoking the accountant's grant takes the client out of their inbox at once.
    await _as_org(
        w.firm_org,
        "UPDATE role_assignment SET revoked_at = now() "
        "WHERE user_id = :user AND scope_type = 'administration' AND scope_id = :admin",
        {"user": str(w.accountant), "admin": str(w.admin)},
    )
    after = await _call(w.firm_org, w.accountant, "GET", _INBOX)
    assert after.json()["items"] == []


async def test_messages_are_append_only(two_organizations: SeededTenants) -> None:
    w = await _world(two_organizations)
    await _firm_asks(w)
    for statement in (
        "UPDATE question_message SET body = 'herschreven'",
        "DELETE FROM question_message",
        "DELETE FROM question_thread",
        "UPDATE question_read SET read_at = now()",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _as_org(w.client_org, statement, {})


async def test_a_resolved_thread_cannot_be_reopened_in_the_database(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    await _call(
        w.firm_org,
        w.accountant,
        "POST",
        _url(f"{_THREAD}/resolve", administration_id=w.admin, thread_id=thread["id"]),
    )
    with pytest.raises(DBAPIError, match="resolved"):
        await _as_org(
            w.client_org,
            "UPDATE question_thread SET status = 'open', resolved_at = NULL, "
            "resolved_by_user_id = NULL WHERE id = :id",
            {"id": thread["id"]},
        )


async def test_the_sweep_mails_the_clients_owner_once_and_never_the_firm(
    two_organizations: SeededTenants,
) -> None:
    w = await _world(two_organizations)
    thread = await _firm_asks(w)
    [owner_email] = [
        row[0]
        for row in await _as_org(
            w.client_org, "SELECT email FROM users WHERE id = :id", {"id": str(w.owner)}
        )
    ]
    [accountant_email] = [
        row[0]
        for row in await _as_org(
            w.client_org, "SELECT email FROM users WHERE id = :id", {"id": str(w.accountant)}
        )
    ]

    ops = create_async_engine(os.environ["OPS_DATABASE_URL"])
    sender = CollectingEmailSender()
    try:
        for _ in range(2):  # the second run sends nothing new for this thread
            async with ops.connect() as conn:
                session = AsyncSession(bind=conn)
                await run_question_notifications(session, sender)
                await session.commit()
    finally:
        await ops.dispose()

    ours = [m for m in sender.sent if m.to in {owner_email, accountant_email}]
    assert [m.to for m in ours] == [owner_email]
    assert "Waar is deze betaling" not in ours[0].body  # never the question itself
    assert thread["subject"] not in ours[0].body

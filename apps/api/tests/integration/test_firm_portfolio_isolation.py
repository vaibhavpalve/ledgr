"""The firm home's second line of defence, against a real Postgres (ADR-109, IAM-005, IAM-107).

The other firm-home integration tests isolate a firm from an UNRELATED tenant, which RLS alone
already does. This file is the case RLS cannot settle on its own: one firm engaged on two clients,
and a staff member who holds a grant on only one of them. The firm session's RLS reaches both
administrations (`app.has_administration_access` follows the engagement), so only the grant join
plus `authorize()` per administration keeps client B out of this person's firm home.

    firm F      active engagement on admin A and on admin B
    accountant  Accountant, administration-scoped, on A only
                (parametrised: with and without Owner at the firm itself - an organization grant
                at the firm must not cascade to clients, ADR-059)
    firm G      a second firm with its own Owner and no engagement at all

Both clients carry the same shape of data: a pending HIGH booking proposal (a bank-paid receipt
and the statement line that paid it) and an open question thread.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.integration.test_bank_routes import (  # noqa: F401 - the autouse fixture is reused
    _balance,
    _bank_paid_receipt,
    _email_verified,
)
from tests.support.isolation import make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_user,
    signup_firm_organization,
)


async def _as_org(org_id: uuid.UUID, sql: str, **params: object) -> Any:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )
        result = await conn.execute(text(sql), params)
        return result.scalar_one() if result.returns_rows else None


async def _engage(firm: uuid.UUID, administration_id: uuid.UUID, *, client: uuid.UUID) -> None:
    await _as_org(
        firm,
        "INSERT INTO firm_engagement (firm_organization_id, administration_id, status, "
        "  initiated_by) VALUES (:firm, :admin, 'pending', 'firm')",
        firm=str(firm),
        admin=str(administration_id),
    )
    await _as_org(
        client,
        "UPDATE firm_engagement SET status = 'active' "
        "WHERE firm_organization_id = :firm AND administration_id = :admin",
        firm=str(firm),
        admin=str(administration_id),
    )


def _mirrored(tenants: SeededTenants) -> SeededTenants:
    """B seen as A, so the bank helpers (which always work on `*_a`) seed B's books."""
    return SeededTenants(
        org_a=tenants.org_b,
        org_b=tenants.org_a,
        admin_a=tenants.admin_b,
        admin_b=tenants.admin_a,
        owner_a=tenants.owner_b,
        owner_b=tenants.owner_a,
    )


async def _call(
    org: uuid.UUID, user: uuid.UUID, method: str, path: str, body: object = None
) -> Response:
    headers = {"Authorization": f"Bearer {make_token(org, user_id=user)}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(method, path, headers=headers, json=body)


@dataclass(frozen=True)
class Portfolio:
    tenants: SeededTenants
    firm: uuid.UUID
    accountant: uuid.UUID
    other_firm: uuid.UUID
    other_owner: uuid.UUID
    proposal_a: str
    proposal_b: str
    bank_b: str
    thread_a: str
    thread_b: str


async def _pending_proposal(tenants: SeededTenants) -> tuple[str, str]:
    world = await _bank_paid_receipt(tenants, settles_through="2000")
    assert world["suggestion"]["confidence"] == "high", world["suggestion"]
    proposal = await _as_org(
        tenants.org_a,
        "SELECT id FROM booking_proposal WHERE bank_transaction_id = :line AND status = 'pending'",
        line=world["transaction_id"],
    )
    return str(proposal), world["bank"]


async def _question(org: uuid.UUID, owner: uuid.UUID, administration_id: uuid.UUID) -> str:
    created = await _call(
        org,
        owner,
        "POST",
        f"/v1/administrations/{administration_id}/questions",
        {"subject": "Bonnetje tankstation", "body": "Welke bon hoort hierbij?"},
    )
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


@pytest_asyncio.fixture(params=[False, True], ids=["staff-only", "staff-and-firm-owner"])
async def portfolio(
    request: pytest.FixtureRequest, two_organizations: SeededTenants
) -> AsyncIterator[Portfolio]:
    tenants = two_organizations
    proposal_a, _ = await _pending_proposal(tenants)
    proposal_b, bank_b = await _pending_proposal(_mirrored(tenants))
    thread_a = await _question(tenants.org_a, tenants.owner_a, tenants.admin_a)
    thread_b = await _question(tenants.org_b, tenants.owner_b, tenants.admin_b)

    firm = await signup_firm_organization(app_engine, name="Kantoor Visser", kvk="44444444")
    await _engage(firm, tenants.admin_a, client=tenants.org_a)
    await _engage(firm, tenants.admin_b, client=tenants.org_b)
    accountant = await seed_user(
        app_engine, email=f"accountant+{uuid.uuid4().hex[:8]}@visser.example"
    )
    if request.param:
        await grant_role(
            app_engine,
            acting_org_id=firm,
            user_id=accountant,
            role_name="Owner",
            scope_type="organization",
            scope_id=firm,
        )
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=accountant,
        role_name="Accountant",
        scope_type="administration",
        scope_id=tenants.admin_a,
    )

    other_firm = await signup_firm_organization(app_engine, name="Kantoor Smit", kvk="55555555")
    other_owner = await seed_user(app_engine, email=f"owner+{uuid.uuid4().hex[:8]}@smit.example")
    await grant_role(
        app_engine,
        acting_org_id=other_firm,
        user_id=other_owner,
        role_name="Owner",
        scope_type="organization",
        scope_id=other_firm,
    )

    yield Portfolio(
        tenants=tenants,
        firm=firm,
        accountant=accountant,
        other_firm=other_firm,
        other_owner=other_owner,
        proposal_a=proposal_a,
        proposal_b=proposal_b,
        bank_b=bank_b,
        thread_a=thread_a,
        thread_b=thread_b,
    )


async def _sanity_rls_reaches_both(p: Portfolio) -> None:
    """The precondition that makes this file meaningful: the firm's session CAN see B."""
    seen = await _as_org(
        p.firm,
        "SELECT count(*) FROM administration WHERE id = ANY(CAST(:ids AS uuid[]))",
        ids=[str(p.tenants.admin_a), str(p.tenants.admin_b)],
    )
    assert seen == 2


def _never_b(p: Portfolio, response: Response) -> None:
    assert response.status_code == 200, response.text
    for foreign in (p.tenants.admin_b, p.proposal_b, p.thread_b):
        assert str(foreign) not in response.text, f"{foreign} leaked into {response.text}"


async def test_the_firm_home_shows_only_the_granted_client(portfolio: Portfolio) -> None:
    p = portfolio
    a = str(p.tenants.admin_a)
    await _sanity_rls_reaches_both(p)

    def firm(method: str, path: str, body: object = None) -> Any:
        return _call(p.firm, p.accountant, method, path, body)

    worklist = await firm("GET", "/v1/firm/worklist?chip=all&page_size=1000")
    _never_b(p, worklist)
    assert [r["administration_id"] for r in worklist.json()["rows"]] == [a]
    assert worklist.json()["chip_counts"]["all"] == 1

    summary = await firm("GET", "/v1/firm/summary")
    _never_b(p, summary)
    assert summary.json()["client_count"] == 1
    assert summary.json()["counts"]["auto_bookings"] == 1
    assert summary.json()["counts"]["open_questions"] == 1

    proposals = await firm("GET", "/v1/firm/proposals")
    _never_b(p, proposals)
    assert proposals.json()["total"] == 1
    assert [x["id"] for g in proposals.json()["groups"] for x in g["proposals"]] == [p.proposal_a]
    named = await firm("GET", f"/v1/firm/proposals?administration_ids={p.tenants.admin_b}")
    _never_b(p, named)
    assert named.json() == {"total": 0, "groups": []}

    inbox = await firm("GET", "/v1/firm/inbox?limit=100")
    _never_b(p, inbox)
    assert [i["thread_id"] for i in inbox.json()["items"]] == [p.thread_a]

    deadlines = await firm("GET", "/v1/firm/deadlines?from=2026-01-01&to=2026-12-31")
    _never_b(p, deadlines)

    # Per-client routes on B refuse outright.
    for path in (
        f"/v1/administrations/{p.tenants.admin_b}/proposals",
        f"/v1/administrations/{p.tenants.admin_b}/questions",
        f"/v1/administrations/{p.tenants.admin_b}/questions/{p.thread_b}",
    ):
        refused = await firm("GET", path)
        assert refused.status_code in {403, 404}, (path, refused.text)
        assert p.thread_b not in refused.text and p.proposal_b not in refused.text

    # Firm-home writes on B are refused too.
    snooze = await firm(
        "POST", f"/v1/firm/clients/{p.tenants.admin_b}/snooze", {"until": None, "reason": "x"}
    )
    assert snooze.status_code in {403, 404}, snooze.text
    assigned = await firm(
        "POST",
        "/v1/firm/clients/assign",
        {"administration_ids": [str(p.tenants.admin_b)], "user_id": str(p.accountant)},
    )
    # Without Owner at the firm the caller may not assign at all (manage user_role, 403); with it,
    # B is reported as not assignable.
    assert assigned.status_code in {200, 403}, assigned.text
    if assigned.status_code == 200:
        assert assigned.json()["assigned"] == 0


async def test_deciding_the_ungranted_clients_proposal_is_refused(portfolio: Portfolio) -> None:
    p = portfolio
    decided = await _call(
        p.firm,
        p.accountant,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": p.proposal_b, "decision": "approve"}]},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert (body["approved"], body["rejected"]) == (0, 0)
    [failure] = body["failed"]
    assert failure["proposal_id"] == p.proposal_b
    assert failure["reason"] in {"not_permitted", "proposal_not_found"}, failure

    status = await _as_org(
        p.tenants.org_b,
        "SELECT status FROM booking_proposal WHERE id = :id",
        id=p.proposal_b,
    )
    assert status == "pending"
    assert await _balance(_mirrored(p.tenants), p.bank_b) == "0.00"

    rejected = await _call(
        p.firm,
        p.accountant,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": p.proposal_b, "decision": "reject"}]},
    )
    assert rejected.json()["rejected"] == 0


async def test_the_firm_approves_its_granted_clients_proposal_once(portfolio: Portfolio) -> None:
    """The review sheet's whole point: firm staff approve a client's proposal from the firm's own
    session. It books exactly one journal entry in the client's books, and the decision is audited
    in the firm's trail (the session's organization), naming the client's administration.

    Was strict-xfail until ADR-112 / migration 0081: the posting's own audit entry belongs in the
    CLIENT's chain, which audit_log_insert did not admit from a firm session. The client's chain
    must still verify afterwards."""
    p = portfolio
    approve = {"decisions": [{"proposal_id": p.proposal_a, "decision": "approve"}]}

    async def entries() -> int:
        return int(
            await _as_org(
                p.tenants.org_a,
                "SELECT count(*) FROM journal_entry WHERE administration_id = :admin",
                admin=str(p.tenants.admin_a),
            )
        )

    before = await entries()
    for _ in range(2):  # a retry under a new key books nothing more (NFR-032)
        decided = await _call(p.firm, p.accountant, "POST", "/v1/firm/proposals/decide", approve)
        assert decided.status_code == 200, decided.text
        assert decided.json() == {"approved": 1, "rejected": 0, "failed": []}
    assert await entries() == before + 1

    audited = await _as_org(
        p.firm,
        "SELECT count(*) FROM audit_log WHERE action = 'approve_booking_proposal' "
        "AND resource_id = :id AND administration_id = :admin AND outcome = 'success'",
        id=p.proposal_a,
        admin=str(p.tenants.admin_a),
    )
    assert audited == 1

    # The posting's entry landed in the client's chain, not a fork of it.
    broken = await _as_org(
        p.tenants.org_a,
        "SELECT count(*) FROM app.verify_audit_chain(:org)",
        org=str(p.tenants.org_a),
    )
    assert broken == 0


async def test_a_second_firm_sees_nothing(portfolio: Portfolio) -> None:
    p = portfolio

    def other(method: str, path: str, body: object = None) -> Any:
        return _call(p.other_firm, p.other_owner, method, path, body)

    everyone = (
        p.tenants.admin_a,
        p.tenants.admin_b,
        p.proposal_a,
        p.proposal_b,
        p.thread_a,
        p.thread_b,
    )
    worklist = await other("GET", "/v1/firm/worklist?chip=all&page_size=1000")
    summary = await other("GET", "/v1/firm/summary")
    proposals = await other("GET", "/v1/firm/proposals")
    inbox = await other("GET", "/v1/firm/inbox")
    for response in (worklist, summary, proposals, inbox):
        assert response.status_code == 200, response.text
        for foreign in everyone:
            assert str(foreign) not in response.text
    assert worklist.json()["rows"] == []
    assert summary.json()["client_count"] == 0
    assert proposals.json() == {"total": 0, "groups": []}
    assert inbox.json() == {"unread_count": 0, "items": []}

    decided = await other(
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": p.proposal_a, "decision": "approve"}]},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["approved"] == 0

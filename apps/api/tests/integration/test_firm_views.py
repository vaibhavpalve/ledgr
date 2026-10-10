"""Saved views, next client and the worklist's wave-2 columns against a real Postgres (ADR-115).

    firm F      active engagements on admin A and admin B
    accountant  Accountant on A only          -> portfolio {A}
    colleague   Accountant on A and on B      -> portfolio {A, B}, same firm
    B's owner   an ordinary second tenant user

Both administrations have one unmatched credit (bank_to_match 1), so both are on "My move".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
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


async def _unmatched_credit(org: uuid.UUID, admin: uuid.UUID, *, tag: str) -> None:
    ledger_account = await _as_org(
        org,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    account = await _as_org(
        org,
        "INSERT INTO bank_account (organization_id, administration_id, name, ledger_account_id) "
        "VALUES (:org, :admin, :name, :ledger) RETURNING id",
        org=str(org),
        admin=str(admin),
        name=f"Rekening {tag}",
        ledger=str(ledger_account),
    )
    await _as_org(
        org,
        "INSERT INTO bank_transaction (organization_id, administration_id, bank_account_id, "
        "  booking_date, amount, description, external_id) "
        "VALUES (:org, :admin, :account, :on, CAST('100.00' AS numeric), :description, :ext)",
        org=str(org),
        admin=str(admin),
        account=str(account),
        on=date(2026, 9, 10),
        description=f"Betaling {tag}",
        ext=f"{tag}-credit",
    )


@dataclass(frozen=True)
class World:
    tenants: SeededTenants
    firm: uuid.UUID
    accountant: uuid.UUID
    colleague: uuid.UUID


@pytest_asyncio.fixture
async def world(two_organizations: SeededTenants) -> AsyncIterator[World]:
    tenants = two_organizations
    firm = await signup_firm_organization(app_engine, name="Kantoor Jansen", kvk="66666666")
    await _engage(firm, tenants.admin_a, client=tenants.org_a)
    await _engage(firm, tenants.admin_b, client=tenants.org_b)
    accountant = await seed_user(app_engine, email=f"acc+{uuid.uuid4().hex[:8]}@jansen.example")
    colleague = await seed_user(app_engine, email=f"col+{uuid.uuid4().hex[:8]}@jansen.example")
    for user, admins in (
        (accountant, [tenants.admin_a]),
        (colleague, [tenants.admin_a, tenants.admin_b]),
    ):
        for admin in admins:
            await grant_role(
                app_engine,
                acting_org_id=firm,
                user_id=user,
                role_name="Accountant",
                scope_type="administration",
                scope_id=admin,
            )
    await _unmatched_credit(tenants.org_a, tenants.admin_a, tag="AAA")
    await _unmatched_credit(tenants.org_b, tenants.admin_b, tag="BBB")
    yield World(tenants=tenants, firm=firm, accountant=accountant, colleague=colleague)


async def _call(
    org: uuid.UUID, user: uuid.UUID, method: str, path: str, body: object = None
) -> Response:
    headers = {"Authorization": f"Bearer {make_token(org, user_id=user)}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(method, path, headers=headers, json=body)


def _accountant(w: World, method: str, path: str, body: object = None) -> Any:
    return _call(w.firm, w.accountant, method, path, body)


def _colleague(w: World, method: str, path: str, body: object = None) -> Any:
    return _call(w.firm, w.colleague, method, path, body)


def _owner_b(w: World, method: str, path: str, body: object = None) -> Any:
    return _call(w.tenants.org_b, w.tenants.owner_b, method, path, body)


async def _save(w: World, name: str, query: dict[str, object] | None = None) -> dict[str, Any]:
    created = await _accountant(w, "POST", "/v1/firm/views", {"name": name, "query": query or {}})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


# --- next client ----------------------------------------------------------------------


@pytest.mark.isolation("GET", "/v1/firm/worklist/next")
async def test_next_walks_the_portfolio_and_never_an_ungranted_client(world: World) -> None:
    a, b = str(world.tenants.admin_a), str(world.tenants.admin_b)

    first = await _accountant(world, "GET", "/v1/firm/worklist/next")
    assert first.status_code == 200, first.text
    assert first.json()["administration_id"] == a
    assert first.json()["remaining"] == 1

    end = await _accountant(world, "GET", f"/v1/firm/worklist/next?after={a}")
    assert end.json() == {"administration_id": None, "display_name": None, "remaining": 0}

    # B is in the firm's RLS reach but not this person's grants: its position is never used.
    from_b = await _accountant(world, "GET", f"/v1/firm/worklist/next?after={b}")
    assert from_b.json()["administration_id"] == a
    assert b not in from_b.text

    # The colleague holds both: walking by name visits each once, then ends.
    seen: list[str] = []
    after = ""
    for _ in range(3):
        step = await _colleague(world, "GET", f"/v1/firm/worklist/next?sort=name{after}")
        assert step.status_code == 200, step.text
        if step.json()["administration_id"] is None:
            break
        seen.append(step.json()["administration_id"])
        after = f"&after={step.json()['administration_id']}"
    assert sorted(seen) == sorted([a, b])

    # Filters apply: a search matching nobody, or a VAT frequency nobody has, is the end at once.
    nobody = await _colleague(world, "GET", "/v1/firm/worklist/next?q=zzzz-nobody")
    assert nobody.json()["administration_id"] is None
    monthly = await _colleague(world, "GET", "/v1/firm/worklist/next?vat_frequency=monthly")
    assert monthly.json()["administration_id"] is None

    # Snoozed clients are skipped, even on a chip that lists them.
    until = (date.today() + timedelta(days=7)).isoformat()
    snoozed = await _colleague(world, "POST", f"/v1/firm/clients/{a}/snooze", {"until": until})
    assert snoozed.status_code == 204, snoozed.text
    only_b = await _colleague(world, "GET", "/v1/firm/worklist/next?chip=all")
    assert only_b.json()["administration_id"] == b
    assert only_b.json()["remaining"] == 1

    # B's own owner, in B's tenant, never reaches A.
    theirs = await _owner_b(world, "GET", f"/v1/firm/worklist/next?after={a}")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["administration_id"] == b
    assert a not in theirs.text


async def test_the_worklist_reads_chasing_and_rules(world: World) -> None:
    a = world.tenants.admin_a
    await _as_org(
        world.tenants.org_a,
        "INSERT INTO chase_send (organization_id, administration_id, kind, missing_count, "
        "  recipient_count, window_key, sent_at) "
        "VALUES (:org, :admin, 'scheduled', 1, 1, '2026-W41', now())",
        org=str(world.tenants.org_a),
        admin=str(a),
    )
    rule = await _as_org(
        world.tenants.org_a,
        "INSERT INTO booking_rule (organization_id, administration_id, counterparty_key, "
        "  account_code, created_by_user_id) VALUES (:org, :admin, 'kpn', '4500', :user) "
        "RETURNING id",
        org=str(world.tenants.org_a),
        admin=str(a),
        user=str(world.tenants.owner_a),
    )
    # One proposal the rule approved, one a person approved: only the first is a rule posting.
    for rule_id in (str(rule), None):
        proposal = await _as_org(
            world.tenants.org_a,
            "INSERT INTO booking_proposal (organization_id, administration_id, confidence, "
            "  group_key, amount) "
            "VALUES (:org, :admin, 'high', 'kpn|4500', CAST('12.50' AS numeric)) RETURNING id",
            org=str(world.tenants.org_a),
            admin=str(a),
        )
        # 0082: rule_id is set only by the update that approves the proposal.
        await _as_org(
            world.tenants.org_a,
            "UPDATE booking_proposal SET status = 'approved', decided_at = now(), "
            "  decided_by_user_id = :user, rule_id = CAST(:rule AS uuid) WHERE id = :id",
            id=str(proposal),
            user=str(world.tenants.owner_a),
            rule=rule_id,
        )
    summary = await _accountant(world, "GET", "/v1/firm/summary?since=2026-01-01T00:00:00Z")
    assert summary.status_code == 200, summary.text
    [line] = [x for x in summary.json()["activity"] if x["kind"] == "rule_postings"]
    assert (line["count"], line["client_count"]) == (1, 1)
    assert [c["administration_id"] for c in line["clients"]] == [str(a)]
    response = await _accountant(world, "GET", "/v1/firm/worklist?chip=all")
    assert response.status_code == 200, response.text
    [row] = response.json()["rows"]
    assert row["last_chased_at"] is not None
    assert row["rules_count"] == 1

    none_monthly = await _accountant(
        world, "GET", "/v1/firm/worklist?chip=all&vat_frequency=monthly"
    )
    assert none_monthly.json()["rows"] == []
    assert none_monthly.json()["chip_counts"]["all"] == 0


# --- saved views ------------------------------------------------------------------------


@pytest.mark.isolation("POST", "/v1/firm/views")
async def test_saving_a_view_counts_through_the_worklist(world: World) -> None:
    view = await _save(world, "  Alles  ", {"chip": "all", "sort": "name"})
    assert view["name"] == "Alles"
    assert view["query"]["chip"] == "all"
    worklist = (await _accountant(world, "GET", "/v1/firm/worklist?chip=all&sort=name")).json()
    assert view["count"] == worklist["total"] == 1  # A only: B is not this person's

    bad = await _accountant(world, "POST", "/v1/firm/views", {"name": "x", "query": {"chip": "?"}})
    assert bad.status_code == 422
    blank = await _accountant(world, "POST", "/v1/firm/views", {"name": "   ", "query": {}})
    assert blank.status_code == 422
    assert blank.json()["detail"]["reason"] == "firm_view_name_invalid"

    for n in range(19):
        await _save(world, f"View {n}")
    over = await _accountant(world, "POST", "/v1/firm/views", {"name": "21st", "query": {}})
    assert over.status_code == 409, over.text
    assert over.json()["detail"]["reason"] == "firm_view_limit_reached"

    # B's owner saving in B's tenant is a separate person in a separate organization.
    theirs = await _owner_b(world, "POST", "/v1/firm/views", {"name": "Mijn", "query": {}})
    assert theirs.status_code == 201, theirs.text


@pytest.mark.isolation("GET", "/v1/firm/views")
async def test_views_are_listed_per_person(world: World) -> None:
    mine = await _save(world, "Mijn werk", {"chip": "my_move", "assigned": "me"})
    colleague_saved = await _colleague(
        world, "POST", "/v1/firm/views", {"name": "Collega", "query": {"chip": "all"}}
    )
    assert colleague_saved.status_code == 201

    listed = await _accountant(world, "GET", "/v1/firm/views")
    assert listed.status_code == 200, listed.text
    assert [v["id"] for v in listed.json()["views"]] == [mine["id"]]
    assert listed.json()["views"][0]["count"] == 1
    assert colleague_saved.json()["id"] not in listed.text

    theirs = await _colleague(world, "GET", "/v1/firm/views")
    [only] = theirs.json()["views"]
    assert only["id"] == colleague_saved.json()["id"]
    assert only["count"] == 2  # the colleague's portfolio holds A and B

    other_tenant = await _owner_b(world, "GET", "/v1/firm/views")
    assert other_tenant.status_code == 200
    assert other_tenant.json()["views"] == []


@pytest.mark.isolation("POST", "/v1/firm/views/{view_id}/rename")
async def test_only_the_owner_renames_a_view(world: World) -> None:
    view = await _save(world, "Oud")
    path = f"/v1/firm/views/{view['id']}/rename"
    for refused in (
        await _colleague(world, "POST", path, {"name": "Gekaapt"}),
        await _owner_b(world, "POST", path, {"name": "Gekaapt"}),
    ):
        assert refused.status_code == 404, refused.text
    renamed = await _accountant(world, "POST", path, {"name": "Nieuw"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Nieuw"
    too_long = await _accountant(world, "POST", path, {"name": "x" * 61})
    assert too_long.status_code == 422


@pytest.mark.isolation("POST", "/v1/firm/views/{view_id}/archive")
async def test_only_the_owner_archives_a_view(world: World) -> None:
    view = await _save(world, "Weg ermee")
    path = f"/v1/firm/views/{view['id']}/archive"
    assert (await _colleague(world, "POST", path)).status_code == 404
    assert (await _owner_b(world, "POST", path)).status_code == 404
    assert [
        v["id"] for v in (await _accountant(world, "GET", "/v1/firm/views")).json()["views"]
    ] == [view["id"]]

    archived = await _accountant(world, "POST", path)
    assert archived.status_code == 204, archived.text
    assert (await _accountant(world, "GET", "/v1/firm/views")).json()["views"] == []
    again = await _accountant(world, "POST", path)
    assert again.status_code == 404
    rename_archived = await _accountant(
        world, "POST", f"/v1/firm/views/{view['id']}/rename", {"name": "Terug"}
    )
    assert rename_archived.status_code == 404

    # Never deleted: the row is still there, archived.
    still_there = await _as_org(
        world.firm,
        "SELECT count(*) FROM saved_view WHERE id = :id AND archived_at IS NOT NULL",
        id=view["id"],
    )
    assert still_there == 1

"""The firm home against a real Postgres (migrations 0078-0080 applied): ADR-109.

The world: a firm (Kantoor De Jong) engaged on organization A's administration, with one staff
member who holds Accountant on it (IAM-107) and Owner at the firm. Organization B is an
ordinary second tenant with look-alike data the firm must never see. Each route is checked
both ways - the firm sees A's figures, and nothing of B reaches the firm's responses (nor A's
the B owner's).

    admin A   one unmatched debit of 50.00 with no receipt   -> missing_receipts 1
              one unmatched credit of 100.00                  -> bank_to_match 1
              quarterly VAT, Q3 2026 open, nothing posted     -> deadline 2026-Q3
    admin B   the same lines; monthly VAT, September 2026     -> deadline 2026-09
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.repository import SqlSessionRepository
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


@dataclass(frozen=True)
class FirmWorld:
    tenants: SeededTenants
    firm: uuid.UUID
    staff: uuid.UUID
    staff_email: str


async def _engage(firm: uuid.UUID, administration_id: uuid.UUID, *, client: uuid.UUID) -> None:
    """The firm proposes, the client accepts - the same transition the application uses."""
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


async def _books(org: uuid.UUID, admin: uuid.UUID, *, scheme: str, tag: str) -> None:
    """A bank account with two unmatched lines, VAT registration and one VAT period."""
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
    for amount, description in (("-50.00", "Tankstation"), ("100.00", "Betaling factuur")):
        await _as_org(
            org,
            "INSERT INTO bank_transaction (organization_id, administration_id, bank_account_id, "
            "  booking_date, amount, description, external_id) "
            "VALUES (:org, :admin, :account, :on, CAST(:amount AS numeric), :description, :ext)",
            org=str(org),
            admin=str(admin),
            account=str(account),
            on=date(2026, 9, 10),
            amount=amount,
            description=f"{description} {tag}",
            ext=f"{tag}-{amount}",
        )
    await _as_org(
        org,
        "UPDATE administration SET vat_number = :vat WHERE id = :admin",
        vat=f"NL{tag}B01",
        admin=str(admin),
    )
    year = await _as_org(
        org,
        "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date, "
        "  period_scheme) VALUES (:org, :admin, '2026-01-01', '2026-12-31', :scheme) "
        "RETURNING id",
        org=str(org),
        admin=str(admin),
        scheme=scheme,
    )
    start, end, number = (
        (date(2026, 7, 1), date(2026, 9, 30), 3)
        if scheme == "quarterly"
        else (date(2026, 9, 1), date(2026, 9, 30), 9)
    )
    await _as_org(
        org,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, period_number, "
        "  start_date, end_date, status) VALUES (:org, :admin, :year, :n, :start, :end, 'open')",
        org=str(org),
        admin=str(admin),
        year=str(year),
        n=number,
        start=start,
        end=end,
    )


@pytest_asyncio.fixture
async def world(two_organizations: SeededTenants) -> AsyncIterator[FirmWorld]:
    tenants = two_organizations
    firm = await signup_firm_organization(app_engine, name="Kantoor De Jong", kvk="33333333")
    email = f"accountant+{uuid.uuid4().hex[:8]}@dejong.example"
    staff = await seed_user(app_engine, email=email)
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=staff,
        role_name="Owner",
        scope_type="organization",
        scope_id=firm,
    )
    await _engage(firm, tenants.admin_a, client=tenants.org_a)
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=staff,
        role_name="Accountant",
        scope_type="administration",
        scope_id=tenants.admin_a,
    )
    await _books(tenants.org_a, tenants.admin_a, scheme="quarterly", tag="AAA")
    await _books(tenants.org_b, tenants.admin_b, scheme="monthly", tag="BBB")
    yield FirmWorld(tenants=tenants, firm=firm, staff=staff, staff_email=email)


async def _call(
    org: uuid.UUID, user: uuid.UUID, method: str, path: str, *, body: object = None
) -> Response:
    headers = {"Authorization": f"Bearer {make_token(org, user_id=user)}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(method, path, headers=headers, json=body)


def _as_firm(world: FirmWorld, method: str, path: str, body: object = None) -> Any:
    return _call(world.firm, world.staff, method, path, body=body)


def _as_b(world: FirmWorld, method: str, path: str, body: object = None) -> Any:
    return _call(world.tenants.org_b, world.tenants.owner_b, method, path, body=body)


@pytest.mark.isolation("GET", "/v1/firm/worklist")
async def test_the_worklist_shows_the_firms_client_and_nothing_else(world: FirmWorld) -> None:
    a, b = world.tenants.admin_a, world.tenants.admin_b
    response = await _as_firm(world, "GET", "/v1/firm/worklist?chip=all")
    assert response.status_code == 200, response.text
    body = response.json()
    [row] = body["rows"]
    assert row["administration_id"] == str(a)
    assert row["counts"]["missing_receipts"] == 1
    assert row["counts"]["bank_to_match"] == 1
    assert row["booked_until"] == "2026-09-09"
    assert row["vat"]["period_label"] == "2026-Q3"
    assert row["last_chased_at"] is None
    assert body["chip_counts"]["all"] == 1
    assert str(b) not in response.text

    show_all = await _as_firm(world, "GET", "/v1/firm/worklist?chip=all&page_size=1000")
    assert show_all.status_code == 200, show_all.text
    too_many = await _as_firm(world, "GET", "/v1/firm/worklist?chip=all&page_size=1001")
    assert too_many.status_code == 422

    # B's owner sees B's own administration through the same route, and nothing of A.
    theirs = await _as_b(world, "GET", "/v1/firm/worklist?chip=all")
    assert theirs.status_code == 200, theirs.text
    assert [r["administration_id"] for r in theirs.json()["rows"]] == [str(b)]
    assert str(a) not in theirs.text


@pytest.mark.isolation("GET", "/v1/firm/summary")
async def test_the_summary_counts_only_the_portfolio(world: FirmWorld) -> None:
    response = await _as_firm(world, "GET", "/v1/firm/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["client_count"] == 1
    assert body["counts"]["missing_receipts"] == 1
    assert body["counts"]["bank_to_match"] == 1
    assert str(world.tenants.admin_b) not in response.text

    theirs = await _as_b(world, "GET", "/v1/firm/summary")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["client_count"] == 1
    assert str(world.tenants.admin_a) not in theirs.text


@pytest.mark.isolation("POST", "/v1/firm/summary/seen")
async def test_marking_the_summary_seen_moves_only_the_callers_window(world: FirmWorld) -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)
    response = await _as_firm(world, "POST", "/v1/firm/summary/seen")
    assert response.status_code == 204, response.text

    summary = (await _as_firm(world, "GET", "/v1/firm/summary")).json()
    assert datetime.fromisoformat(summary["since"]) >= before

    async with app_engine.begin() as conn:
        seen = (
            await conn.execute(
                text("SELECT id, firm_activity_seen_at FROM users WHERE id = ANY(:ids)"),
                {"ids": [world.staff, world.tenants.owner_b]},
            )
        ).all()
    by_user = {row.id: row.firm_activity_seen_at for row in seen}
    assert by_user[world.staff] is not None
    assert by_user[world.tenants.owner_b] is None


@pytest.mark.isolation("POST", "/v1/firm/clients/{administration_id}/snooze")
async def test_snoozing_a_client_and_not_someone_elses(world: FirmWorld) -> None:
    a, b = world.tenants.admin_a, world.tenants.admin_b
    until = (date.today() + timedelta(days=7)).isoformat()

    refused = await _as_firm(
        world, "POST", f"/v1/firm/clients/{b}/snooze", {"until": until, "reason": "x"}
    )
    assert refused.status_code in {403, 404}, refused.text
    refused_b = await _as_b(
        world, "POST", f"/v1/firm/clients/{a}/snooze", {"until": until, "reason": "x"}
    )
    assert refused_b.status_code in {403, 404}, refused_b.text

    past = await _as_firm(world, "POST", f"/v1/firm/clients/{a}/snooze", {"until": "2020-01-01"})
    assert past.status_code == 422

    ok = await _as_firm(
        world, "POST", f"/v1/firm/clients/{a}/snooze", {"until": until, "reason": "Op vakantie"}
    )
    assert ok.status_code == 204, ok.text
    snoozed = (await _as_firm(world, "GET", "/v1/firm/worklist?chip=snoozed")).json()
    assert [r["snooze_reason"] for r in snoozed["rows"]] == ["Op vakantie"]
    mine = (await _as_firm(world, "GET", "/v1/firm/worklist?chip=my_move")).json()
    assert mine["rows"] == []

    cleared = await _as_firm(world, "POST", f"/v1/firm/clients/{a}/snooze", {"until": None})
    assert cleared.status_code == 204
    assert (await _as_firm(world, "GET", "/v1/firm/worklist?chip=snoozed")).json()["rows"] == []


@pytest.mark.isolation("POST", "/v1/firm/clients/assign")
async def test_assigning_reports_what_it_could_not_assign(world: FirmWorld) -> None:
    a, b = world.tenants.admin_a, world.tenants.admin_b
    # "Mine" also holds clients assigned to nobody (ADR-109): before any assignment, A is there.
    unassigned = (await _as_firm(world, "GET", "/v1/firm/worklist?chip=all&assigned=me")).json()
    assert [r["administration_id"] for r in unassigned["rows"]] == [str(a)]
    assert unassigned["rows"][0]["assigned_user_id"] is None

    response = await _as_firm(
        world,
        "POST",
        "/v1/firm/clients/assign",
        {"administration_ids": [str(a), str(b)], "user_id": str(world.staff)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assigned"] == 1
    assert body["failed"] == [{"administration_id": str(b), "reason": "administration_not_found"}]

    mine = (await _as_firm(world, "GET", "/v1/firm/worklist?chip=all&assigned=me")).json()
    assert [r["assigned_user_id"] for r in mine["rows"]] == [str(world.staff)]

    stranger = await _as_firm(
        world,
        "POST",
        "/v1/firm/clients/assign",
        {"administration_ids": [str(a)], "user_id": str(world.tenants.owner_b)},
    )
    assert stranger.status_code == 422

    # B's owner cannot assign A's administration: it is not visible to B at all.
    theirs = await _as_b(
        world, "POST", "/v1/firm/clients/assign", {"administration_ids": [str(a)], "user_id": None}
    )
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["assigned"] == 0


@pytest.mark.isolation("GET", "/v1/firm/staff")
async def test_staff_lists_the_firms_people_only(world: FirmWorld) -> None:
    response = await _as_firm(world, "GET", "/v1/firm/staff")
    assert response.status_code == 200, response.text
    staff = response.json()
    assert {s["user_id"] for s in staff} == {str(world.staff)}
    assert staff[0]["email"] == world.staff_email
    assert str(world.tenants.owner_b) not in response.text
    assert str(world.tenants.owner_a) not in response.text


@pytest.mark.isolation("GET", "/v1/firm/deadlines")
async def test_deadlines_bucket_the_portfolios_returns(world: FirmWorld) -> None:
    window = "?from=2026-10-01&to=2026-11-30"
    response = await _as_firm(world, "GET", f"/v1/firm/deadlines{window}")
    assert response.status_code == 200, response.text
    [deadline] = response.json()
    assert (deadline["kind"], deadline["period_label"], deadline["due_date"]) == (
        "vat",
        "2026-Q3",
        "2026-10-31",
    )
    assert deadline["client_count"] == 1
    assert sum(deadline["buckets"].values()) == 1

    theirs = await _as_b(world, "GET", f"/v1/firm/deadlines{window}")
    assert [d["period_label"] for d in theirs.json()] == ["2026-09"]

    backwards = await _as_firm(world, "GET", "/v1/firm/deadlines?from=2026-11-30&to=2026-10-01")
    assert backwards.status_code == 422


async def test_a_new_session_shifts_the_login_times(two_organizations: SeededTenants) -> None:
    user = two_organizations.owner_a
    first = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    second = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    for at in (first, second):
        # A bootstrap-shaped session: no tenant context, as sign-in has none yet.
        async with factory() as session, session.begin():
            await SqlSessionRepository(session).create(
                user_id=user,
                token_hash=f"test-{uuid.uuid4().hex}",
                privileged=True,
                created_at=at,
                expires_at=at + timedelta(hours=12),
                mfa_verified_at=None,
                ip_address=None,
                user_agent=None,
            )
    async with app_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT last_login_at, previous_login_at FROM users WHERE id = :id"),
                {"id": user},
            )
        ).one()
    assert (row.previous_login_at, row.last_login_at) == (first, second)

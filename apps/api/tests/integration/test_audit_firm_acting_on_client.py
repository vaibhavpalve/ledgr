"""A firm acting in a client's books files its audit entries in the CLIENT's chain (ADR-112).

Migration 0081 widened `audit_log_insert` by one branch and made `audit_log_seal()` read the chain
head as `ledgr_audit`. This file pins down both halves of that, against a real Postgres:

  * what a firm session CAN now do - append to an actively-engaged client's chain, under that
    client's organization, with the entry sealed onto the client's true head (no fork, even with
    client-side and firm-side entries interleaved), and post an expense from the firm's session;
  * what it still CANNOT - append for an administration it is not (actively) engaged on, append
    under any organization other than the administration's owner, append with no administration,
    or read a single row of the client's log (IAM-094 / IAM-109: the SELECT policy is unchanged).

    firm F     active engagement on admin A (owned by org A); nothing on admin B
    firm P     a PENDING engagement on admin A
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.db import engine as app_engine
from tests.integration.test_bank_routes import (  # noqa: F401 - the autouse fixture is reused
    _bank_world,
    _email_verified,
    _exec,
    _scalar,
    _seed_expense,
    _world,
)
from tests.integration.test_firm_portfolio_isolation import _as_org, _engage
from tests.integration.test_firm_portfolio_isolation import _call as _call_as
from tests.support.seed import SeededTenants, grant_role, seed_user, signup_firm_organization

_INSERT = (
    "INSERT INTO audit_log (organization_id, administration_id, actor_type, category, action, "
    "  resource_type, outcome) "
    "VALUES (:org, :admin, 'system', 'posting', :action, 'journal_entry', 'success')"
)


async def _insert_as(
    session_org: uuid.UUID,
    *,
    org: uuid.UUID,
    admin: uuid.UUID | None,
    action: str = "post_journal_entry",
) -> None:
    await _as_org(
        session_org,
        _INSERT,
        org=str(org),
        admin=str(admin) if admin else None,
        action=action,
    )


async def _chain(org: uuid.UUID) -> list[Any]:
    """The client's chain as the CLIENT reads it."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        result = await conn.execute(
            text(
                "SELECT sequence_number, previous_hash, entry_hash, action "
                "FROM audit_log WHERE organization_id = :org ORDER BY sequence_number"
            ),
            {"org": str(org)},
        )
        return list(result)


async def _breaks(org: uuid.UUID) -> int:
    return int(
        await _as_org(org, "SELECT count(*) FROM app.verify_audit_chain(:org)", org=str(org))
    )


def _refused_by_rls(raised: pytest.ExceptionInfo[BaseException]) -> None:
    assert "row-level security" in str(raised.value), raised.value


@pytest_asyncio.fixture
async def firm(two_organizations: SeededTenants) -> uuid.UUID:
    firm_id = await signup_firm_organization(app_engine, name="Kantoor Bakker", kvk="66666666")
    await _engage(firm_id, two_organizations.admin_a, client=two_organizations.org_a)
    return firm_id


# ---------------------------------------------------------------------------
# What a firm session can now do
# ---------------------------------------------------------------------------


async def test_a_firm_entry_lands_on_the_clients_true_chain_head(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    """The QA finding: under the caller's RLS the seal saw no predecessor and would have sealed the
    firm's entry as sequence 1 with the zero hash. Interleaved on purpose - client, firm, firm,
    client, firm - so each side must find the head the OTHER side just wrote."""
    t = two_organizations
    before = await _chain(t.org_a)

    for side in ("client", "firm", "firm", "client", "firm"):
        await _insert_as(
            t.org_a if side == "client" else firm,
            org=t.org_a,
            admin=t.admin_a,
            action=f"{side}_post",
        )

    chain = await _chain(t.org_a)
    added = chain[len(before) :]
    assert [r.action for r in added] == [
        "client_post",
        "firm_post",
        "firm_post",
        "client_post",
        "firm_post",
    ]
    # Contiguous, and each links to the entry before it - whoever wrote that one.
    for prev, cur in zip(chain, chain[1:], strict=False):
        assert cur.sequence_number == prev.sequence_number + 1
        assert cur.previous_hash == prev.entry_hash
    assert chain[0].sequence_number == 1
    assert await _breaks(t.org_a) == 0
    # The firm's own chain is untouched by entries filed under its client.
    firm_chain = await _chain(firm)
    assert all(r.action not in {"client_post", "firm_post"} for r in firm_chain)
    assert await _breaks(firm) == 0


async def test_the_repository_appends_for_the_client_and_reads_back_nothing(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    """`SqlAuditRepository.append` no longer uses RETURNING (which would evaluate the SELECT
    policy). From the firm's session it appends and returns None; from the client's own session
    it returns the sealed entry exactly as before."""
    t = two_organizations
    event = AuditEvent(
        organization_id=t.org_a,
        administration_id=t.admin_a,
        actor_type=ActorType.SYSTEM,
        category=AuditCategory.POSTING,
        action="repository_firm_append",
        resource_type="journal_entry",
        outcome=AuditOutcome.SUCCESS,
    )
    async with app_engine.connect() as conn:
        async with AsyncSession(bind=conn) as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(firm)}
            )
            assert await AuditLog(SqlAuditRepository(session)).record(event) is None

        async with AsyncSession(bind=conn) as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(t.org_a)},
            )
            own = await AuditLog(SqlAuditRepository(session)).record(event)
    assert own is not None
    chain = await _chain(t.org_a)
    assert [r.action for r in chain[-2:]] == ["repository_firm_append"] * 2
    assert own.sequence_number == chain[-1].sequence_number
    assert own.previous_hash == chain[-2].entry_hash
    assert await _breaks(t.org_a) == 0


async def test_firm_staff_post_an_expense_in_a_granted_clients_books(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    """Expense form + expense posting from the FIRM's session, for a Model A-style engaged client.
    Before 0081 the posting's audit entry (filed under the client) failed the insert policy."""
    t = two_organizations
    accountant = await seed_user(
        app_engine, email=f"accountant+{uuid.uuid4().hex[:8]}@bakker.example"
    )
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=accountant,
        role_name="Accountant",
        scope_type="administration",
        scope_id=t.admin_a,
    )

    # The client's books, set up as the client (same shape as test_bank_routes._bank_paid_receipt).
    world = await _bank_world(t)
    await _world(t)

    async def account(code: str, name: str, kind: str) -> uuid.UUID:
        return await _scalar(  # type: ignore[no-any-return]
            t,
            "SELECT (ledger.create_account(:admin, :code, :name, :kind, NULL, NULL, NULL)).id",
            admin=str(t.admin_a),
            code=code,
            name=name,
            kind=kind,
        )

    costs = await account("4100", "Kantoorkosten", "expense")
    vat_in = await account("1720", "Te vorderen omzetbelasting", "asset")
    await _scalar(
        t,
        "SELECT (ledger.create_journal(:admin, 'IK', 'Inkoopboek', 'purchase')).id",
        admin=str(t.admin_a),
    )
    for purpose, target in (
        ("expense_category", costs),
        ("vat_input", vat_in),
        ("business_account", world["bank_ledger_account"]),
    ):
        await _exec(
            t,
            "INSERT INTO expense_posting_account (organization_id, administration_id, purpose, "
            "  category_key, account_id) VALUES (:org, :admin, :purpose, NULL, :acct)",
            org=str(t.org_a),
            admin=str(t.admin_a),
            purpose=purpose,
            acct=str(target),
        )
    expense_id = await _seed_expense(t.admin_a, t.org_a, t.owner_a, supplier="Staples")
    chain_before = len(await _chain(t.org_a))

    base = f"/v1/administrations/{t.admin_a}/expenses/{expense_id}"
    filled = await _call_as(
        firm,
        accountant,
        "PATCH",
        base,
        {
            "expense_date": "2026-09-16",
            "supplier": "Staples",
            "gross_amount": "121.00",
            "vat_treatment": "btw_21",
            "category": "Office supplies",
            "payment_method": "business_account",
        },
    )
    assert filled.status_code == 200, filled.text
    posted = await _call_as(firm, accountant, "POST", f"{base}/posting")
    assert posted.status_code == 200, posted.text

    entry = await _as_org(
        t.org_a, "SELECT journal_entry_id FROM expense WHERE id = :id", id=str(expense_id)
    )
    assert entry is not None, "the expense was booked in the client's ledger"

    chain = await _chain(t.org_a)
    assert len(chain) > chain_before, "the posting was audited in the client's chain"
    assert await _breaks(t.org_a) == 0
    # ...and the firm still cannot read any of it.
    visible = await _as_org(
        firm, "SELECT count(*) FROM audit_log WHERE organization_id = :org", org=str(t.org_a)
    )
    assert visible == 0


# ---------------------------------------------------------------------------
# What a firm session still cannot do
# ---------------------------------------------------------------------------


async def test_a_firm_cannot_append_for_an_administration_it_is_not_engaged_on(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    t = two_organizations
    before = await _chain(t.org_b)
    with pytest.raises(DBAPIError) as raised:
        await _insert_as(firm, org=t.org_b, admin=t.admin_b)
    _refused_by_rls(raised)
    assert await _chain(t.org_b) == before


async def test_a_pending_engagement_admits_nothing(two_organizations: SeededTenants) -> None:
    t = two_organizations
    pending = await signup_firm_organization(app_engine, name="Kantoor Jansen", kvk="77777777")
    await _as_org(
        pending,
        "INSERT INTO firm_engagement (firm_organization_id, administration_id, status, "
        "  initiated_by) VALUES (:firm, :admin, 'pending', 'firm')",
        firm=str(pending),
        admin=str(t.admin_a),
    )
    with pytest.raises(DBAPIError) as raised:
        await _insert_as(pending, org=t.org_a, admin=t.admin_a)
    _refused_by_rls(raised)


async def test_a_firm_cannot_file_under_an_organization_that_does_not_own_the_administration(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    """Engaged on A, so `has_administration_access(admin_a)` is true - but the row must then name
    A's OWNER. Naming org B (another tenant's chain) with admin A is refused."""
    t = two_organizations
    before = await _chain(t.org_b)
    with pytest.raises(DBAPIError) as raised:
        await _insert_as(firm, org=t.org_b, admin=t.admin_a)
    _refused_by_rls(raised)
    assert await _chain(t.org_b) == before


async def test_a_firm_cannot_file_under_a_client_without_naming_an_administration(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    t = two_organizations
    with pytest.raises(DBAPIError) as raised:
        await _insert_as(firm, org=t.org_a, admin=None)
    _refused_by_rls(raised)


async def test_a_firm_still_cannot_read_the_clients_audit_log(
    two_organizations: SeededTenants, firm: uuid.UUID
) -> None:
    """audit_log_select is unchanged: appending to a chain is not reading it. INSERT ... RETURNING
    is the read in disguise, and is refused for the same reason."""
    t = two_organizations
    await _insert_as(t.org_a, org=t.org_a, admin=t.admin_a)
    await _insert_as(firm, org=t.org_a, admin=t.admin_a)
    assert len(await _chain(t.org_a)) >= 2

    for sql in (
        "SELECT count(*) FROM audit_log WHERE organization_id = :org",
        "SELECT count(*) FROM audit_log WHERE administration_id = :admin",
        "SELECT entries FROM app.audit_chain_head(:org)",
    ):
        seen = await _as_org(firm, sql, org=str(t.org_a), admin=str(t.admin_a))
        assert seen == 0, sql

    with pytest.raises(DBAPIError) as raised:
        await _as_org(
            firm,
            _INSERT + " RETURNING entry_hash",
            org=str(t.org_a),
            admin=str(t.admin_a),
            action="returning",
        )
    _refused_by_rls(raised)


async def test_the_seal_definer_is_the_audit_owner_and_nothing_more(
    two_organizations: SeededTenants,
) -> None:
    """The privilege 0081 relies on, stated as facts about the catalogue: the seal runs as
    ledgr_audit with a pinned search_path; ledgr_audit still cannot log in, bypass RLS or be
    assumed by anyone; and the read-everything policy applies to ledgr_audit alone."""
    async with app_engine.begin() as conn:
        fn = (
            await conn.execute(
                text(
                    "SELECT p.prosecdef, pg_get_userbyid(p.proowner) AS owner, p.proconfig "
                    "FROM pg_proc p WHERE p.proname = 'audit_log_seal'"
                )
            )
        ).one()
        role = (
            await conn.execute(
                text(
                    "SELECT rolcanlogin, rolbypassrls, rolsuper, "
                    "  (SELECT count(*) FROM pg_auth_members m WHERE m.roleid = r.oid) AS members "
                    "FROM pg_roles r WHERE rolname = 'ledgr_audit'"
                )
            )
        ).one()
        policy = (
            await conn.execute(
                text(
                    "SELECT cmd, roles::text[] AS roles FROM pg_policies "
                    "WHERE tablename = 'audit_log' AND policyname = 'audit_log_seal_read'"
                )
            )
        ).one()
    assert fn.prosecdef is True
    assert fn.owner == "ledgr_audit"
    assert any(c.startswith("search_path=") for c in fn.proconfig)
    assert (role.rolcanlogin, role.rolbypassrls, role.rolsuper, role.members) == (
        False,
        False,
        False,
        0,
    )
    assert policy.cmd == "SELECT"
    assert policy.roles == ["ledgr_audit"]

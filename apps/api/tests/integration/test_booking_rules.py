"""Approval rules against a real Postgres (migration 0082, ADR-113, contract-wave2 decisions 1-4).

Real HTTP, real routes, real RLS. What this proves that tests/firm/test_rules.py cannot: that
"remember" really writes a per-administration rule, that the next certain match really is booked by
it through the Bank screen's own path (once), that `max_amount` and retiring really keep a line
pending, that 0082's trigger and grants hold, that a rule of client A never touches client B even
for the same counterparty and the same firm (FR-BNK-006), that revoking the creator's grant
suspends the rule, and that another tenant can neither see nor change any of it (IAM-005).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest). Needs 0065-0082.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.exc import DBAPIError

from api.db import engine as app_engine
from api.main import app
from tests.integration.test_bank_routes import (  # noqa: F401 - the autouse fixture is reused
    _CSV_HEADER,
    _balance,
    _bank_paid_receipt,
    _call,
    _email_verified,
)
from tests.integration.test_expense_form_isolation import _seed_expense
from tests.integration.test_firm_portfolio_isolation import _as_org, _engage, _mirrored
from tests.integration.test_firm_portfolio_isolation import _call as _call_as
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.isolation import assert_tenant_isolated
from tests.support.seed import SeededTenants, grant_role, seed_user, signup_firm_organization

DECIDE = "/v1/firm/proposals/decide"


def _rules_path(admin: uuid.UUID, suffix: str = "") -> str:
    return f"/v1/administrations/{admin}/rules{suffix}"


async def _pending_proposal(tenants: SeededTenants, line: str) -> str | None:
    found = await _scalar(
        tenants,
        "SELECT id FROM booking_proposal WHERE bank_transaction_id = :line AND status = 'pending'",
        line=line,
    )
    return None if found is None else str(found)


async def _proposal(tenants: SeededTenants, line: str) -> Any:
    return await _as_org(
        tenants.org_a,
        "SELECT row(status, rule_id, decided_by_user_id)::text FROM booking_proposal "
        "WHERE bank_transaction_id = :line ORDER BY created_at DESC LIMIT 1",
        line=line,
    )


async def _another_staples(tenants: SeededTenants, gross: str, booked: str) -> str:
    """One more bank-paid Staples receipt in admin A, posted, and the statement line that paid it
    imported into A's existing bank account - as A's own owner. Returns the line's id."""
    admin, org = tenants.admin_a, tenants.org_a
    expense_id = str(await _seed_expense(admin, org, tenants.owner_a, supplier="Staples"))
    filled = await _call(
        tenants,
        admin,
        "PATCH",
        f"/expenses/{expense_id}",
        {
            "expense_date": "2026-09-16",
            "supplier": "Staples",
            "gross_amount": gross,
            "vat_treatment": "btw_21",
            "category": "Office supplies",
            "payment_method": "business_account",
        },
    )
    assert filled.status_code == 200, filled.text
    posted = await _call(tenants, admin, "POST", f"/expenses/{expense_id}/posting")
    assert posted.status_code == 200, posted.text
    account_id = await _scalar(
        tenants, "SELECT id FROM bank_account WHERE administration_id = :a", a=str(admin)
    )
    statement = (
        f"{_CSV_HEADER}\n{booked},-{gross},Staples Nederland,NL44INGB0001234567,Kantoorartikelen\n"
    )
    imported = await _call(
        tenants, admin, "POST", f"/bank-accounts/{account_id}/import", {"csv": statement}
    )
    assert imported.status_code == 200, imported.text
    line = await _scalar(
        tenants,
        "SELECT id FROM bank_transaction WHERE administration_id = :a AND amount = :amt",
        a=str(admin),
        amt=-Decimal(gross),
    )
    return str(line)


async def _decide(
    org: uuid.UUID, user: uuid.UUID, proposal_id: str, *, remember: bool = True
) -> Response:
    return await _call_as(
        org,
        user,
        "POST",
        DECIDE,
        {"decisions": [{"proposal_id": proposal_id, "decision": "approve", "remember": remember}]},
    )


async def _ruled(tenants: SeededTenants) -> dict[str, Any]:
    """A's first Staples receipt approved by A's owner with "Always do this"."""
    world = await _bank_paid_receipt(tenants, settles_through="2000")
    proposal_id = await _pending_proposal(tenants, world["transaction_id"])
    assert proposal_id is not None
    decided = await _decide(tenants.org_a, tenants.owner_a, proposal_id)
    assert decided.status_code == 200, decided.text
    assert decided.json() == {"approved": 1, "rejected": 0, "failed": [], "rules_created": 1}
    rule_id = await _scalar(
        tenants, "SELECT id FROM booking_rule WHERE administration_id = :a", a=str(tenants.admin_a)
    )
    return {**world, "proposal_id": proposal_id, "rule_id": str(rule_id)}


async def _foreign(tenants: SeededTenants, method: str, path: str, json: object = None) -> Response:
    """B's owner, naming A's administration in the path."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await assert_tenant_isolated(
            client,
            method,
            path,
            as_org=tenants.org_b,
            as_user=tenants.owner_b,
            foreign_record_ids=[],
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json=json,
        )
    assert response.status_code in {403, 404}, response.text
    return response


# ---------------------------------------------------------------------------
# Remember, and the rule at work
# ---------------------------------------------------------------------------


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/rules")
async def test_remember_writes_one_rule_for_the_client_and_another_tenant_cannot_read_it(
    two_organizations: SeededTenants,
) -> None:
    t = two_organizations
    world = await _ruled(t)

    listing = await _call(t, t.admin_a, "GET", "/rules")
    assert listing.status_code == 200, listing.text
    [rule] = listing.json()["rules"]
    assert rule["id"] == world["rule_id"]
    assert (rule["counterparty_key"], rule["account_code"]) == ("staples", "4100")
    assert rule["counterparty_label"] == "Staples"
    assert rule["account_name"] == "Kantoorkosten"
    assert (rule["status"], rule["max_amount"], rule["suspended_reason"]) == ("active", None, None)
    assert rule["postings_count"] == 0 and rule["last_posted_at"] is None
    assert rule["created_by_name"]

    # Remembering again keeps the one rule.
    assert (
        await _scalar(
            t, "SELECT count(*) FROM booking_rule WHERE administration_id = :a", a=str(t.admin_a)
        )
        == 1
    )

    foreign = await _foreign(t, "GET", _rules_path(t.admin_a))
    assert world["rule_id"] not in foreign.text
    own_b = await _call(_mirrored(t), t.admin_b, "GET", "/rules")
    assert own_b.status_code == 200, own_b.text
    assert own_b.json() == {"rules": []}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/rule-postings")
async def test_the_next_certain_match_is_booked_by_the_rule_once_and_listed(
    two_organizations: SeededTenants,
) -> None:
    t = two_organizations
    world = await _ruled(t)
    assert await _balance(t, world["bank"]) == "-121.00"

    line = await _another_staples(t, "60.50", "2026-09-19")

    # Booked by the import itself, through the Bank screen's path: the bank moved once more.
    assert await _balance(t, world["bank"]) == "-181.50"
    assert await _balance(t, world["transit"]) == "0.00"
    status = await _proposal(t, line)
    assert status == f"(approved,{world['rule_id']},)", status  # decided by no person
    line_status = await _scalar(t, "SELECT status FROM bank_transaction WHERE id = :id", id=line)
    assert line_status == "reconciled"
    audited = await _scalar(
        t,
        "SELECT count(*) FROM audit_log WHERE action = 'approve_booking_proposal' "
        "AND actor_type = 'system' AND detail->>'rule_id' = :rule "
        "AND detail->>'rule_created_by' = :creator",
        rule=world["rule_id"],
        creator=str(t.owner_a),
    )
    assert audited == 1

    postings = await _call(t, t.admin_a, "GET", "/rule-postings?limit=10")
    assert postings.status_code == 200, postings.text
    [item] = postings.json()["items"]
    assert item["rule_id"] == world["rule_id"]
    assert (item["amount"], item["date"], item["account_code"]) == ("60.50", "2026-09-19", "4100")
    assert item["undoable"] is False
    [rule] = (await _call(t, t.admin_a, "GET", "/rules")).json()["rules"]
    assert rule["postings_count"] == 1 and rule["last_posted_at"] is not None

    # Re-importing the same statement adds nothing and books nothing (NFR-032).
    account_id = await _scalar(
        t, "SELECT id FROM bank_account WHERE administration_id = :a", a=str(t.admin_a)
    )
    statement = (
        f"{_CSV_HEADER}\n2026-09-19,-60.50,Staples Nederland,NL44INGB0001234567,Kantoorartikelen\n"
    )
    again = await _call(
        t, t.admin_a, "POST", f"/bank-accounts/{account_id}/import", {"csv": statement}
    )
    assert again.status_code == 200, again.text
    assert await _balance(t, world["bank"]) == "-181.50"

    bad = await _call(t, t.admin_a, "GET", "/rule-postings?limit=0")
    assert bad.status_code == 422
    foreign = await _foreign(t, "GET", f"/v1/administrations/{t.admin_a}/rule-postings")
    assert item["proposal_id"] not in foreign.text


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/rules/{rule_id}/max-amount")
async def test_above_max_amount_stays_a_pending_proposal(two_organizations: SeededTenants) -> None:
    t = two_organizations
    world = await _ruled(t)
    path = f"/rules/{world['rule_id']}/max-amount"

    for bad in ("1.001", "0", "-5", "lots"):
        refused = await _call(t, t.admin_a, "POST", path, {"max_amount": bad})
        assert refused.status_code == 422, (bad, refused.text)
        assert refused.json()["detail"]["reason"] == "booking_rules_invalid"

    foreign = await _foreign(
        t, "POST", _rules_path(t.admin_a, path[len("/rules") :]), {"max_amount": "1.00"}
    )
    assert world["rule_id"] not in foreign.text

    capped = await _call(t, t.admin_a, "POST", path, {"max_amount": "50.00"})
    assert capped.status_code == 200, capped.text
    assert capped.json()["max_amount"] == "50.00"

    line = await _another_staples(t, "60.50", "2026-09-19")
    assert await _pending_proposal(t, line) is not None
    assert await _balance(t, world["bank"]) == "-121.00"

    lifted = await _call(t, t.admin_a, "POST", path, {"max_amount": None})
    assert lifted.json()["max_amount"] is None


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/rules/{rule_id}/retire")
async def test_a_retired_rule_books_nothing_and_is_never_deleted(
    two_organizations: SeededTenants,
) -> None:
    t = two_organizations
    world = await _ruled(t)
    rule_id = world["rule_id"]

    foreign = await _foreign(t, "POST", _rules_path(t.admin_a, f"/{rule_id}/retire"))
    assert rule_id not in foreign.text
    assert (
        await _scalar(t, "SELECT status FROM booking_rule WHERE id = :id", id=rule_id) == "active"
    )

    retired = await _call(t, t.admin_a, "POST", f"/rules/{rule_id}/retire")
    assert retired.status_code == 200, retired.text
    assert retired.json()["status"] == "retired"
    again = await _call(t, t.admin_a, "POST", f"/rules/{rule_id}/retire")
    assert again.status_code == 200 and again.json()["status"] == "retired"
    capped = await _call(t, t.admin_a, "POST", f"/rules/{rule_id}/max-amount", {"max_amount": "5"})
    assert capped.status_code == 409, capped.text
    listing = await _call(t, t.admin_a, "GET", "/rules")
    assert listing.json() == {"rules": []}

    line = await _another_staples(t, "60.50", "2026-09-19")
    assert await _pending_proposal(t, line) is not None

    # 0082: retired is final, and nothing deletes a rule.
    with pytest.raises(DBAPIError, match="retired"):
        await _exec(
            t,
            "UPDATE booking_rule SET status = 'active', retired_at = NULL, "
            "retired_by_user_id = NULL WHERE id = :id",
            id=rule_id,
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        await _exec(t, "DELETE FROM booking_rule WHERE id = :id", id=rule_id)
    missing = await _call(t, t.admin_a, "POST", f"/rules/{uuid.uuid4()}/retire")
    assert missing.status_code == 404


async def test_0082_freezes_a_rule_and_only_an_approval_names_one(
    two_organizations: SeededTenants,
) -> None:
    t = two_organizations
    world = await _ruled(t)
    with pytest.raises(DBAPIError, match="only its status and cap"):
        await _exec(
            t, "UPDATE booking_rule SET account_code = '9999' WHERE id = :id", id=world["rule_id"]
        )
    line = await _another_staples(t, "60.50", "2026-09-19")
    # A person approved the first proposal; it names no rule and cannot be made to.
    with pytest.raises(DBAPIError, match="cannot change"):
        await _exec(
            t,
            "UPDATE booking_proposal SET rule_id = :rule WHERE id = :id",
            rule=world["rule_id"],
            id=world["proposal_id"],
        )
    assert line


async def test_remember_on_a_rejection_is_refused(two_organizations: SeededTenants) -> None:
    t = two_organizations
    refused = await _call_as(
        t.org_a,
        t.owner_a,
        "POST",
        DECIDE,
        {"decisions": [{"proposal_id": str(uuid.uuid4()), "decision": "reject", "remember": True}]},
    )
    assert refused.status_code == 422, refused.text


# ---------------------------------------------------------------------------
# One firm, two clients, the same counterparty
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirmWorld:
    tenants: SeededTenants
    firm: uuid.UUID
    accountant: uuid.UUID
    world_a: dict[str, str]


@pytest_asyncio.fixture
async def firm_world(two_organizations: SeededTenants) -> FirmWorld:
    t = two_organizations
    world_a = await _bank_paid_receipt(t, settles_through="2000")
    firm = await signup_firm_organization(app_engine, name="Kantoor Regel", kvk="88888888")
    await _engage(firm, t.admin_a, client=t.org_a)
    await _engage(firm, t.admin_b, client=t.org_b)
    accountant = await seed_user(
        app_engine, email=f"accountant+{uuid.uuid4().hex[:8]}@regel.example"
    )
    for admin in (t.admin_a, t.admin_b):
        await grant_role(
            app_engine,
            acting_org_id=firm,
            user_id=accountant,
            role_name="Accountant",
            scope_type="administration",
            scope_id=admin,
        )
    return FirmWorld(tenants=t, firm=firm, accountant=accountant, world_a=world_a)


async def _firm_remembers_a(fw: FirmWorld) -> str:
    t = fw.tenants
    proposal_id = await _pending_proposal(t, fw.world_a["transaction_id"])
    assert proposal_id is not None
    decided = await _decide(fw.firm, fw.accountant, proposal_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["rules_created"] == 1, decided.json()
    rule_id = await _as_org(
        t.org_a, "SELECT id FROM booking_rule WHERE administration_id = :a", a=str(t.admin_a)
    )
    return str(rule_id)


async def test_a_rule_for_client_a_never_affects_client_b(firm_world: FirmWorld) -> None:
    """FR-BNK-006: the same firm, the same person, the same counterparty and account - B's
    certain match still waits for a person, and B has no rule."""
    fw = firm_world
    t = fw.tenants
    rule_a = await _firm_remembers_a(fw)

    world_b = await _bank_paid_receipt(_mirrored(t), settles_through="2000")
    assert world_b["suggestion"]["confidence"] == "high"
    pending_b = await _as_org(
        t.org_b,
        "SELECT row(status, rule_id)::text FROM booking_proposal WHERE bank_transaction_id = :l",
        l=world_b["transaction_id"],
    )
    assert pending_b == "(pending,)"
    assert await _balance(_mirrored(t), world_b["bank"]) == "0.00"
    assert await _as_org(t.org_b, "SELECT count(*) FROM booking_rule") == 0

    # The firm sees A's rule under A, and nothing under B.
    rules_a = await _call_as(fw.firm, fw.accountant, "GET", _rules_path(t.admin_a))
    assert [r["id"] for r in rules_a.json()["rules"]] == [rule_a]
    rules_b = await _call_as(fw.firm, fw.accountant, "GET", _rules_path(t.admin_b))
    assert rules_b.json() == {"rules": []}
    # Naming A's rule under B's path finds nothing.
    crossed = await _call_as(
        fw.firm, fw.accountant, "POST", _rules_path(t.admin_b, f"/{rule_a}/retire")
    )
    assert crossed.status_code == 404, crossed.text
    assert (
        await _as_org(t.org_a, "SELECT status FROM booking_rule WHERE id = :id", id=rule_a)
        == "active"
    )


async def test_revoking_the_creators_grant_suspends_the_rule(firm_world: FirmWorld) -> None:
    fw = firm_world
    t = fw.tenants
    rule_id = await _firm_remembers_a(fw)

    # While the accountant holds the grant, the client's own import is booked by the rule.
    booked = await _another_staples(t, "60.50", "2026-09-19")
    assert (await _proposal(t, booked)).startswith(f"(approved,{rule_id}")

    await _as_org(
        fw.firm,
        "UPDATE role_assignment SET revoked_at = now() "
        "WHERE user_id = :u AND scope_type = 'administration' AND scope_id = :a "
        "AND revoked_at IS NULL",
        u=str(fw.accountant),
        a=str(t.admin_a),
    )

    waiting = await _another_staples(t, "30.25", "2026-09-20")
    assert await _pending_proposal(t, waiting) is not None
    rule = await _as_org(
        t.org_a,
        "SELECT row(status, suspended_reason)::text FROM booking_rule WHERE id = :id",
        id=rule_id,
    )
    assert rule == "(suspended,creator_not_authorized)"
    listing = await _call(t, t.admin_a, "GET", "/rules")
    [shown] = listing.json()["rules"]
    assert (shown["status"], shown["suspended_reason"]) == ("suspended", "creator_not_authorized")
    suspended_audit = await _scalar(
        t,
        "SELECT count(*) FROM audit_log WHERE action = 'suspend_booking_rule' "
        "AND resource_id = :id AND actor_type = 'system'",
        id=rule_id,
    )
    assert suspended_audit == 1

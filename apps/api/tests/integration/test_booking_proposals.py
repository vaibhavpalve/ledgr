"""Booking proposals against a real Postgres (migration 0079, ADR-110).

Real HTTP, real routes, real RLS. What this proves that the fakes in tests/firm/test_proposals.py
cannot: that a statement import really writes a proposal for a certain match, that approving one
really books it through the Bank screen's own path (and only once, however often it is retried),
that rejecting one leaves the ledger alone, that 0079's trigger and grants hold, and that another
tenant can neither see nor decide any of it (IAM-005).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest). Needs migrations
0065-0079 applied.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.exc import DBAPIError

from api.main import app
from tests.integration.test_bank_routes import (  # noqa: F401 - the autouse fixture is reused
    _balance,
    _bank_paid_receipt,
    _call,
    _email_verified,
)
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants


async def _firm(
    tenants: SeededTenants,
    method: str,
    path: str,
    json: object = None,
    *,
    as_b: bool = False,
    key: str | None = None,
) -> Response:
    org, user = (tenants.org_b, tenants.owner_b) if as_b else (tenants.org_a, tenants.owner_a)
    token = make_token(org, user_id=user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key or str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


async def _proposed(tenants: SeededTenants) -> dict[str, Any]:
    """A bank-paid receipt and the statement line that paid it - imported after the receipt was
    posted, so the import itself proposes the match."""
    world = await _bank_paid_receipt(tenants, settles_through="2000")
    assert world["suggestion"]["confidence"] == "high", world["suggestion"]
    listing = await _firm(tenants, "GET", "/v1/firm/proposals")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1, body
    [group] = body["groups"]
    [proposal] = group["proposals"]
    return {**world, "group": group, "proposal_id": proposal["id"], "proposal": proposal}


@pytest.mark.isolation("GET", "/v1/firm/proposals")
async def test_an_import_proposes_a_certain_match_grouped_for_review(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    group = world["group"]
    assert group["account_code"] == "4100"
    assert group["account_name"] == "Kantoorkosten"
    assert group["group_key"] == "staples|4100"
    assert (group["count"], group["client_count"]) == (1, 1)
    assert group["total_amount"] == "121.00"
    assert world["proposal"]["amount"] == "121.00"
    assert world["proposal"]["administration_id"] == str(two_organizations.admin_a)
    # Proposing posted nothing: the money still waits in Kruisposten.
    assert await _balance(two_organizations, world["transit"]) == "-121.00"

    # Another tenant's owner sees none of it.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        foreign = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/firm/proposals",
            as_org=two_organizations.org_b,
            as_user=two_organizations.owner_b,
            foreign_record_ids=[world["proposal_id"], two_organizations.admin_a],
        )
        assert foreign.json() == {"total": 0, "groups": []}
        # Naming the other tenant's administration explicitly does not widen it.
        named = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/firm/proposals?administration_ids={two_organizations.admin_a}",
            as_org=two_organizations.org_b,
            as_user=two_organizations.owner_b,
            foreign_record_ids=[world["proposal_id"]],
        )
        assert named.json()["total"] == 0


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/proposals")
async def test_one_clients_proposals_and_another_tenant_cannot_read_them(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    mine = await _call(two_organizations, two_organizations.admin_a, "GET", "/proposals")
    assert mine.status_code == 200, mine.text
    assert mine.json()["total"] == 1

    token_b = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        foreign = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/proposals",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert foreign.status_code in {403, 404}, foreign.text
    assert world["proposal_id"] not in foreign.text


@pytest.mark.isolation("POST", "/v1/firm/proposals/decide")
async def test_approving_books_once_and_another_tenant_cannot_decide(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    approve = {"decisions": [{"proposal_id": world["proposal_id"], "decision": "approve"}]}

    foreign = await _firm(
        two_organizations, "POST", "/v1/firm/proposals/decide", approve, as_b=True
    )
    assert foreign.status_code == 200, foreign.text
    assert foreign.json() == {
        "approved": 0,
        "rejected": 0,
        "failed": [{"proposal_id": world["proposal_id"], "reason": "proposal_not_found"}],
    }
    assert await _balance(two_organizations, world["bank"]) == "0.00"

    decided = await _firm(two_organizations, "POST", "/v1/firm/proposals/decide", approve)
    assert decided.status_code == 200, decided.text
    assert decided.json() == {"approved": 1, "rejected": 0, "failed": []}
    # Booked through ADR-092's path: Kruisposten cleared, the bank moved once.
    assert await _balance(two_organizations, world["transit"]) == "0.00"
    assert await _balance(two_organizations, world["bank"]) == "-121.00"

    # NFR-032: a retry under a NEW key is answered again and books nothing more; one under the
    # SAME key is replayed by the idempotency middleware.
    again = await _firm(two_organizations, "POST", "/v1/firm/proposals/decide", approve)
    assert again.json() == {"approved": 1, "rejected": 0, "failed": []}
    key = str(uuid.uuid4())
    for _ in range(2):
        replay = await _firm(
            two_organizations, "POST", "/v1/firm/proposals/decide", approve, key=key
        )
        assert replay.status_code == 200, replay.text
    assert await _balance(two_organizations, world["bank"]) == "-121.00"

    status = await _scalar(
        two_organizations,
        "SELECT status FROM booking_proposal WHERE id = :id",
        id=world["proposal_id"],
    )
    assert status == "approved"
    line = await _scalar(
        two_organizations,
        "SELECT matched_expense_id FROM bank_transaction WHERE id = :id",
        id=world["transaction_id"],
    )
    assert str(line) == world["expense_id"]
    audited = await _scalar(
        two_organizations,
        "SELECT count(*) FROM audit_log WHERE action = 'approve_booking_proposal' "
        "AND resource_id = :id",
        id=world["proposal_id"],
    )
    assert audited == 1

    listing = await _firm(two_organizations, "GET", "/v1/firm/proposals")
    assert listing.json()["total"] == 0


async def test_rejecting_leaves_the_ledger_and_the_line_alone(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    rejected = await _firm(
        two_organizations,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": world["proposal_id"], "decision": "reject"}]},
    )
    assert rejected.json() == {"approved": 0, "rejected": 1, "failed": []}
    assert await _balance(two_organizations, world["transit"]) == "-121.00"
    assert await _balance(two_organizations, world["bank"]) == "0.00"
    line_status = await _scalar(
        two_organizations,
        "SELECT status FROM bank_transaction WHERE id = :id",
        id=world["transaction_id"],
    )
    assert line_status == "unmatched"

    # Approving after rejecting is refused; the decision stands.
    late = await _firm(
        two_organizations,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": world["proposal_id"], "decision": "approve"}]},
    )
    assert late.json()["failed"] == [
        {"proposal_id": world["proposal_id"], "reason": "proposal_already_decided"}
    ]
    assert await _balance(two_organizations, world["bank"]) == "0.00"


async def test_matching_on_the_bank_screen_supersedes_the_proposal(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    settled = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-transactions/{world['transaction_id']}/reconcile-with-expense",
        {"expense_id": world["expense_id"]},
    )
    assert settled.status_code == 200, settled.text
    status = await _scalar(
        two_organizations,
        "SELECT status FROM booking_proposal WHERE id = :id",
        id=world["proposal_id"],
    )
    assert status == "superseded"
    late = await _firm(
        two_organizations,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": world["proposal_id"], "decision": "approve"}]},
    )
    assert late.json()["failed"][0]["reason"] == "proposal_already_decided"
    assert await _balance(two_organizations, world["bank"]) == "-121.00"


async def test_a_decided_proposal_cannot_be_reopened_or_deleted(
    two_organizations: SeededTenants,
) -> None:
    world = await _proposed(two_organizations)
    await _firm(
        two_organizations,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": world["proposal_id"], "decision": "reject"}]},
    )
    with pytest.raises(DBAPIError, match="cannot change"):
        await _exec(
            two_organizations,
            "UPDATE booking_proposal SET status = 'pending', decided_at = NULL WHERE id = :id",
            id=world["proposal_id"],
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        await _exec(
            two_organizations,
            "DELETE FROM booking_proposal WHERE id = :id",
            id=world["proposal_id"],
        )


async def test_a_malformed_decision_is_refused_whole(two_organizations: SeededTenants) -> None:
    refused = await _firm(
        two_organizations,
        "POST",
        "/v1/firm/proposals/decide",
        {"decisions": [{"proposal_id": "not-a-uuid", "decision": "approve"}]},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["reason"] == "proposals_invalid"

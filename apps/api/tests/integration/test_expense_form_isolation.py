"""IAM-005 isolation tests for the expense form routes.

An expense holds what somebody spent, where, and how they paid for it. A leak
here is one client's spending visible to another firm - and a write across the
boundary is a claim entered against books nobody authorised.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants


async def _seed_expense(
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    supplier: str,
) -> uuid.UUID:
    """A capture item and the expense hanging off it, written directly.

    These tests are about the READ and WRITE paths of the form; going through
    capture would also need a fiscal year, an encryption key and a scanner,
    which tests/expenses/ already exercises.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO capture_session ("
                    "  organization_id, administration_id, opened_by_user_id"
                    ") VALUES (:org, :admin, :user) RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "user": str(user_id),
                },
            )
        ).scalar_one()
        item_id = (
            await conn.execute(
                text(
                    "INSERT INTO capture_item ("
                    "  organization_id, administration_id, session_id, position"
                    ") VALUES (:org, :admin, :session, 1) RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "session": str(session_id),
                },
            )
        ).scalar_one()
        return (
            await conn.execute(
                text(
                    "INSERT INTO expense ("
                    "  organization_id, administration_id, capture_item_id,"
                    "  submitted_by_user_id, supplier"
                    ") VALUES (:org, :admin, :item, :user, :supplier) RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "item": str(item_id),
                    "user": str(user_id),
                    "supplier": supplier,
                },
            )
        ).scalar_one()


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/expenses")
async def test_another_tenants_expense_list_is_not_readable(
    two_organizations: SeededTenants,
) -> None:
    """MOB-004's Approve/View list: the new endpoint must not become the one
    place a supplier name or amount leaks after every other route on this
    resource was already checked.
    """
    foreign = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="De Vries Groothandel",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/administrations/{two_organizations.admin_b}/expenses?status=draft",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign, two_organizations.admin_b],
        )

    assert response.status_code in (403, 404)
    assert "De Vries Groothandel" not in response.text


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/expenses/{expense_id}")
async def test_another_tenants_expense_is_not_readable(
    two_organizations: SeededTenants,
) -> None:
    foreign = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="De Vries Groothandel",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/administrations/{two_organizations.admin_b}/expenses/{foreign}",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign, two_organizations.admin_b],
        )

    assert response.status_code in (403, 404)
    # A supplier name is the client's own trading relationship.
    assert "De Vries Groothandel" not in response.text


@pytest.mark.isolation("PATCH", "/v1/administrations/{administration_id}/expenses/{expense_id}")
async def test_another_tenants_expense_cannot_be_edited(
    two_organizations: SeededTenants,
) -> None:
    foreign = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="De Vries Groothandel",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.patch(
            f"/v1/administrations/{two_organizations.admin_b}/expenses/{foreign}",
            json={"supplier": "Overwritten", "gross_amount": "999.00"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"expense-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        row = (
            await conn.execute(
                text("SELECT supplier, gross_amount FROM expense WHERE id = :id"),
                {"id": str(foreign)},
            )
        ).one()
    assert row.supplier == "De Vries Groothandel", "the other tenant's claim is untouched"
    assert row.gross_amount is None


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/expenses/{expense_id}/ready"
)
async def test_another_tenants_expense_cannot_be_submitted(
    two_organizations: SeededTenants,
) -> None:
    """Submitting somebody else's claim would put it in front of their
    approver on a decision they never made.
    """
    foreign = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="De Vries Groothandel",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/expenses/{foreign}/ready",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"ready-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        status = (
            await conn.execute(
                text("SELECT status FROM expense WHERE id = :id"), {"id": str(foreign)}
            )
        ).scalar_one()
    assert status == "draft"


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/expenses/{expense_id}/posting"
)
async def test_another_tenants_expense_cannot_be_posted(
    two_organizations: SeededTenants,
) -> None:
    """The write that would put one client's cost into another's books - and
    into their ledger, which is append-only, so the correction would be a
    reversing entry rather than a deletion (FR-GL-003).
    """
    foreign = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="De Vries Groothandel",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/expenses/{foreign}/posting",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"post-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        row = (
            await conn.execute(
                text("SELECT status, journal_entry_id FROM expense WHERE id = :id"),
                {"id": str(foreign)},
            )
        ).one()
        entries = (
            await conn.execute(
                text("SELECT count(*) FROM journal_entry WHERE administration_id = :admin"),
                {"admin": str(two_organizations.admin_b)},
            )
        ).scalar_one()

    assert row.status == "draft"
    assert row.journal_entry_id is None
    assert entries == 0, "nothing reached the other tenant's ledger"


async def test_a_posting_account_cannot_name_another_tenants_account(
    two_organizations: SeededTenants,
) -> None:
    """An account from another administration's chart would post one client's
    lunch into another client's books - the wrong-client failure FR-FRM-000a
    calls the worst in this product, arriving through a configuration table.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        foreign_account = (
            await conn.execute(
                text(
                    "INSERT INTO ledger_account ("
                    "  organization_id, administration_id, code, name, type"
                    ") VALUES (:org, :admin, '4000', 'Kantoorkosten', 'expense') "
                    "RETURNING id"
                ),
                {
                    "org": str(two_organizations.org_b),
                    "admin": str(two_organizations.admin_b),
                },
            )
        ).scalar_one()

    with pytest.raises(Exception) as raised:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO expense_posting_account ("
                    "  organization_id, administration_id, purpose, account_id"
                    ") VALUES (:org, :admin, 'vat_input', :account)"
                ),
                {
                    "org": str(two_organizations.org_a),
                    "admin": str(two_organizations.admin_a),
                    "account": str(foreign_account),
                },
            )

    assert "administration it maps" in str(raised.value)


async def test_a_category_suggestion_never_crosses_a_tenant(
    two_organizations: SeededTenants,
) -> None:
    """FR-EXP-001b's default is drawn from history, and history is somebody's
    spending. `expenses.suggest_category` is not SECURITY DEFINER precisely so
    that RLS confines what it can see.
    """
    await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="Albert Heijn",
    )
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        await conn.execute(
            text("UPDATE expense SET category = 'Boodschappen' WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        suggestion = (
            await conn.execute(
                text("SELECT expenses.suggest_category(:admin, :user, 'Albert Heijn')"),
                {
                    "admin": str(two_organizations.admin_b),
                    "user": str(two_organizations.owner_b),
                },
            )
        ).scalar_one()

    assert suggestion is None, (
        "org A asked for a suggestion naming org B's administration and user, and "
        "RLS returned nothing rather than another tenant's spending habits"
    )


async def test_rls_hides_expenses_from_the_other_tenant(
    two_organizations: SeededTenants,
) -> None:
    mine = await _seed_expense(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Mine",
    )
    theirs = await _seed_expense(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="Theirs",
    )

    async def visible(organization_id: uuid.UUID) -> set[uuid.UUID]:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization_id)},
            )
            result = await conn.execute(text("SELECT id FROM expense"))
            return {row.id for row in result}

    assert mine in await visible(two_organizations.org_a)
    assert theirs not in await visible(two_organizations.org_a)
    assert theirs in await visible(two_organizations.org_b)
    assert mine not in await visible(two_organizations.org_b)


async def test_the_vat_split_agrees_with_the_python_implementation(
    two_organizations: SeededTenants,
) -> None:
    """FR-EXP-001b's rule exists twice. tests/expenses/vat_cases.py is the
    table; this runs it through migration 0033's SQL and compares.

    Postgres `round(numeric, 2)` rounds half away from zero; Python's decimal
    default is ROUND_HALF_EVEN, so `api.expenses.vat` states ROUND_HALF_UP at
    every call site. This is what proves the two actually agree rather than
    happening to on the cases somebody thought of.
    """
    from api.expenses.vat import split_gross
    from tests.expenses.vat_cases import VAT_CASES

    async with app_engine.begin() as conn:
        for case in VAT_CASES:
            stored = (
                await conn.execute(
                    text("SELECT expenses.vat_from_gross(:gross, :rate) AS vat"),
                    {"gross": case.gross_amount, "rate": case.rate_percent},
                )
            ).scalar_one()

            python = split_gross(case.gross_amount, case.rate_percent)
            assert stored == case.expected_vat, f"SQL disagrees with the table: {case.why}"
            assert stored == python.vat, (
                f"SQL gave {stored} and Python gave {python.vat} for {case.gross} @ {case.rate}"
            )


async def test_the_database_refuses_a_ready_expense_that_is_incomplete(
    two_organizations: SeededTenants,
) -> None:
    """FR-EXP-001b's minimum and FR-EXP-001e's payment method, enforced for
    every writer rather than only for the form service.

    This is also what makes the correction to capture finalisation binding:
    the old behaviour - readying every draft when a session closed - is now
    impossible rather than merely no longer done.
    """
    expense_id = await _seed_expense(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    with pytest.raises(Exception) as raised:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text("UPDATE expense SET status = 'ready' WHERE id = :id"),
                {"id": str(expense_id)},
            )

    assert "expense_ready_is_complete" in str(raised.value)


async def test_the_database_holds_vat_to_the_rule(
    two_organizations: SeededTenants,
) -> None:
    """`expense_vat_is_derived`. A hand-written VAT figure that does not match
    the rule is refused, so the stored number cannot drift from the gross and
    the rate beside it.
    """
    expense_id = await _seed_expense(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    with pytest.raises(Exception) as raised:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    "UPDATE expense SET expense_date = :on, gross_amount = 121.00, "
                    "  vat_treatment = 'btw_21', vat_rate = 21.00, vat_amount = 30.00 "
                    "WHERE id = :id"
                ),
                {"id": str(expense_id), "on": date(2025, 6, 1)},
            )

    assert "expense_vat_is_derived" in str(raised.value)


async def test_net_is_generated_and_always_completes_the_gross(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-001's balance, as a column no writer supplies."""
    expense_id = await _seed_expense(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text(
                "UPDATE expense SET expense_date = :on, gross_amount = 100.00, "
                "  vat_treatment = 'btw_21', vat_rate = 21.00, "
                "  vat_amount = expenses.vat_from_gross(100.00, 21.00) "
                "WHERE id = :id"
            ),
            {"id": str(expense_id), "on": date(2025, 6, 1)},
        )
        row = (
            await conn.execute(
                text("SELECT gross_amount, vat_amount, net_amount FROM expense WHERE id = :id"),
                {"id": str(expense_id)},
            )
        ).one()

    assert row.vat_amount == Decimal("17.36")
    assert row.net_amount == Decimal("82.64")
    assert row.net_amount + row.vat_amount == row.gross_amount

"""ADR-086: a freshly onboarded administration can post.

Before 0067 a new administration had a chart and a fiscal year and nothing else, so its first
invoice was refused for a missing sales journal and its first receipt for a missing account
mapping. These tests onboard through the real route and assert the journals and mappings every
posting path looks up now exist, that every expense category the capture screen offers resolves
to an account, and that running the provisioning again changes nothing.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.expenses.categories import EXPENSE_CATEGORIES
from api.main import app
from tests.integration.test_onboarding_isolation import _CREATE_BODY, _headers, _rgs_is_loaded
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants, seed_session

pytestmark = pytest.mark.anyio

#: The tenant the reads run as - set by _onboard, which every test calls first.
_ORG: list[str] = [""]


async def _onboard(tenants: SeededTenants) -> str:
    if not await _rgs_is_loaded():
        pytest.skip("no current RGS version loaded - run `make load-rgs` first")
    _ORG[0] = str(tenants.org_a)
    session_id = await seed_session(app_engine, user_id=tenants.owner_a)
    token = make_token(tenants.org_a, user_id=tenants.owner_a, session_id=session_id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/v1/administrations", headers=_headers(token), json=_CREATE_BODY
        )
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


async def _rows(sql: str, **params: object) -> list[tuple[object, ...]]:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": _ORG[0]}
        )
        result = await conn.execute(text(sql), params)
        return [tuple(row) for row in result]


async def _snapshot(administration_id: str) -> dict[str, list[tuple[object, ...]]]:
    return {
        "journals": await _rows(
            "SELECT journal_type, code FROM ledger_journal WHERE administration_id = :a "
            "ORDER BY journal_type",
            a=administration_id,
        ),
        "sales": await _rows(
            "SELECT purpose, vat_treatment_key, account_id FROM sales_posting_account "
            "WHERE administration_id = :a ORDER BY purpose, vat_treatment_key NULLS FIRST",
            a=administration_id,
        ),
        "expense": await _rows(
            "SELECT purpose, category_key, account_id FROM expense_posting_account "
            "WHERE administration_id = :a ORDER BY purpose, category_key NULLS FIRST",
            a=administration_id,
        ),
    }


async def test_onboarding_creates_every_journal_a_posting_path_looks_for(
    two_organizations: SeededTenants,
) -> None:
    administration_id = await _onboard(two_organizations)
    snapshot = await _snapshot(administration_id)
    types = {journal_type for journal_type, _code in snapshot["journals"]}
    assert {"sales", "purchase", "bank", "cash", "memorial"} <= types


async def test_sales_map_revenue_per_treatment_and_output_vat(
    two_organizations: SeededTenants,
) -> None:
    administration_id = await _onboard(two_organizations)
    sales = (await _snapshot(administration_id))["sales"]
    revenue_keys = {key for purpose, key, _ in sales if purpose == "revenue"}
    # The fallback, plus the treatments the seeded revenue accounts name.
    assert None in revenue_keys
    assert {"btw_21", "btw_9", "btw_verlegd", "btw_icp", "btw_export"} <= revenue_keys
    assert [key for purpose, key, _ in sales if purpose == "vat_output"] == [None]

    [(vat_code,)] = await _rows(
        "SELECT a.code FROM sales_posting_account s JOIN ledger_account a ON a.id = s.account_id "
        "WHERE s.administration_id = :a AND s.purpose = 'vat_output'",
        a=administration_id,
    )
    assert vat_code == "1700"


async def test_every_capture_category_resolves_to_an_account(
    two_organizations: SeededTenants,
) -> None:
    """The drift guard 0067's header promises: a category added to EXPENSE_CATEGORIES and not
    to the migration's list would post to the fallback silently - this makes it loud."""
    administration_id = await _onboard(two_organizations)
    expense = (await _snapshot(administration_id))["expense"]
    mapped = {
        str(key).strip().lower()
        for purpose, key, _ in expense
        if purpose == "expense_category" and key
    }
    missing = [c.label for c in EXPENSE_CATEGORIES if c.label.strip().lower() not in mapped]
    assert missing == [], f"categories with no default mapping in 0067: {missing}"

    purposes = {purpose for purpose, _key, _ in expense}
    assert {
        "vat_input",
        "business_account",
        "business_card",
        "reimbursement_liability",
        "expense_category",
    } <= purposes


async def test_the_code_the_picker_shows_is_the_account_the_category_books_to(
    two_organizations: SeededTenants,
) -> None:
    """The capture screen shows "Office supplies 4100": that must be where it lands."""
    administration_id = await _onboard(two_organizations)
    rows = await _rows(
        "SELECT e.category_key, a.code FROM expense_posting_account e "
        "JOIN ledger_account a ON a.id = e.account_id "
        "WHERE e.administration_id = :a AND e.purpose = 'expense_category' "
        "AND e.category_key IS NOT NULL",
        a=administration_id,
    )
    booked = {str(label).lower(): code for label, code in rows}
    wrong = {
        c.label: (c.rgs_code, booked.get(c.label.lower()))
        for c in EXPENSE_CATEGORIES
        if booked.get(c.label.lower()) != c.rgs_code
    }
    assert wrong == {}, f"shown code vs booked account (BV chart): {wrong}"


async def test_provisioning_again_changes_nothing(two_organizations: SeededTenants) -> None:
    administration_id = await _onboard(two_organizations)
    before = await _snapshot(administration_id)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": _ORG[0]}
        )
        await conn.execute(text("SELECT app.ensure_posting_defaults(:a)"), {"a": administration_id})
    assert await _snapshot(administration_id) == before


async def test_an_existing_mapping_is_never_overwritten(two_organizations: SeededTenants) -> None:
    administration_id = await _onboard(two_organizations)
    [(other_account,)] = await _rows(
        "SELECT id FROM ledger_account WHERE administration_id = :a AND code = '4000'",
        a=administration_id,
    )
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": _ORG[0]}
        )
        await conn.execute(
            text(
                "UPDATE expense_posting_account SET account_id = :acct "
                "WHERE administration_id = :a AND category_key = 'Office supplies'"
            ),
            {"acct": str(other_account), "a": administration_id},
        )
        await conn.execute(text("SELECT app.ensure_posting_defaults(:a)"), {"a": administration_id})
    [(account_id,)] = await _rows(
        "SELECT account_id FROM expense_posting_account "
        "WHERE administration_id = :a AND category_key = 'Office supplies'",
        a=administration_id,
    )
    assert uuid.UUID(str(account_id)) == uuid.UUID(str(other_account))

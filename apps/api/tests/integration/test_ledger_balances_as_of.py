"""`ledger.balances_as_of` (migration 0062): the dashboard's month-by-month cash read.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.integration.test_dunning import _overdue_invoice
from tests.integration.test_sales_invoice_posting import _exec
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

# The shared seed issues a 1210.00 invoice dated 2026-09-09, so on that day the
# receivable is debited 1210.00 and revenue plus output VAT are credited.


async def _as_of(tenants: SeededTenants, year: object, as_of: date) -> dict[object, Decimal]:
    result = await _exec(
        tenants,
        "SELECT account_id, balance FROM ledger.balances_as_of(:admin, :year, :as_of)",
        admin=str(tenants.admin_a),
        year=str(year),
        as_of=as_of,
    )
    return {row.account_id: Decimal(row.balance) for row in result}


async def test_nothing_counts_before_the_entry_is_dated(two_organizations: SeededTenants) -> None:
    world = await _overdue_invoice(two_organizations)

    before = await _as_of(two_organizations, world["year"], date(2026, 9, 8))
    on_the_day = await _as_of(two_organizations, world["year"], date(2026, 9, 9))

    assert before == {}
    assert sum(on_the_day.values(), Decimal(0)) == Decimal("0.00")  # balanced entry
    assert Decimal("1210.00") in on_the_day.values()


async def test_the_last_day_of_the_year_agrees_with_the_trial_balance(
    two_organizations: SeededTenants,
) -> None:
    world = await _overdue_invoice(two_organizations)

    as_of = await _as_of(two_organizations, world["year"], date(2026, 12, 31))
    trial = await _exec(
        two_organizations,
        "SELECT account_id, balance FROM ledger.trial_balance(:admin, :year) "
        "WHERE total_debit <> 0 OR total_credit <> 0",
        admin=str(two_organizations.admin_a),
        year=str(world["year"]),
    )

    assert as_of == {row.account_id: Decimal(row.balance) for row in trial}


async def test_another_organization_sees_none_of_it(two_organizations: SeededTenants) -> None:
    """Row-level security applies to the caller: the function is SECURITY INVOKER."""
    world = await _overdue_invoice(two_organizations)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        result = await conn.execute(
            text("SELECT account_id, balance FROM ledger.balances_as_of(:admin, :year, :as_of)"),
            {
                "admin": str(two_organizations.admin_a),
                "year": str(world["year"]),
                "as_of": date(2026, 12, 31),
            },
        )
        assert list(result) == []

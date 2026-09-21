"""IAM-005: the mobile home-screen dashboard answers nothing across a tenant
boundary.

    IAM-005  Automated tests assert tenant isolation on every endpoint; a new
             endpoint cannot ship without an isolation test.

Same shape as `test_sales_invoice_isolation.py`: organization A's Owner asks
for organization B's dashboard and must be refused, whether by authorization
(403) or by RLS filtering the fiscal year out from under the query (404) - both
are correct, and which one arrives is not the property under test.

The fiscal year id is invented rather than seeded, matching that file's own
reasoning: a caller with no access to administration B must be refused before
any id is ever looked up, so a fabricated one proves more than a real one
would.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.integration.test_dunning import _overdue_invoice
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

REFUSED = {403, 404}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/dashboard")
async def test_reading_another_tenants_dashboard_is_refused(
    two_organizations: SeededTenants,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_b}/dashboard"
            f"?fiscal_year_id={uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code in REFUSED


async def test_the_owner_reads_their_own_dashboard_with_the_new_fields(
    two_organizations: SeededTenants,
) -> None:
    """The route end to end against Postgres: an overdue invoice shows what is still
    owed, and the cash history, change and next-return fields have their shape."""
    world = await _overdue_invoice(two_organizations, due="2026-09-10")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}/dashboard"
            f"?fiscal_year_id={world['year']}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()

    overdue = [i for i in body["items_needing_action"] if i["kind"] == "overdue_invoice"]
    assert [i["amount"] for i in overdue] == ["1210.00"]
    assert body["receipts_to_review"] == 0

    assert isinstance(body["cash_history"], list)
    for point in body["cash_history"]:
        assert set(point) == {"month", "balance"}
    if len(body["cash_history"]) >= 2:
        previous, latest = body["cash_history"][-2:]
        from decimal import Decimal

        assert Decimal(body["cash_change"]) == Decimal(latest["balance"]) - Decimal(
            previous["balance"]
        )

    vat_return = body["vat_return"]
    assert vat_return is not None
    # Due the last day of the month after the period ends (api.vat.deadlines).
    assert vat_return["period_start"] < vat_return["period_end"] < vat_return["due_date"]
    assert vat_return["due_date"][8:] in {"28", "29", "30", "31"}

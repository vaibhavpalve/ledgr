"""CSV exports against a real Postgres - api.exports.routes (ADR-089).

An onboarded company posts its opening balance through the real route, then exports its journal
lines and trial balance; another tenant's token gets neither.
"""

from __future__ import annotations

import pytest

from tests.integration.test_opening_balance_routes import _call, _world
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


async def _with_opening(tenants: SeededTenants) -> tuple[str, str]:
    admin, year, codes = await _world(tenants)
    posted = await _call(
        tenants,
        admin,
        "POST",
        "/opening-balance",
        body={
            "fiscal_year_id": year,
            "lines": [
                {"account_id": codes["1100"], "debit": "12500.00"},
                {"account_id": codes["0500"], "credit": "12500.00"},
            ],
        },
    )
    assert posted.status_code == 200, posted.text
    return admin, year


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/exports/journal.csv")
async def test_the_journal_exports_every_line_the_dutch_way(
    two_organizations: SeededTenants,
) -> None:
    admin, year = await _with_opening(two_organizations)
    response = await _call(
        two_organizations, admin, "GET", f"/exports/journal.csv?fiscal_year_id={year}"
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "grootboekmutaties_2026-01-01_2026-12-31.csv" in response.headers["content-disposition"]
    rows = response.content.decode("utf-8-sig").splitlines()
    assert rows[0].startswith("boeking;datum;dagboek")
    assert any(";1100;" in row and ";12500,00;0,00;" in row for row in rows[1:])

    foreign = await _call(
        two_organizations,
        admin,
        "GET",
        f"/exports/journal.csv?fiscal_year_id={year}",
        foreign=True,
    )
    assert foreign.status_code in {403, 404}
    assert "12500" not in foreign.text


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/exports/trial-balance.csv")
async def test_the_trial_balance_exports_balances(two_organizations: SeededTenants) -> None:
    admin, year = await _with_opening(two_organizations)
    response = await _call(
        two_organizations,
        admin,
        "GET",
        f"/exports/trial-balance.csv?fiscal_year_id={year}&dialect=international",
    )
    assert response.status_code == 200, response.text
    rows = response.text.splitlines()
    assert rows[0] == "rekening,rekeningnaam,soort,debet,credit,saldo"
    assert any(row.startswith("1100,") and row.endswith(",12500.00,0.00,12500.00") for row in rows)

    foreign = await _call(
        two_organizations,
        admin,
        "GET",
        f"/exports/trial-balance.csv?fiscal_year_id={year}",
        foreign=True,
    )
    assert foreign.status_code in {403, 404}

"""The opening balance against a real Postgres - api.opening.routes (ADR-088).

A company is onboarded through the real route (chart seeded from RGS, fiscal year and periods
opened, posting defaults provisioned), then its opening balance is read and posted over HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient, Response

from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_posting_defaults import _onboard, _rows
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants


class _Verified:
    async def is_verified(self, user_id: uuid.UUID) -> bool:
        return True


@pytest.fixture(autouse=True)
async def _email_verified() -> AsyncIterator[None]:
    app.dependency_overrides[get_email_verification_checker] = lambda: _Verified()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_email_verification_checker, None)


async def _call(
    tenants: SeededTenants,
    admin: str,
    method: str,
    path: str,
    *,
    body: object = None,
    foreign: bool = False,
) -> Response:
    token = (
        make_token(tenants.org_b, user_id=tenants.owner_b)
        if foreign
        else make_token(tenants.org_a, user_id=tenants.owner_a)
    )
    headers = {"Authorization": f"Bearer {token}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(
            method, f"/v1/administrations/{admin}{path}", headers=headers, json=body
        )


async def _world(tenants: SeededTenants) -> tuple[str, str, dict[str, str]]:
    admin = await _onboard(tenants)
    [(year,)] = await _rows("SELECT id FROM fiscal_year WHERE administration_id = :a", a=admin)
    codes = {
        str(code): str(account_id)
        for account_id, code in await _rows(
            "SELECT id, code FROM ledger_account WHERE administration_id = :a", a=admin
        )
    }
    return admin, str(year), codes


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/opening-balance")
async def test_the_form_offers_balance_sheet_accounts_only(
    two_organizations: SeededTenants,
) -> None:
    admin, year, codes = await _world(two_organizations)
    response = await _call(
        two_organizations, admin, "GET", f"/opening-balance?fiscal_year_id={year}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    offered = {a["code"] for a in body["accounts"]}
    assert "1100" in offered and "0500" in offered
    assert "8000" not in offered  # revenue
    assert "1300" not in offered  # debtors: a control account
    assert body["posted"] is None
    assert body["entry_date"] == "2026-01-01"

    foreign = await _call(
        two_organizations, admin, "GET", f"/opening-balance?fiscal_year_id={year}", foreign=True
    )
    assert foreign.status_code in {403, 404}
    assert codes["1100"] not in foreign.text


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/opening-balance")
async def test_posting_it_once_and_only_once(two_organizations: SeededTenants) -> None:
    admin, year, codes = await _world(two_organizations)
    body = {
        "fiscal_year_id": year,
        "lines": [
            {"account_id": codes["1100"], "debit": "12500.00"},
            {"account_id": codes["0500"], "credit": "10000.00"},
        ],
        "balance_account_id": codes["0530"],
    }

    foreign = await _call(
        two_organizations, admin, "POST", "/opening-balance", body=body, foreign=True
    )
    assert foreign.status_code in {403, 404}

    posted = await _call(two_organizations, admin, "POST", "/opening-balance", body=body)
    assert posted.status_code == 200, posted.text
    lines = {
        line["account_code"]: (line["debit"], line["credit"])
        for line in posted.json()["posted"]["lines"]
    }
    assert lines == {
        "1100": ("12500.00", "0.00"),
        "0500": ("0.00", "10000.00"),
        "0530": ("0.00", "2500.00"),
    }
    assert posted.json()["posted"]["entry_date"] == "2026-01-01"

    again = await _call(two_organizations, admin, "POST", "/opening-balance", body=body)
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "opening_already_posted"

    read = await _call(two_organizations, admin, "GET", f"/opening-balance?fiscal_year_id={year}")
    assert read.json()["posted"]["id"] == posted.json()["posted"]["id"]

    balance = await _call(two_organizations, admin, "GET", f"/trial-balance?fiscal_year_id={year}")
    assert balance.json()["balanced"] is True


async def test_a_control_account_is_refused_by_name(two_organizations: SeededTenants) -> None:
    admin, year, codes = await _world(two_organizations)
    refused = await _call(
        two_organizations,
        admin,
        "POST",
        "/opening-balance",
        body={
            "fiscal_year_id": year,
            "lines": [
                {"account_id": codes["1300"], "debit": "100.00"},
                {"account_id": codes["0500"], "credit": "100.00"},
            ],
        },
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "opening_control_account"
    assert refused.json()["detail"]["code"] == "1300"

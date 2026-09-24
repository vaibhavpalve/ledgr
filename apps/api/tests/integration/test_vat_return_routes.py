"""The BTW screen against a real Postgres - api.vat_returns.routes (ADR-087).

Real HTTP, real routes, the real ledger. The world is one quarter with a 21% sale, a 21% purchase
and a reverse-charged EU purchase, posted through `ledger.post_entry` with each line's VAT
treatment - the same column every posting path writes (0035). No chart is seeded, so the VAT
accounts are recognised through the posting configuration that sends VAT to them.

    1a   turnover 1000.00   VAT 210.00
    4b   turnover   50.00   VAT  10.50 -> 10 (down)
    5a   220
    5b   21.00 + 10.50 = 31.50 -> 32 (up)
    total due 188
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient, Response

from api.auth.email_verification import get_email_verification_checker
from api.main import app
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


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
    admin: uuid.UUID,
    method: str,
    path: str,
    *,
    body: object = None,
    as_org: uuid.UUID | None = None,
    as_user: uuid.UUID | None = None,
) -> Response:
    token = make_token(as_org or tenants.org_a, user_id=as_user or tenants.owner_a)
    headers = {"Authorization": f"Bearer {token}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method, f"/v1/administrations/{admin}{path}", headers=headers, json=body
        )


async def _account(tenants: SeededTenants, code: str, name: str, kind: str) -> uuid.UUID:
    return await _scalar(
        tenants,
        "SELECT (ledger.create_account(:admin, :code, :name, :kind, NULL, NULL, NULL)).id",
        admin=str(tenants.admin_a),
        code=code,
        name=name,
        kind=kind,
    )


async def _post(
    tenants: SeededTenants,
    *,
    journal: uuid.UUID,
    period: uuid.UUID,
    on: str,
    description: str,
    lines: list[tuple[uuid.UUID, str, str, str | None]],
) -> None:
    payload = [
        {"account_id": str(a), "debit": d, "credit": c, "vat_treatment": t} for a, d, c, t in lines
    ]
    await _exec(
        tenants,
        "SELECT ledger.post_entry("
        "  p_administration_id => :admin, p_journal_id => :journal, p_period_id => :period,"
        "  p_entry_date => :on, p_description => :description,"
        "  p_document_reference => NULL, p_posted_by_user_id => NULL, p_source_system => 'test',"
        "  p_lines => CAST(:lines AS jsonb))",
        admin=str(tenants.admin_a),
        journal=str(journal),
        period=str(period),
        on=date.fromisoformat(on),
        description=description,
        lines=json.dumps(payload),
    )


async def _world(tenants: SeededTenants) -> dict[str, uuid.UUID]:
    admin, org = tenants.admin_a, tenants.org_a
    year = await _scalar(
        tenants,
        "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
        "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id",
        org=str(org),
        admin=str(admin),
    )
    period = await _scalar(
        tenants,
        "INSERT INTO period (organization_id, administration_id, fiscal_year_id, "
        "  period_number, start_date, end_date, status) "
        "VALUES (:org, :admin, :year, 2, '2026-04-01', '2026-06-30', 'open') RETURNING id",
        org=str(org),
        admin=str(admin),
        year=str(year),
    )
    bank = await _account(tenants, "1100", "Bank", "asset")
    vat_out = await _account(tenants, "1700", "Te betalen omzetbelasting", "liability")
    vat_in = await _account(tenants, "1720", "Te vorderen omzetbelasting", "asset")
    revenue = await _account(tenants, "8000", "Omzet hoog", "revenue")
    costs = await _account(tenants, "4100", "Kantoorkosten", "expense")
    # No chart is seeded here, so the VAT accounts are recognised the other way the return
    # knows them: by the posting configuration that sends VAT to them (0034, 0040).
    await _exec(
        tenants,
        "INSERT INTO sales_posting_account (organization_id, administration_id, purpose, "
        "  vat_treatment_key, account_id) VALUES (:org, :admin, 'vat_output', NULL, :acct)",
        org=str(org),
        admin=str(admin),
        acct=str(vat_out),
    )
    await _exec(
        tenants,
        "INSERT INTO expense_posting_account (organization_id, administration_id, purpose, "
        "  category_key, account_id) VALUES (:org, :admin, 'vat_input', NULL, :acct)",
        org=str(org),
        admin=str(admin),
        acct=str(vat_in),
    )
    journal = await _scalar(
        tenants,
        "SELECT (ledger.create_journal(:admin, 'MEM', 'Memoriaal', 'memorial')).id",
        admin=str(admin),
    )
    await _post(
        tenants,
        journal=journal,
        period=period,
        on="2026-05-10",
        description="Factuur 2026-001",
        lines=[
            (bank, "1210.00", "0.00", None),
            (revenue, "0.00", "1000.00", "btw_21"),
            (vat_out, "0.00", "210.00", "btw_21"),
        ],
    )
    await _post(
        tenants,
        journal=journal,
        period=period,
        on="2026-05-12",
        description="Kantoorartikelen",
        lines=[
            (costs, "100.00", "0.00", "btw_21"),
            (vat_in, "21.00", "0.00", "btw_21"),
            (bank, "0.00", "121.00", None),
        ],
    )
    await _post(
        tenants,
        journal=journal,
        period=period,
        on="2026-05-20",
        description="Advertenties (IE)",
        lines=[(costs, "50.00", "0.00", "btw_icp"), (bank, "0.00", "50.00", None)],
    )
    return {"year": year, "period": period}


def _box(body: dict[str, object], code: str) -> dict[str, object]:
    boxes = body["boxes"]
    assert isinstance(boxes, list)
    return next(b for b in boxes if b["code"] == code)


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/vat-returns")
async def test_the_overview_lists_the_quarter_with_its_total(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns?fiscal_year_id={world['year']}",
    )
    assert response.status_code == 200, response.text
    [quarter] = response.json()["returns"]
    assert quarter["period_id"] == str(world["period"])
    assert quarter["total_due"] == "188.00"
    assert quarter["due_date"] == "2026-07-31"

    foreign = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns?fiscal_year_id={world['year']}",
        as_org=two_organizations.org_b,
        as_user=two_organizations.owner_b,
    )
    assert foreign.status_code in {403, 404}
    assert str(world["period"]) not in foreign.text


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/vat-returns/{period_id}")
async def test_one_return_has_every_box_and_its_checks(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations, two_organizations.admin_a, "GET", f"/vat-returns/{world['period']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (_box(body, "1a")["turnover_rounded"], _box(body, "1a")["vat_rounded"]) == (
        "1000.00",
        "210.00",
    )
    assert (_box(body, "4b")["turnover_rounded"], _box(body, "4b")["vat_rounded"]) == (
        "50.00",
        "10.00",
    )
    assert _box(body, "5a")["vat_rounded"] == "220.00"
    assert _box(body, "5b")["vat_rounded"] == "32.00"
    assert body["total_due"] == "188.00"
    assert body["can_be_filed"] is True
    assert {c["severity"] for c in body["checks"]} <= {"warning"}

    foreign = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns/{world['period']}",
        as_org=two_organizations.org_b,
        as_user=two_organizations.owner_b,
    )
    assert foreign.status_code in {403, 404}
    assert "1000.00" not in foreign.text


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/vat-returns/{period_id}/boxes/{code}/lines"
)
async def test_a_box_drills_down_to_its_postings(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    response = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns/{world['period']}/boxes/1a/lines",
    )
    assert response.status_code == 200, response.text
    lines = response.json()["lines"]
    assert {(line["column"], line["amount"]) for line in lines} == {
        ("turnover", "1000.00"),
        ("vat", "210.00"),
    }
    assert all(line["description"] == "Factuur 2026-001" for line in lines)

    unknown = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns/{world['period']}/boxes/9z/lines",
    )
    assert unknown.status_code == 404

    foreign = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/vat-returns/{world['period']}/boxes/1a/lines",
        as_org=two_organizations.org_b,
        as_user=two_organizations.owner_b,
    )
    assert foreign.status_code in {403, 404}
    assert "Factuur 2026-001" not in foreign.text


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/vat-returns/{period_id}/file"
)
async def test_filing_needs_the_warnings_acknowledged_and_then_locks_the_period(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    path = f"/vat-returns/{world['period']}/file"

    foreign = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        body={"acknowledged_warnings": ["provisional_ruleset"]},
        as_org=two_organizations.org_b,
        as_user=two_organizations.owner_b,
    )
    assert foreign.status_code in {403, 404}

    prepared = (
        await _call(
            two_organizations, two_organizations.admin_a, "GET", f"/vat-returns/{world['period']}"
        )
    ).json()
    warnings = [c["code"] for c in prepared["checks"] if c["severity"] == "warning"]

    if warnings:
        refused = await _call(two_organizations, two_organizations.admin_a, "POST", path, body={})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason"] == "vat_warnings_not_acknowledged"

    changed = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        body={"acknowledged_warnings": warnings, "expected_total": "187.00"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "vat_figures_changed"

    filed = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        body={
            "acknowledged_warnings": warnings,
            "expected_total": "188.00",
            "filing_reference": "OB-TEST-2026-Q2",
        },
    )
    assert filed.status_code == 200, filed.text
    body = filed.json()
    assert body["status"] == "filed"
    assert body["period_status"] == "vat_filed"
    assert body["total_due"] == "188.00"
    assert body["filed"]["filing_reference"] == "OB-TEST-2026-Q2"
    assert body["filed"]["warnings_acknowledged"] == warnings

    # Read back: the stored return, in the same shape it was prepared in.
    again = await _call(
        two_organizations, two_organizations.admin_a, "GET", f"/vat-returns/{world['period']}"
    )
    assert again.json()["boxes"] == prepared["boxes"]
    assert again.json()["status"] == "filed"

    twice = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        path,
        body={"acknowledged_warnings": warnings},
    )
    assert twice.status_code == 409
    assert twice.json()["detail"]["reason"] == "vat_return_already_filed"

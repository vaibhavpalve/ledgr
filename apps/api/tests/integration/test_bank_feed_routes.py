"""The live bank feed's routes against a real Postgres (ADR-108, migration 0077).

Real HTTP, real BankFeedService/BankService, real RLS; only the provider is the in-memory fake,
through the `get_bank_feed_provider` dependency. What this proves that the unit tests cannot: that
a fetched line lands in bank_transaction through the statement import's own insert (so the same
line uploaded later in a file is booked once), and that another tenant can neither see nor drive
any of it (IAM-005).

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest). Needs migration
0077 applied.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from api.bank.adapters import UnconfiguredBankFeed
from api.bank.feed_job import sync_due_feeds
from api.bank.feed_routes import feed_settings, get_bank_feed_provider
from api.db import engine
from api.main import app
from tests.bank.test_feed_service import FakeProvider
from tests.integration.test_bank_routes import _bank_world, _call
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

_FEED = "/v1/administrations/{administration_id}/bank-accounts/{bank_account_id}/feed"
_IBAN = "NL91ABNA0417164300"


@pytest.fixture(autouse=True)
def provider() -> AsyncIterator[FakeProvider]:
    fake = FakeProvider()
    app.dependency_overrides[get_bank_feed_provider] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_bank_feed_provider, None)


async def _account(tenants: SeededTenants) -> str:
    world = await _bank_world(tenants)
    created = await _call(
        tenants,
        tenants.admin_a,
        "POST",
        "/bank-accounts",
        {"name": "ABN AMRO", "iban": _IBAN, "ledger_account_id": str(world["bank_ledger_account"])},
    )
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


async def _as_b(method: str, path: str, tenants: SeededTenants, json: object = None):  # type: ignore[no-untyped-def]
    token = make_token(tenants.org_b, user_id=tenants.owner_b)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
            json=json,
        )


async def _link(tenants: SeededTenants, account_id: str) -> str:
    started = await _call(
        tenants,
        tenants.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/connect",
        {"institution_id": "ABNAMRO_ABNANL2A", "institution_name": "ABN AMRO", "language": "en"},
    )
    assert started.status_code == 200, started.text
    connection_id = started.json()["connection"]["id"]
    completed = await _call(
        tenants, tenants.admin_a, "POST", f"/bank-feed/connections/{connection_id}/complete"
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["connection"]["status"] == "linked"
    return str(connection_id)


@pytest.mark.isolation("GET", _FEED)
async def test_feed_status_is_the_owners_alone(two_organizations: SeededTenants) -> None:
    account_id = await _account(two_organizations)
    status = await _call(
        two_organizations, two_organizations.admin_a, "GET", f"/bank-accounts/{account_id}/feed"
    )
    assert status.status_code == 200, status.text
    assert status.json() == {"configured": True, "provider": "gocardless", "connection": None}

    other = await _as_b(
        "GET",
        f"/v1/administrations/{two_organizations.admin_a}/bank-accounts/{account_id}/feed",
        two_organizations,
    )
    assert other.status_code in {403, 404}


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/bank-feed/institutions")
async def test_institutions_need_access_to_the_administration(
    two_organizations: SeededTenants,
) -> None:
    listing = await _call(
        two_organizations, two_organizations.admin_a, "GET", "/bank-feed/institutions?country=NL"
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["institutions"][0]["id"] == "ING"

    other = await _as_b(
        "GET",
        f"/v1/administrations/{two_organizations.admin_a}/bank-feed/institutions",
        two_organizations,
    )
    assert other.status_code in {403, 404}


@pytest.mark.isolation("POST", f"{_FEED}/connect")
async def test_connect_returns_the_banks_consent_link(two_organizations: SeededTenants) -> None:
    account_id = await _account(two_organizations)
    other = await _as_b(
        "POST",
        f"/v1/administrations/{two_organizations.admin_a}/bank-accounts/{account_id}/feed/connect",
        two_organizations,
        {"institution_id": "ING"},
    )
    assert other.status_code in {403, 404}

    started = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/connect",
        {"institution_id": "ING"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["link"] == "https://bank.example/consent"
    assert started.json()["connection"]["status"] == "pending"


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/bank-feed/connections/{connection_id}/complete"
)
async def test_completing_imports_through_the_statement_path_and_dedupes_with_a_file(
    two_organizations: SeededTenants,
) -> None:
    account_id = await _account(two_organizations)
    connection_id = await _link(two_organizations, account_id)

    listing = await _call(
        two_organizations,
        two_organizations.admin_a,
        "GET",
        f"/bank-accounts/{account_id}/transactions",
    )
    assert [t["amount"] for t in listing.json()["transactions"]] == ["-12.50"]

    # The same line, uploaded later in a statement file: recognised, not booked twice.
    upload = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/import",
        {
            "csv": "date,amount,counterparty_name,counterparty_iban,description\n"
            "2026-10-01,-12.50,KPN,,Telefoon\n"
        },
    )
    assert upload.status_code == 200, upload.text
    assert upload.json() == {
        "import_id": upload.json()["import_id"],
        "transaction_count": 0,
        "duplicate_count": 1,
    }

    # Another tenant cannot complete (or even find) this connection.
    other = await _as_b(
        "POST",
        f"/v1/administrations/{two_organizations.admin_b}/bank-feed/connections/{connection_id}/complete",
        two_organizations,
    )
    assert other.status_code == 404


@pytest.mark.isolation("POST", f"{_FEED}/sync")
async def test_sync_reads_again_without_doubling(two_organizations: SeededTenants) -> None:
    account_id = await _account(two_organizations)
    await _link(two_organizations, account_id)

    synced = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/sync",
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["imported"]["transaction_count"] == 0
    assert synced.json()["imported"]["duplicate_count"] == 1

    other = await _as_b(
        "POST",
        f"/v1/administrations/{two_organizations.admin_a}/bank-accounts/{account_id}/feed/sync",
        two_organizations,
    )
    assert other.status_code in {403, 404}


@pytest.mark.isolation("POST", f"{_FEED}/disconnect")
async def test_disconnect_revokes_and_stops_syncing(
    two_organizations: SeededTenants, provider: FakeProvider
) -> None:
    account_id = await _account(two_organizations)
    await _link(two_organizations, account_id)

    other = await _as_b(
        "POST",
        f"/v1/administrations/{two_organizations.admin_a}/bank-accounts/{account_id}/feed/disconnect",
        two_organizations,
    )
    assert other.status_code in {403, 404}

    revoked = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/disconnect",
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["connection"]["status"] == "revoked"
    assert provider.revoked == ["req-1"]

    again = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/sync",
    )
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "bank_feed_not_linked"


async def test_without_a_provider_the_feed_says_so(two_organizations: SeededTenants) -> None:
    app.dependency_overrides[get_bank_feed_provider] = lambda: UnconfiguredBankFeed()
    account_id = await _account(two_organizations)
    status = await _call(
        two_organizations, two_organizations.admin_a, "GET", f"/bank-accounts/{account_id}/feed"
    )
    assert status.json()["configured"] is False

    started = await _call(
        two_organizations,
        two_organizations.admin_a,
        "POST",
        f"/bank-accounts/{account_id}/feed/connect",
        {"institution_id": "ING"},
    )
    assert started.status_code == 409
    assert started.json()["detail"]["reason"] == "bank_feed_not_configured"


async def test_the_daily_job_syncs_each_feed_under_its_own_tenant(
    two_organizations: SeededTenants, provider: FakeProvider
) -> None:
    account_id = await _account(two_organizations)
    connection_id = await _link(two_organizations, account_id)
    ops = create_async_engine(os.environ["OPS_DATABASE_URL"])
    try:
        # Just linked (and read): not due yet.
        now = datetime.now(UTC)
        quiet = await sync_due_feeds(
            ops_engine=ops, app_engine=engine, provider=provider, settings=feed_settings(), now=now
        )
        assert connection_id not in " ".join(quiet.details)

        # A day later it is due. The job's write passes RLS only because each sync is scoped to
        # the connection's own organization.
        report = await sync_due_feeds(
            ops_engine=ops,
            app_engine=engine,
            provider=provider,
            settings=feed_settings(),
            now=now + timedelta(days=1),
        )
    finally:
        await ops.dispose()
    # Other tests' connections share this database, each in its own organization: every one is
    # synced under its own tenant, and none fails.
    assert report.due >= 1 and report.synced == report.due and report.failed == 0, report.details
    ours = [line for line in report.details if connection_id in line]
    assert ours == [
        f"synced: connection {connection_id} (administration {two_organizations.admin_a}): "
        "0 new, 1 already there"
    ]  # the same line again: de-duplicated

    status = await _call(
        two_organizations, two_organizations.admin_a, "GET", f"/bank-accounts/{account_id}/feed"
    )
    assert status.json()["connection"]["last_error"] is None

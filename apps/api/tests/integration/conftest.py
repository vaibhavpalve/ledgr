"""Fixtures for tests that need a live Postgres with migrations applied.

Seeding reuses api.db.engine - the same engine the running API uses - so
these tests write through DATABASE_URL as ledgr_app, exactly like
production traffic, and the app under test (imported via api.main in the
test files) reads back through that same connection. There is deliberately
no second "test database URL": one DATABASE_URL, pointed at a test Postgres
with migrations applied, is what makes seeding and serving see the same
data.

TENANT_ISOLATION_TESTS_ENABLED is a separate, explicit opt-in - DATABASE_URL
always resolves to *something* (api.config.Settings has a default), so its
mere presence can't be the skip signal, or these tests could silently run
destructive seeds against a developer's default local database. CI sets
both DATABASE_URL and this flag together, after applying migrations, in
apps/api/scripts/bootstrap_test_db.py.

The skip check is an AUTOUSE fixture (require_db below), not something each
test opts into - a test in this package that forgets to depend on
two_organizations (as tests/test_encryption_key_isolation.py's tests do;
they seed their own tenants directly) would otherwise skip the gate
entirely and attempt a real connection unconditionally. That's exactly the
bug this migration's predecessor version had (see git history / the
tenant-isolation-harness task): a module-level `pytestmark` in this file
looked like it would gate every sibling test, but pytestmark only has that
effect inside a test module itself, not a conftest. autouse=True fixtures
DO apply to every test collected under this directory, which is the
property actually needed here.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from api.db import engine as app_engine
from tests.support.seed import SeededTenants, seed_two_organizations_with_overlapping_data


def _isolation_tests_enabled() -> bool:
    return os.environ.get("TENANT_ISOLATION_TESTS_ENABLED") == "1"


@pytest.fixture(autouse=True)
def require_db() -> None:
    if not _isolation_tests_enabled():
        pytest.skip(
            "TENANT_ISOLATION_TESTS_ENABLED=1 not set - these tests write to a "
            "real Postgres (via DATABASE_URL) and need migrations applied. "
            "Run `make dev-up`, then `make test-api-db-setup`, then re-run with "
            "TENANT_ISOLATION_TESTS_ENABLED=1. CI does this automatically."
        )


@pytest_asyncio.fixture(autouse=True)
async def _return_pooled_connections_to_their_own_event_loop() -> AsyncIterator[None]:
    """Dispose the shared engine's pool after every test.

    `api.db.engine` is a module-level AsyncEngine with a normal connection
    pool, and pytest-asyncio gives each test its own event loop. An asyncpg
    connection belongs to the loop that opened it, so a connection pooled by
    one test and handed to the next arrives attached to a loop that has since
    been closed - which surfaces as `RuntimeError: Event loop is closed` and
    `'NoneType' object has no attribute 'send'` from inside the driver, during
    setup, on tests that have nothing to do with each other.

    Disposing here rather than switching the engine to NullPool keeps
    production's pooling exactly as it is: the fix belongs to the test session,
    which is what has many event loops, not to api.db, which has one.

    autouse, and in this conftest, so it covers every DB-backed test including
    the ones that seed their own tenants and never ask for `two_organizations`
    - the same reason `require_db` above is autouse.
    """
    yield
    await app_engine.dispose()


@pytest_asyncio.fixture
async def two_organizations() -> SeededTenants:
    return await seed_two_organizations_with_overlapping_data(app_engine)

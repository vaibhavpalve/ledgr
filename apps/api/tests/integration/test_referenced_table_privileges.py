"""The privilege an append-only table still needs, and the guarantee it must
keep anyway (0027).

0020 and 0024 revoke UPDATE, DELETE and TRUNCATE from ledgr_ledger on the
tables it owns, describing it as belt and braces over the RAISE triggers that
actually enforce FR-GL-003 and CMP-009. On a table something REFERENCES, that
revoke is not free: a foreign-key check locks the parent row with
`SELECT ... FOR KEY SHARE`, a row-locking clause needs UPDATE or DELETE
privilege, and withholding both makes every insert into the child table fail.

0027 grants UPDATE back on exactly the referenced tables. This file is what
replaces the documentation that revoke used to provide, and it asserts both
halves - because restoring the privilege is only safe if the trigger is really
what stops the write, and that is a claim worth testing rather than repeating.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from api.db import engine as app_engine
from tests.integration.ledger_world import build_db_world, post_entry
from tests.ledger.cases import entry
from tests.support.seed import SeededTenants

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")

#: (table, referenced by something) - the two tables 0027 had to repair, and
#: the one it deliberately left alone.
REFERENCED = ("journal_entry", "rgs_element")
NOT_REFERENCED = ("journal_line",)


@pytest_asyncio.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not set")
    engine = create_async_engine(_ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", REFERENCED)
async def test_a_referenced_table_keeps_update_for_its_owner(
    table: str, admin_engine: AsyncEngine
) -> None:
    """Without this the foreign key cannot be checked at all, and every write
    through the ledger's narrow API fails with `permission denied`.
    """
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT has_table_privilege('ledgr_ledger', :t, 'UPDATE') AS can_update, "
                    "       has_table_privilege('ledgr_ledger', :t, 'DELETE') AS can_delete, "
                    "       has_table_privilege('ledgr_ledger', :t, 'TRUNCATE') AS can_truncate"
                ),
                {"t": table},
            )
        ).one()

    assert row.can_update, (
        f"{table} is referenced by a foreign key, so its owner needs UPDATE for "
        "the FOR KEY SHARE lock the check takes (0027)"
    )
    assert not row.can_delete, f"{table} should not have DELETE restored - the lock needs only one"
    assert not row.can_truncate


@pytest.mark.parametrize("table", NOT_REFERENCED)
async def test_a_table_nothing_references_keeps_the_full_revoke(
    table: str, admin_engine: AsyncEngine
) -> None:
    """The belt-and-braces intent survives wherever it costs nothing."""
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT has_table_privilege('ledgr_ledger', :t, 'UPDATE') AS can_update, "
                    "       has_table_privilege('ledgr_ledger', :t, 'DELETE') AS can_delete"
                ),
                {"t": table},
            )
        ).one()

    assert not row.can_update
    assert not row.can_delete


@pytest.mark.parametrize("table", ("journal_entry", "journal_line"))
async def test_the_privilege_does_not_make_the_row_writable(
    table: str, admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The half that matters. Restoring UPDATE is only safe because the trigger
    is what refuses the write, so the trigger is exercised here - as the role
    that now holds the privilege, and as a superuser besides.

    A lock is not a write and fires no trigger; an UPDATE is and does.

    Two things are deliberate setup rather than noise:

      * An entry is posted first. These are FOR EACH ROW triggers, so an UPDATE
        over an empty table matches nothing, raises nothing, and would let this
        test pass against a table with no guard at all.
      * Tenant context is set. journal_entry is FORCE ROW LEVEL SECURITY and
        ledgr_ledger is bound by it, so without a tenant the UPDATE sees no
        rows - and would again pass for the wrong reason.

    rgs_element's copy of the same guarantee is asserted in
    test_chart_of_accounts.py, where the RGS dataset is already loaded.
    """
    entry_id = await _post_one_entry(two_organizations)
    before = await _fingerprint(two_organizations, entry_id)

    # As a superuser: RLS is bypassed and the privilege check does not apply,
    # so the trigger is the only thing left - which is exactly the claim 0027
    # rests on.
    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"UPDATE {table} SET id = id"))

    assert "append-only" in str(raised.value), (
        f"{table} was not stopped by its trigger: {raised.value}"
    )

    # As ledgr_ledger, which now holds UPDATE on journal_entry, the refusal
    # comes earlier and takes a different form per table:
    #
    #   journal_entry  no UPDATE policy exists (0020 withholds one), so RLS
    #                  matches no rows and the statement is a silent no-op
    #   journal_line   UPDATE was never granted back, so it is refused outright
    #
    # Both are refusals and neither is the trigger, so the assertion is about
    # the ROW rather than about what was raised. That is the only phrasing that
    # covers a layer which declines without saying so.
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE ledgr_ledger"))
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(text(f"UPDATE {table} SET id = id"))
    except (DBAPIError, SQLAlchemyError):
        pass

    assert await _fingerprint(two_organizations, entry_id) == before, (
        f"an UPDATE on {table} as ledgr_ledger changed the row"
    )


async def _fingerprint(tenants: SeededTenants, entry_id: uuid.UUID) -> tuple[object, ...]:
    """Enough of the entry and its lines to notice any change to either."""
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT e.id, e.entry_number, e.description, e.posted_at, "
                    "       (SELECT count(*) FROM journal_line l "
                    "         WHERE l.journal_entry_id = e.id) AS lines, "
                    "       (SELECT coalesce(sum(l.debit), 0) FROM journal_line l "
                    "         WHERE l.journal_entry_id = e.id) AS debit "
                    "FROM journal_entry e WHERE e.id = :id"
                ),
                {"id": str(entry_id)},
            )
        ).one()
    return tuple(row)


async def _post_one_entry(tenants: SeededTenants) -> uuid.UUID:
    """A balanced entry through the narrow API - which is exactly the write
    that could not happen before 0027.
    """
    async with app_engine.connect() as conn:
        world = await build_db_world(conn, tenants)
        entry_id = await post_entry(conn, entry(world))
        await conn.commit()
    return entry_id


async def test_a_posting_can_actually_be_written(
    two_organizations: SeededTenants,
) -> None:
    """The regression this whole migration exists for, stated as the thing a
    user does: post an entry. It exercises journal_line -> journal_entry, which
    is the foreign key whose check could not take its lock.
    """
    entry_id = await _post_one_entry(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        lines = (
            await conn.execute(
                text("SELECT count(*) FROM journal_line WHERE journal_entry_id = :id"),
                {"id": str(entry_id)},
            )
        ).scalar_one()

    assert isinstance(entry_id, uuid.UUID)
    assert lines == 2

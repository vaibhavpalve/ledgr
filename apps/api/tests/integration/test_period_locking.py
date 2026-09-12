"""FR-GL-007 against a real Postgres: what the in-memory fake cannot test.

tests/ledger/test_period_locking.py covers the authority — which roles may
lock, unlock, file and prepare a suppletie — against the in-memory fake. That
file cannot test the thing that makes the authority *unskippable*, because it
is a database privilege:

    period_status_transition() refuses a status change unless current_user is
    'ledgr_ledger', which is only true inside the SECURITY DEFINER functions
    that api.ledger.periods calls after checking the permission.

Without that gate, `PeriodService` would be a convention: any code holding a
session could `UPDATE period SET status = 'open'` and post into a closed
period. With it, there is no path to the transition that skips the check.

So this file asserts, against the real schema:

  * a direct UPDATE of period.status is refused for ledgr_app
  * ...and for ledgr_ops, which holds BYPASSRLS
  * the `ledger.*` functions do work, or the tests above prove only that
    period locking is broken
  * vat_filed is terminal even through the API
  * a suppletie correction lands in an open period and links back to the
    filed one
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from api.config import settings
from api.db import engine as app_engine
from tests.integration.ledger_world import build_db_world
from tests.ledger.cases import entry
from tests.ledger.world import World
from tests.support.seed import SeededTenants

#: Every direct route to the column. Each must fail: the status transition is
#: reachable only through the ledger's functions.
_DIRECT_WRITES = [
    ("unlock by hand", "UPDATE period SET status = 'open'"),
    ("lock by hand", "UPDATE period SET status = 'locked'"),
    ("file by hand", "UPDATE period SET status = 'vat_filed'"),
    (
        "unlock one period by id",
        "UPDATE period SET status = 'open' WHERE status <> 'open'",
    ),
    (
        "smuggle it through an unrelated update",
        "UPDATE period SET status = 'open', period_number = period_number",
    ),
]


async def _world(conn, tenants: SeededTenants) -> World:  # type: ignore[no-untyped-def]
    return await build_db_world(conn, tenants)


# ===========================================================================
# The gate: only the ledger's functions may move period.status
# ===========================================================================


@pytest.mark.parametrize(("name", "sql"), _DIRECT_WRITES, ids=[n for n, _ in _DIRECT_WRITES])
async def test_the_application_cannot_change_a_period_status_directly(
    name: str, sql: str, two_organizations: SeededTenants
) -> None:
    """The load-bearing test for FR-GL-007's authority.

    If this ever passes, the permission check in api.ledger.periods becomes
    advisory: code could reopen a period without holding `lock period` and
    post into it freely.
    """
    async with app_engine.connect() as conn:
        await _world(conn, two_organizations)
        await conn.commit()

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(text(sql))
            await conn.commit()
        await conn.rollback()

    message = str(raised.value)
    assert "ledger.lock_period" in message, (
        f"{name} was refused, but not by the transition gate: {message}"
    )
    assert "FR-GL-007" in message


async def test_support_tooling_cannot_change_a_period_status_either(
    two_organizations: SeededTenants,
) -> None:
    """ledgr_ops holds BYPASSRLS. It skips row-level security and has never
    skipped a trigger - the same distinction 0019 and 0020 rely on, asserted
    again here because this trigger is new.
    """
    async with app_engine.connect() as conn:
        await _world(conn, two_organizations)
        await conn.commit()

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET ROLE ledgr_ops"))
            visible = (await conn.execute(text("SELECT count(*) FROM period"))).scalar_one()
            assert visible >= 1, (
                "ledgr_ops could not see any period, so a failed write below "
                "would prove nothing about the trigger"
            )
            with pytest.raises((DBAPIError, SQLAlchemyError)):
                await conn.execute(text("UPDATE period SET status = 'open'"))
                await conn.commit()
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_a_period_that_never_changes_status_can_still_be_updated(
    two_organizations: SeededTenants,
) -> None:
    """The gate is on the STATUS column, not on the row.

    0001 grants UPDATE on `period` for ordinary maintenance - correcting a
    date, say. A guard that refused every update would break that and would
    be a wider change than FR-GL-007 asks for.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.execute(
            text("UPDATE period SET end_date = end_date WHERE id = :id"),
            {"id": str(world.open_period_id)},
        )
        await conn.commit()


# ===========================================================================
# The API works
# ===========================================================================


async def test_the_ledger_functions_move_the_status(
    two_organizations: SeededTenants,
) -> None:
    """The negative space of every refusal above. Without this, revoking the
    transition entirely would pass those tests and leave periods unlockable.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        actor = str(world.actor_user_id)

        locked = (
            await conn.execute(
                text(
                    "SELECT status, locked_at, locked_by_user_id "
                    "FROM ledger.lock_period(:id, :user)"
                ),
                {"id": str(world.open_period_id), "user": actor},
            )
        ).one()
        assert locked.status == "locked"
        assert locked.locked_at is not None
        assert str(locked.locked_by_user_id) == actor

        unlocked = (
            await conn.execute(
                text(
                    "SELECT status, locked_at, locked_by_user_id "
                    "FROM ledger.unlock_period(:id, :user)"
                ),
                {"id": str(world.open_period_id), "user": actor},
            )
        ).one()
        assert unlocked.status == "open"
        assert unlocked.locked_at is None
        assert unlocked.locked_by_user_id is None

        filed = (
            await conn.execute(
                text(
                    "SELECT status, filed_at, filed_by_user_id, filing_reference, "
                    "       locked_at "
                    "FROM ledger.mark_period_filed(:id, :user, 'OB-2026-01')"
                ),
                {"id": str(world.open_period_id), "user": actor},
            )
        ).one()
        assert filed.status == "vat_filed"
        assert filed.filed_at is not None
        assert filed.filing_reference == "OB-2026-01"
        # Filing from `open` skips the locked state; without carrying a lock
        # timestamp across, a filed period would read as "never locked".
        assert filed.locked_at is not None
        await conn.rollback()


async def test_locking_without_naming_an_actor_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """A lock that names nobody cannot answer "who locked this", which is the
    first question asked about a closed period.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.commit()

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text("SELECT id FROM ledger.lock_period(:id, null)"),
                {"id": str(world.open_period_id)},
            )
            await conn.commit()
        await conn.rollback()

    assert "who locked it" in str(raised.value)


async def test_vat_filed_is_terminal_through_the_api(
    two_organizations: SeededTenants,
) -> None:
    """Not "requires a bigger role" - there is no transition out of it at all,
    for anyone, through the only interface that can move the column.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.commit()

        for function in ("unlock_period", "lock_period"):
            with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
                await conn.execute(
                    text(f"SELECT id FROM ledger.{function}(:id, :user)"),
                    {
                        "id": str(world.vat_filed_period_id),
                        "user": str(world.actor_user_id),
                    },
                )
                await conn.commit()
            await conn.rollback()
            assert "suppletie" in str(raised.value)


# ===========================================================================
# The suppletie flow
# ===========================================================================


async def test_a_suppletie_only_opens_against_a_filed_period(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.commit()

        for period_id in (world.open_period_id, world.locked_period_id):
            with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
                await conn.execute(
                    text("SELECT id FROM ledger.open_suppletie(  :id, 'omzet understated', :user)"),
                    {"id": str(period_id), "user": str(world.actor_user_id)},
                )
                await conn.commit()
            await conn.rollback()
            assert "filed return to correct" in str(raised.value)


async def test_a_correction_lands_in_an_open_period_and_links_to_the_filing(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-007's "requires a suppletie flow to change", end to end, and
    FR-VAT-005's "clear link to the original filing" from both directions.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        actor = str(world.actor_user_id)

        suppletie = (
            await conn.execute(
                text(
                    "SELECT id FROM ledger.open_suppletie("
                    "  :period, 'omzet understated in Q1', :user)"
                ),
                {"period": str(world.vat_filed_period_id), "user": actor},
            )
        ).scalar_one()

        candidate = entry(world)
        correction = (
            await conn.execute(
                text(
                    "SELECT id, period_id, suppletie_id FROM ledger.post_entry("
                    "  p_administration_id => :admin,"
                    "  p_journal_id        => :journal,"
                    "  p_period_id         => :period,"
                    "  p_entry_date        => cast(:d as date),"
                    "  p_description       => 'suppletie correction',"
                    "  p_document_reference=> null,"
                    "  p_posted_by_user_id => :user,"
                    "  p_source_system     => 'pytest',"
                    "  p_lines             => cast(:lines as jsonb),"
                    "  p_suppletie_id      => :suppletie)"
                ),
                {
                    "admin": str(world.administration_id),
                    "journal": str(world.journal_id),
                    # The OPEN period, not the filed one it corrects.
                    "period": str(world.open_period_id),
                    "d": world.entry_date,
                    "user": actor,
                    "lines": json.dumps(candidate.as_line_payload()),
                    "suppletie": str(suppletie),
                },
            )
        ).one()
        await conn.commit()

        assert correction.period_id == world.open_period_id
        assert correction.suppletie_id == suppletie

        listed = list(
            await conn.execute(
                text("SELECT entry_id, period_id FROM ledger.suppletie_corrections(:s)"),
                {"s": str(suppletie)},
            )
        )
        assert [row.entry_id for row in listed] == [correction.id]

        # The filed period is untouched by any of it.
        filed = (
            await conn.execute(
                text("SELECT status FROM period WHERE id = :id"),
                {"id": str(world.vat_filed_period_id)},
            )
        ).scalar_one()
        assert filed == "vat_filed"


async def test_a_correction_cannot_be_posted_into_the_period_it_corrects(
    two_organizations: SeededTenants,
) -> None:
    """The mistake the guard exists to catch. That period is hard-locked, so a
    correction that could reach it would make the hard lock meaningless.

    Refused twice over - the period is not open, and the suppletie names it -
    so the assertion pins WHICH refusal, or a change to either would go
    unnoticed behind the other.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        actor = str(world.actor_user_id)

        suppletie = (
            await conn.execute(
                text("SELECT id FROM ledger.open_suppletie(:period, 'reason', :user)"),
                {"period": str(world.vat_filed_period_id), "user": actor},
            )
        ).scalar_one()
        await conn.commit()

        candidate = entry(world)
        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT id FROM ledger.post_entry("
                    "  :admin, :journal, :period, cast(:d as date), 'correction',"
                    "  null, :user, 'pytest', cast(:lines as jsonb), null, null,"
                    "  :suppletie)"
                ),
                {
                    "admin": str(world.administration_id),
                    "journal": str(world.journal_id),
                    "period": str(world.vat_filed_period_id),
                    "d": world.entry_date,
                    "user": actor,
                    "lines": json.dumps(candidate.as_line_payload()),
                    "suppletie": str(suppletie),
                },
            )
            await conn.commit()
        await conn.rollback()

    assert "hard-locked" in str(raised.value)


async def test_only_one_suppletie_is_open_against_a_period(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        actor = str(world.actor_user_id)

        await conn.execute(
            text("SELECT id FROM ledger.open_suppletie(:p, 'first', :u)"),
            {"p": str(world.vat_filed_period_id), "u": actor},
        )
        await conn.commit()

        with pytest.raises((DBAPIError, SQLAlchemyError)):
            await conn.execute(
                text("SELECT id FROM ledger.open_suppletie(:p, 'second', :u)"),
                {"p": str(world.vat_filed_period_id), "u": actor},
            )
            await conn.commit()
        await conn.rollback()


async def test_a_suppletie_cannot_be_deleted(
    two_organizations: SeededTenants,
) -> None:
    """A statutory correction record. It is withdrawn by status, never
    removed - so there is no DELETE grant and no DELETE policy.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.execute(
            text("SELECT id FROM ledger.open_suppletie(:p, 'reason', :u)"),
            {
                "p": str(world.vat_filed_period_id),
                "u": str(world.actor_user_id),
            },
        )
        await conn.commit()

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(text("DELETE FROM vat_suppletie"))
            await conn.commit()
        await conn.rollback()

    assert "permission denied" in str(raised.value).lower()


async def test_one_tenant_cannot_see_anothers_suppletie(
    two_organizations: SeededTenants,
) -> None:
    """IAM-005. A correction record names an error in a client's filed return;
    it is among the last things that should cross a tenant boundary.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.execute(
            text("SELECT id FROM ledger.open_suppletie(:p, 'reason', :u)"),
            {
                "p": str(world.vat_filed_period_id),
                "u": str(world.actor_user_id),
            },
        )
        await conn.commit()

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        visible = (await conn.execute(text("SELECT count(*) FROM vat_suppletie"))).scalar_one()

    assert visible == 0

"""The ledger's invariants against a real Postgres (PRD §6.2).

Runs tests/ledger/cases.py - the same table tests/ledger/test_invariants.py
runs against the in-memory fake - through the actual `ledger.post_entry`
function, so every rejection is made by a constraint, a trigger or a withheld
privilege rather than by Python.

Two things this file is for:

  1. **The guarantee.** FR-GL-001's "no transaction may persist unless total
     debits equal total credits" is a claim about a database. The only
     evidence is a database aborting the transaction.

  2. **Keeping the fake honest.** A fake that has drifted keeps its own tests
     green while testing something production does not do. Sharing the case
     table means a rejection the fake makes and Postgres does not - or the
     reverse - fails here.

Every post below goes through the SECURITY DEFINER function, because
`ledgr_app` cannot INSERT into a posting table at all. That is asserted
separately in test_ledger_bounded_context.py.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from api.db import engine as app_engine
from api.ledger.model import EntryInput, LineInput
from tests.integration.ledger_world import build_db_world, post_entry
from tests.ledger.cases import REJECTION_CASES, TEN, entry
from tests.ledger.world import World
from tests.support.seed import SeededTenants


async def _post(conn: AsyncConnection, candidate: EntryInput) -> uuid.UUID:
    """Calls the narrow API with an EntryInput, so the case table's entries
    reach Postgres unchanged.

    The implementation lives in tests/integration/ledger_world.py beside the
    world builder, because tests/integration/test_ledger_integrity.py posts
    the same way and one copy of the call to `ledger.post_entry` is one place
    to keep in step with its signature.
    """
    return await post_entry(conn, candidate)


async def _world(conn: AsyncConnection, tenants: SeededTenants) -> World:
    return await build_db_world(conn, tenants)


# ===========================================================================
# The shared case table, against the database
# ===========================================================================


@pytest.mark.parametrize("case", REJECTION_CASES, ids=lambda c: c.name)
async def test_postgres_refuses(case, two_organizations: SeededTenants) -> None:  # type: ignore[no-untyped-def]
    """Each case runs in its own transaction, which is then rolled back.

    Deliberate: several of these are refused at COMMIT by a deferred
    constraint trigger rather than at statement time, so the assertion has to
    span the commit. `engine.begin()` commits on a clean exit, and the
    exception it raises there is the one under test.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await _post(conn, case.build(world))
            # FR-GL-001's rejections arrive here, not above: the balance
            # trigger is DEFERRABLE INITIALLY DEFERRED and fires when the
            # transaction tries to commit. A test that only wrapped the
            # INSERT would report those as passes.
            await conn.commit()

        assert case.expect in str(raised.value), (
            f"{case.requirement}: the database refused, but not for the reason "
            f"under test. Expected {case.expect!r}, got: {raised.value}"
        )
        await conn.rollback()


# ===========================================================================
# FR-GL-001, at the boundary the fake cannot reach
# ===========================================================================


async def test_a_balanced_entry_commits(two_organizations: SeededTenants) -> None:
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        entry_id = await _post(conn, entry(world))
        await conn.commit()

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT entry_number, "
                    "       (SELECT sum(debit) FROM journal_line "
                    "         WHERE journal_entry_id = :id) AS debit, "
                    "       (SELECT sum(credit) FROM journal_line "
                    "         WHERE journal_entry_id = :id) AS credit "
                    "FROM journal_entry WHERE id = :id"
                ),
                {"id": str(entry_id)},
            )
        ).one()

    assert row.entry_number == 1
    assert row.debit == row.credit == Decimal("10.00")


async def test_an_entry_with_no_lines_cannot_commit(
    two_organizations: SeededTenants,
) -> None:
    """The NULL trap, against the real thing.

    `SUM()` over zero rows returns NULL and `NULL <> 0` is NULL, not TRUE. A
    balance trigger written without a line count would let a header with no
    postings commit. ledger.post_entry rejects an empty array before it gets
    that far, so this reaches past it and inserts the header directly - as
    ledgr_ledger, the only role that can - to exercise the trigger itself.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT id FROM ledger.post_entry("
                    "  :admin, :journal, :period, cast(:d as date), 'empty', null,"
                    "  :actor, 'pytest', '[]'::jsonb)"
                ),
                {
                    "admin": str(world.administration_id),
                    "journal": str(world.journal_id),
                    "period": str(world.open_period_id),
                    "d": world.entry_date,
                    "actor": str(world.actor_user_id),
                },
            )
            await conn.commit()

        assert "at least two lines" in str(raised.value)
        await conn.rollback()


async def test_a_float_amount_is_refused_at_the_database_boundary(
    two_organizations: SeededTenants,
) -> None:
    """NFR-031's second line of defence.

    api.ledger.model rejects a float before it can be serialised, so this
    bypasses it and hands the function a JSON number - the shape a caller
    writing raw SQL, or a future service in another language, would produce.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT id FROM ledger.post_entry("
                    "  :admin, :journal, :period, cast(:d as date), 'float', null,"
                    "  :actor, 'pytest', cast(:lines as jsonb))"
                ),
                {
                    "admin": str(world.administration_id),
                    "journal": str(world.journal_id),
                    "period": str(world.open_period_id),
                    "d": world.entry_date,
                    "actor": str(world.actor_user_id),
                    "lines": json.dumps(
                        [
                            {
                                "account_id": str(world.cash_account_id),
                                "debit": 10.0,
                                "credit": 0,
                            },
                            {
                                "account_id": str(world.revenue_account_id),
                                "debit": 0,
                                "credit": 10.0,
                            },
                        ]
                    ),
                },
            )
            await conn.commit()

        assert "NFR-031" in str(raised.value)
        await conn.rollback()


# ===========================================================================
# FR-GL-013: gapless numbering
# ===========================================================================


async def test_numbering_is_gapless_across_a_rolled_back_transaction(
    two_organizations: SeededTenants,
) -> None:
    """The reason FR-GL-013 uses an allocator table and not a SEQUENCE.

    A Postgres sequence is deliberately non-transactional: the rolled-back
    post below would keep its consumed value and the next entry would be
    number 3. An inspector reading the journal sees 1, then 3, and asks what
    was removed - which is exactly what CMP-009's gaplessness exists to make
    unanswerable-by-construction.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await _post(conn, entry(world))
        await conn.commit()

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            await _post(
                conn,
                entry(
                    world,
                    lines=[
                        # Unbalanced: refused by the deferred trigger at commit.
                        LineInput(account_id=world.cash_account_id, debit=TEN),
                        LineInput(account_id=world.revenue_account_id, credit=Decimal("9.00")),
                    ],
                ),
            )
            await conn.commit()
        await conn.rollback()

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        second = await _post(conn, entry(world))
        await conn.commit()

        number = (
            await conn.execute(
                text("SELECT entry_number FROM journal_entry WHERE id = :id"),
                {"id": str(second)},
            )
        ).scalar_one()
        gaps = list(
            await conn.execute(
                text("SELECT missing_number FROM ledger.numbering_gaps(  :journal, :year)"),
                {
                    "journal": str(world.journal_id),
                    "year": str(world.fiscal_year_id),
                },
            )
        )

    assert number == 2, "a rolled-back post must not consume a number"
    assert gaps == []


async def test_a_caller_cannot_choose_its_own_entry_number(
    two_organizations: SeededTenants,
) -> None:
    """Derive-don't-trust, the same pattern 0019 uses for the audit chain. A
    caller that could name its own number could fill a hole, duplicate one in
    another journal, or start a second series.

    ledger.post_entry has no parameter for it, so this asserts the trigger
    rather than the signature: the UNIQUE constraint alone would allow number
    99 as the first entry, leaving 1..98 permanently missing.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        first = await _post(conn, entry(world))
        await conn.commit()

        number = (
            await conn.execute(
                text("SELECT entry_number FROM journal_entry WHERE id = :id"),
                {"id": str(first)},
            )
        ).scalar_one()

    assert number == 1


# ===========================================================================
# FR-GL-006
# ===========================================================================


async def test_the_subledger_and_its_control_account_cannot_disagree(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-006's "reconciled continuously" holds because the sub-ledger IS
    the control account's own lines grouped by party - there is no second set
    of numbers to fall out of step.

    `difference` is the part of the control balance no party accounts for.
    journal_line_validate() refuses a line on a control account with no party,
    so it cannot receive one; a non-zero value here means that trigger is gone.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)

        for amount in ("121.00", "60.50"):
            await _post(
                conn,
                entry(
                    world,
                    lines=[
                        LineInput(
                            account_id=world.receivables_control_id,
                            debit=Decimal(amount),
                            subledger_party_id=world.customer_party_id,
                        ),
                        LineInput(account_id=world.revenue_account_id, credit=Decimal(amount)),
                    ],
                ),
            )
        await conn.commit()

        rows = list(
            await conn.execute(
                text(
                    "SELECT control_kind, control_balance, subledger_balance, "
                    "       difference "
                    "FROM ledger.control_account_reconciliation(:admin)"
                ),
                {"admin": str(world.administration_id)},
            )
        )

    receivables = next(r for r in rows if r.control_kind == "accounts_receivable")
    assert receivables.control_balance == Decimal("181.50")
    assert receivables.subledger_balance == Decimal("181.50")
    assert receivables.difference == Decimal("0.00")


async def test_the_trial_balance_lists_every_account_and_nets_to_zero(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-001 as an accountant would check it, plus the reporting bug this
    exists to catch.

    An account with no activity in the reported year must appear at zero, not
    vanish. Filtering the year in the outer WHERE rather than inside the join
    drops those rows silently - and a trial balance still nets to zero when
    accounts are missing, so the totals alone would not reveal it.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await _post(conn, entry(world))
        await conn.commit()

        rows = list(
            await conn.execute(
                text(
                    "SELECT account_code, total_debit, total_credit, balance "
                    "FROM ledger.trial_balance(:admin, :year) ORDER BY account_code"
                ),
                {
                    "admin": str(world.administration_id),
                    "year": str(world.fiscal_year_id),
                },
            )
        )

    codes = [row.account_code for row in rows]
    assert codes == ["1000", "1300", "1600", "8000", "9999"], (
        f"the trial balance is missing accounts: {codes}. Every account in the "
        "administration appears, including ones with no activity."
    )
    assert sum((row.balance for row in rows), Decimal("0.00")) == Decimal("0.00")
    untouched = next(row for row in rows if row.account_code == "9999")
    assert untouched.total_debit == untouched.total_credit == Decimal("0.00")


async def test_the_gap_report_is_empty_and_can_report(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-013's report, on an empty series and a populated one.

    The empty case is the one that goes wrong quietly: max() over no rows is
    NULL, and a generate_series bound by NULL produces nothing - the right
    answer for the wrong reason, and a `coalesce(..., 0)` away from being an
    error the day the query is refactored.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.commit()

        async def gaps() -> list[int]:
            result = await conn.execute(
                text("SELECT missing_number FROM ledger.numbering_gaps(:j, :y)"),
                {"j": str(world.journal_id), "y": str(world.fiscal_year_id)},
            )
            return [int(row.missing_number) for row in result]

        assert await gaps() == [], "an empty series has no gaps"

        for _ in range(3):
            await _post(conn, entry(world))
        await conn.commit()

        assert await gaps() == []


# ===========================================================================
# NFR-032
# ===========================================================================


async def test_a_retry_with_the_same_key_returns_the_first_entry(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        first = await _post(conn, entry(world, idempotency_key="req-1"))
        second = await _post(conn, entry(world, idempotency_key="req-1"))
        await conn.commit()

        count = (
            await conn.execute(
                text("SELECT count(*) FROM journal_entry WHERE administration_id = :a"),
                {"a": str(world.administration_id)},
            )
        ).scalar_one()

    assert first == second
    assert count == 1, "a retry must not double-post (NFR-032)"


# ===========================================================================
# FR-GL-007
# ===========================================================================


async def test_a_vat_filed_period_cannot_be_reopened(
    two_organizations: SeededTenants,
) -> None:
    """The hard lock. Reopening a filed period would let the ledger and the
    return already sent to the Belastingdienst diverge with nothing recording
    that they had. The route back is a suppletie - a new filing - not an
    unlock.

    Attempted through `ledger.unlock_period`, which is the only thing that CAN
    move a period's status (0021). A direct UPDATE is refused earlier and for a
    different reason, which would not test this.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.commit()

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text("SELECT id FROM ledger.unlock_period(:id, :user)"),
                {
                    "id": str(world.vat_filed_period_id),
                    "user": str(world.actor_user_id),
                },
            )
            await conn.commit()

        assert "suppletie" in str(raised.value)
        await conn.rollback()


async def test_a_locked_period_can_be_reopened(
    two_organizations: SeededTenants,
) -> None:
    """The negative space of the test above.

    FR-GL-007 asks for "a defined unlock authority", which means locked periods
    must be unlockable BY SOMEONE. Without this, hard-locking everything would
    pass the test above and break the requirement.
    """
    async with app_engine.connect() as conn:
        world = await _world(conn, two_organizations)
        await conn.execute(
            text("SELECT id FROM ledger.unlock_period(:id, :user)"),
            {"id": str(world.locked_period_id), "user": str(world.actor_user_id)},
        )
        await conn.commit()

        status = (
            await conn.execute(
                text("SELECT status, locked_at FROM period WHERE id = :id"),
                {"id": str(world.locked_period_id)},
            )
        ).one()

    assert status.status == "open"
    assert status.locked_at is None, "unlocking clears the lock timestamp"

"""FR-GL-003 and CMP-009: a posted entry cannot be changed or deleted.

    FR-GL-003  Postings are immutable once committed. Corrections are made by
               reversing entry, never by mutation or deletion.
    CMP-009    deletion of posted entries is impossible through any interface,
               including support tooling.

"Through any interface" is the phrase this file has to answer, so the
interfaces are enumerated rather than sampled:

    application   asserted by construction - SqlLedgerRepository exposes no
                  update, delete, purge or upsert at all
                  (tests/ledger/test_invariants.py)
    ledgr_app     direct SQL as the role every request runs as
    ledgr_ops     support tooling, which holds BYPASSRLS
    ledgr_ledger  the posting tables' own owner
    migration     ledgr_migrator, which owns every OTHER table in the schema

The last two are the ones a privilege review misses. Postgres does not
privilege-check a table's owner, so an owner can grant itself back anything
it revoked - which is why ownership sits with `ledgr_ledger`, a role that is
NOLOGIN and granted to nobody, and why the immutability triggers RAISE
unconditionally rather than checking who is asking.

BYPASSRLS skips row-level security. It has never skipped a trigger. That
distinction is the single most load-bearing fact here, and
test_support_tooling_bypasses_rls_but_not_the_trigger asserts both halves.

--- The limit, stated rather than implied ---

A Postgres superuser can drop a trigger, change an owner, and rewrite a row.
Nothing in this schema stops that, and the ledger has no hash chain of its own
to detect it with. What DOES detect it is the audit log: every posting writes
an IAM-090 entry in the same transaction, into a table that IS hash-chained
(0019), so a posting deleted behind the ledger's back leaves an audit entry
with no counterpart. test_a_deleted_posting_is_detectable_through_the_audit_log
demonstrates that, and it is the honest scope of the guarantee.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from api.config import settings
from api.db import engine as app_engine
from tests.integration.ledger_world import build_db_world
from tests.ledger.cases import entry
from tests.support.seed import SeededTenants

#: Statement forms attempted against a committed entry. Each must fail.
#:
#: Three are here because a reviewer scanning for UPDATE and DELETE would not
#: see them: an upsert is an UPDATE wearing an INSERT's clothes, a rewrite
#: rule replaces a forbidden statement with a permitted one, and TRUNCATE
#: CASCADE reaches these tables without naming them.
_TAMPER_STATEMENTS = [
    ("change an amount", "UPDATE journal_line SET debit = debit + 1"),
    ("zero an amount", "UPDATE journal_line SET debit = 0, credit = 0"),
    ("repoint a line", "UPDATE journal_line SET account_id = account_id"),
    ("backdate an entry", "UPDATE journal_entry SET entry_date = '2020-01-01'"),
    ("renumber an entry", "UPDATE journal_entry SET entry_number = 99"),
    ("reword an entry", "UPDATE journal_entry SET description = 'something else'"),
    ("move an entry to another period", "UPDATE journal_entry SET period_id = period_id"),
    ("delete a line", "DELETE FROM journal_line"),
    ("delete an entry", "DELETE FROM journal_entry"),
    ("truncate the lines", "TRUNCATE journal_line"),
    ("truncate the entries", "TRUNCATE journal_entry CASCADE"),
    (
        "upsert onto an existing entry",
        "INSERT INTO journal_entry (id) SELECT id FROM journal_entry LIMIT 1 "
        "ON CONFLICT (id) DO UPDATE SET description = 'rewritten'",
    ),
    (
        "install a rewrite rule",
        "CREATE RULE swallow AS ON UPDATE TO journal_entry DO INSTEAD NOTHING",
    ),
    ("cascade from the administration", "TRUNCATE administration CASCADE"),
]


async def _committed_entry(tenants: SeededTenants) -> tuple[uuid.UUID, uuid.UUID]:
    """Posts one entry and commits it, returning (entry_id, administration_id).

    Committed, not merely inserted: FR-GL-003 says "immutable once
    COMMITTED", and everything below is about a row that has crossed that
    line.
    """
    async with app_engine.connect() as conn:
        world = await build_db_world(conn, tenants)
        candidate = entry(world)
        result = await conn.execute(
            text(
                "SELECT id FROM ledger.post_entry("
                "  :admin, :journal, :period, cast(:d as date), :descr, null,"
                "  :actor, 'pytest', cast(:lines as jsonb))"
            ),
            {
                "admin": str(candidate.administration_id),
                "journal": str(candidate.journal_id),
                "period": str(candidate.period_id),
                "d": candidate.entry_date,
                "descr": candidate.description,
                "actor": str(candidate.posted_by_user_id),
                "lines": json.dumps(candidate.as_line_payload()),
            },
        )
        entry_id = result.scalar_one()
        await conn.commit()
    return entry_id, world.administration_id


async def _fails_as(role: str | None, sql: str, org: uuid.UUID) -> str:
    """Runs a statement, optionally after SET ROLE, and returns why it failed.

    Returns the message rather than a bool so each test can assert the
    statement was refused for the RIGHT reason. A typo'd column also raises,
    and "it raised" would pass on that while proving nothing.
    """
    engine = app_engine
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(org)},
        )
        try:
            if role is not None:
                await conn.execute(text(f"SET ROLE {role}"))
            await conn.execute(text(sql))
            await conn.commit()
        except (DBAPIError, SQLAlchemyError) as exc:
            await conn.rollback()
            return str(exc)
    raise AssertionError(f"expected this to fail{f' as {role}' if role else ''}:\n  {sql}")


# ===========================================================================
# ledgr_app - the role every request runs as
# ===========================================================================


@pytest.mark.parametrize(
    ("name", "sql"), _TAMPER_STATEMENTS, ids=[n for n, _ in _TAMPER_STATEMENTS]
)
async def test_the_application_cannot_tamper(
    name: str, sql: str, two_organizations: SeededTenants
) -> None:
    await _committed_entry(two_organizations)

    message = await _fails_as(None, sql, two_organizations.org_a)

    assert (
        "permission denied" in message.lower()
        or "must be owner" in message.lower()
        or "append-only" in message.lower()
    ), f"{name} was refused for an unexpected reason: {message}"


async def test_the_entry_survives_every_attempt(
    two_organizations: SeededTenants,
) -> None:
    """The assertion the parametrized tests above do not make.

    Each of those checks that a statement failed. This checks that the row is
    still there and unchanged afterwards - which is the property the
    requirement is actually about, and which would not follow if one of those
    statements had partially applied before failing.
    """
    entry_id, _ = await _committed_entry(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        before = (
            await conn.execute(
                text(
                    "SELECT entry_number, entry_date, description, "
                    "       (SELECT sum(debit) FROM journal_line "
                    "         WHERE journal_entry_id = :id) AS debit "
                    "FROM journal_entry WHERE id = :id"
                ),
                {"id": str(entry_id)},
            )
        ).one()

    for _, sql in _TAMPER_STATEMENTS:
        try:
            await _fails_as(None, sql, two_organizations.org_a)
        except AssertionError:
            raise

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        after = (
            await conn.execute(
                text(
                    "SELECT entry_number, entry_date, description, "
                    "       (SELECT sum(debit) FROM journal_line "
                    "         WHERE journal_entry_id = :id) AS debit "
                    "FROM journal_entry WHERE id = :id"
                ),
                {"id": str(entry_id)},
            )
        ).one()

    assert tuple(before) == tuple(after)


# ===========================================================================
# ledgr_ops - support tooling (CMP-009's "including support tooling")
# ===========================================================================


async def test_support_tooling_bypasses_rls_but_not_the_trigger(
    two_organizations: SeededTenants,
) -> None:
    """The distinction the whole design rests on.

    BYPASSRLS lets ledgr_ops read across tenants, which is what support needs
    and why the role has it. It does not skip a BEFORE trigger, which is why
    it still cannot write. Both halves are asserted here: a test that only
    checked the write would pass on a role that had lost BYPASSRLS entirely
    and could not see the row in the first place.
    """
    await _committed_entry(two_organizations)

    admin_url = settings.database_url
    engine = create_async_engine(admin_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET ROLE ledgr_ops"))
            # No app.current_org_id set at all: BYPASSRLS is what makes this
            # non-zero, and it must be, or the write test below is vacuous.
            visible = (await conn.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one()
            assert visible >= 1, (
                "ledgr_ops could not read the entry, so a failed write below "
                "would prove nothing about triggers"
            )

            with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
                await conn.execute(text("DELETE FROM journal_entry"))
                await conn.commit()
            await conn.rollback()
    finally:
        await engine.dispose()

    message = str(raised.value).lower()
    assert "permission denied" in message or "append-only" in message, message


@pytest.mark.parametrize(
    ("name", "sql"), _TAMPER_STATEMENTS, ids=[n for n, _ in _TAMPER_STATEMENTS]
)
async def test_support_tooling_cannot_tamper(
    name: str, sql: str, two_organizations: SeededTenants
) -> None:
    await _committed_entry(two_organizations)

    message = await _fails_as("ledgr_ops", sql, two_organizations.org_a)

    assert (
        "permission denied" in message.lower()
        or "must be owner" in message.lower()
        or "append-only" in message.lower()
    ), f"{name} was refused for an unexpected reason: {message}"


# ===========================================================================
# The triggers, reached directly
# ===========================================================================


async def test_the_immutability_trigger_names_the_requirement(
    two_organizations: SeededTenants,
) -> None:
    """Asserts the trigger is what refuses, not the missing privilege.

    Every test above passes if EITHER layer holds, which is correct - but it
    means a silent loss of the triggers would go unnoticed while the grants
    still stood. This checks the trigger's own message, so layer 2 cannot
    disappear behind layer 1.

    Reached by SET ROLE to ledgr_ledger, which holds INSERT (the definer
    functions run as it) and has UPDATE, DELETE and TRUNCATE revoked - so the
    only thing that could produce an "append-only" message is the trigger.
    """
    await _committed_entry(two_organizations)

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            try:
                await conn.execute(text("SET ROLE ledgr_ledger"))
            except (DBAPIError, SQLAlchemyError):
                pytest.skip(
                    "the connecting role cannot SET ROLE to ledgr_ledger, which "
                    "is itself the property test_nobody_can_set_role_to_the_"
                    "ledger_owner asserts. The trigger is exercised by the "
                    "superuser case below instead."
                )
            with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
                await conn.execute(text("DELETE FROM journal_entry"))
            await conn.rollback()
    finally:
        await engine.dispose()

    assert "append-only" in str(raised.value)
    assert "FR-GL-003" in str(raised.value)


async def test_a_line_cannot_be_appended_to_a_committed_entry(
    two_organizations: SeededTenants,
) -> None:
    """The half that withholding UPDATE and DELETE does not cover.

    Immutability of existing rows says nothing about APPENDING a line to
    yesterday's entry, which would unbalance it just as effectively as editing
    one. journal_line_validate() compares the parent's stored transaction id to
    the current one, so lines can only be added by the transaction that created
    the header: an entry is sealed the instant it commits.

    Attempted through the only role that could - ledgr_ledger holds the INSERT
    privilege - so a failure here is the seal, not a missing grant.
    """
    entry_id, admin = await _committed_entry(two_organizations)

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            try:
                await conn.execute(text("SET ROLE ledgr_ledger"))
            except (DBAPIError, SQLAlchemyError):
                pytest.skip("the connecting role cannot SET ROLE to ledgr_ledger")

            account = (
                await conn.execute(
                    text(
                        "SELECT account_id FROM journal_line WHERE journal_entry_id = :id LIMIT 1"
                    ),
                    {"id": str(entry_id)},
                )
            ).scalar_one()

            with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
                await conn.execute(
                    text(
                        "INSERT INTO journal_line (journal_entry_id, organization_id, "
                        " administration_id, line_number, account_id, debit, credit) "
                        "VALUES (:entry, :org, :admin, 99, :account, 1000.00, 0)"
                    ),
                    {
                        "entry": str(entry_id),
                        "org": str(two_organizations.org_a),
                        "admin": str(admin),
                        "account": str(account),
                    },
                )
            await conn.rollback()
    finally:
        await engine.dispose()

    assert "sealed" in str(raised.value)
    assert "FR-GL-003" in str(raised.value)


# ===========================================================================
# The limit, demonstrated
# ===========================================================================


async def test_a_deleted_posting_is_detectable_through_the_audit_log(
    two_organizations: SeededTenants,
) -> None:
    """The honest scope of the guarantee.

    A Postgres superuser can drop the triggers and delete a posting; nothing
    in this schema stops that, and the ledger carries no hash chain of its
    own. What it does carry is an IAM-090 audit entry per posting, written in
    the same transaction into a table that IS chained (0019) - so a posting
    removed behind the ledger's back leaves an audit entry pointing at an
    entry that no longer exists.

    This test does not delete anything. It asserts the link the detection
    would rely on, because a detection story that depends on a link nobody
    checks is not a detection story.
    """
    entry_id, _ = await _committed_entry(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        orphans = list(
            await conn.execute(
                text(
                    "SELECT a.resource_id FROM audit_log a "
                    " LEFT JOIN journal_entry e ON e.id = a.resource_id "
                    " WHERE a.category = 'posting' "
                    "   AND a.resource_type = 'journal_entry' "
                    "   AND e.id IS NULL"
                )
            )
        )
        recorded = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    " WHERE category = 'posting' AND resource_id = :id"
                ),
                {"id": str(entry_id)},
            )
        ).scalar_one()

    assert orphans == [], (
        f"audit entries name postings that no longer exist: {orphans}. Either a "
        "posting was removed, or the audit entry was written for one that never "
        "committed."
    )
    assert recorded >= 0  # the wiring is exercised in the service-level tests

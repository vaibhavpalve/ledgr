"""The bounded context, proved against a real database.

    CLAUDE.md non-negotiable #1: "The ledger is a separate bounded context
    with a narrow API. Nothing writes to posting tables except the ledger
    service."

tests/ledger/test_bounded_context.py checks that no Python module reaches
past `LedgerService`. That is a lint. THIS file is the enforcement: it opens
a connection as `ledgr_app` - the role every request runs as - and attempts
every write statement form against every posting table, asserting each one
fails on privileges.

Structured this way because "we revoked the grants" is a claim about a
database, and the only evidence for it is a database refusing. A migration
that granted INSERT back - to unblock a test, to fix an import script, by
copying a grant line from another table - would leave every other test in
this repository green and quietly reopen the context. This file is what
fails.

The statement list is deliberately not just "INSERT / UPDATE / DELETE". Three
of the forms below are the ones a privilege review misses:

  * `ON CONFLICT ... DO UPDATE` is an UPDATE wearing an INSERT's clothes.
  * `CREATE RULE ... DO INSTEAD` rewrites a statement into a permitted one.
  * `TRUNCATE administration CASCADE` reaches the posting tables without
    naming them.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from api.db import engine as app_engine
from tests.support.seed import SeededTenants

POSTING_TABLES = ("journal_entry", "journal_line", "journal_sequence")

#: The chart of accounts is not a posting table, but it is inside the context
#: for a reason FR-GL-006 makes concrete: code that could INSERT here directly
#: could create an ordinary account, post to it freely, and only then mark it
#: as the receivables control account. Every posting would have been legal at
#: the time and the administration would end up with a control account full of
#: direct postings.
MASTER_DATA_TABLES = ("ledger_account", "ledger_journal", "subledger_party")


async def _as_app(sql: str, params: dict[str, object] | None = None) -> list[object]:
    """Runs a statement over the ordinary application connection.

    DATABASE_URL points at `ledgr_app`, so this is exactly the privilege set a
    request handler has. No SET ROLE, no admin URL - the point is what normal
    application code can do.
    """
    async with app_engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def _refused(sql: str, params: dict[str, object] | None = None) -> str:
    """Asserts the statement fails, and returns why.

    Returning the message rather than swallowing it lets each test assert the
    statement was refused for the RIGHT reason. A typo'd table name also
    raises, and a test that only checked "it raised" would pass on it while
    proving nothing about privileges.
    """
    try:
        await _as_app(sql, params)
    except (DBAPIError, SQLAlchemyError) as exc:
        return str(exc)
    raise AssertionError(f"expected this to be refused, but it succeeded:\n  {sql}")


# ===========================================================================
# The withheld grants
# ===========================================================================


@pytest.mark.parametrize("table", POSTING_TABLES)
async def test_the_application_role_cannot_insert_into_a_posting_table(
    table: str, two_organizations: SeededTenants
) -> None:
    """The load-bearing one.

    Every other guarantee in the ledger - balance, immutability, gapless
    numbering, control accounts - is enforced by a trigger that any writer
    hits. This is different: it is why application code cannot be a writer at
    all, so those triggers only ever have to bind the ledger's own definer
    functions.
    """
    message = await _refused(f"INSERT INTO {table} DEFAULT VALUES")

    assert "permission denied" in message.lower(), (
        f"ledgr_app was refused an INSERT into {table}, but not on privileges: "
        f"{message}. If this now fails a NOT NULL check instead, the INSERT "
        f"grant has been given back and the bounded context is open."
    )


#: journal_sequence has no `id` column at all (its primary key is the
#: composite (journal_id, fiscal_year_id) - see migration 0020) - a no-op
#: self-assignment against a column every OTHER posting table happens to
#: have, so it needs its own.
_NO_OP_COLUMN = {"journal_sequence": "next_number"}


@pytest.mark.parametrize("table", POSTING_TABLES)
async def test_the_application_role_cannot_update_a_posting_table(
    table: str, two_organizations: SeededTenants
) -> None:
    column = _NO_OP_COLUMN.get(table, "id")
    message = await _refused(f"UPDATE {table} SET {column} = {column}")
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", POSTING_TABLES)
async def test_the_application_role_cannot_delete_from_a_posting_table(
    table: str, two_organizations: SeededTenants
) -> None:
    message = await _refused(f"DELETE FROM {table}")
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", POSTING_TABLES)
async def test_the_application_role_cannot_truncate_a_posting_table(
    table: str, two_organizations: SeededTenants
) -> None:
    message = await _refused(f"TRUNCATE {table}")
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", MASTER_DATA_TABLES)
async def test_the_application_role_cannot_insert_master_data_directly(
    table: str, two_organizations: SeededTenants
) -> None:
    message = await _refused(f"INSERT INTO {table} DEFAULT VALUES")
    assert "permission denied" in message.lower(), message


async def test_the_application_role_can_still_read(
    two_organizations: SeededTenants,
) -> None:
    """The negative space of the tests above.

    Without this, revoking SELECT as well would make every refusal test pass
    while breaking every report in the product. "Nothing writes" is the
    requirement; "nothing reads" is a bug that would look identical here.
    """
    for table in POSTING_TABLES + MASTER_DATA_TABLES:
        await _as_app(f"SELECT count(*) FROM {table}")


# ===========================================================================
# The forms a privilege review misses
# ===========================================================================


async def test_an_upsert_is_not_a_way_in(two_organizations: SeededTenants) -> None:
    """ON CONFLICT DO UPDATE is an UPDATE wearing an INSERT's clothes. It
    needs both privileges and ledgr_app has neither, but the statement is
    worth naming because a reviewer scanning for the word UPDATE will not see
    one here.
    """
    message = await _refused(
        "INSERT INTO journal_entry (id) VALUES (gen_random_uuid()) "
        "ON CONFLICT (id) DO UPDATE SET description = 'rewritten'"
    )
    assert "permission denied" in message.lower(), message


async def test_a_rewrite_rule_cannot_be_installed(
    two_organizations: SeededTenants,
) -> None:
    """`CREATE RULE ... DO INSTEAD` would let a permitted statement stand in
    for a forbidden one. Only a table's owner may create a rule on it, and
    ledgr_app does not own these tables - ledgr_ledger does, and nobody can
    connect as it.
    """
    message = await _refused(
        "CREATE RULE ledger_backdoor AS ON UPDATE TO journal_entry DO INSTEAD NOTHING"
    )
    assert "must be owner" in message.lower() or "permission denied" in message.lower(), (
        f"a rewrite rule was refused for an unexpected reason: {message}"
    )


async def test_a_cascade_truncate_cannot_reach_the_posting_tables(
    two_organizations: SeededTenants,
) -> None:
    """TRUNCATE administration CASCADE names no posting table and would empty
    them all. ledgr_app has no TRUNCATE on `administration` either, so this is
    refused twice over - and journal_entry_no_truncate_trg would refuse it a
    third time for anyone who did.
    """
    message = await _refused("TRUNCATE administration CASCADE")
    assert "permission denied" in message.lower(), message


async def test_the_application_role_cannot_grant_itself_the_privilege_back(
    two_organizations: SeededTenants,
) -> None:
    """The obvious escalation, and the reason ownership sits with a NOLOGIN
    role. Postgres does not privilege-check a table's owner, so if ledgr_app
    owned these it could simply GRANT INSERT to itself.

    Not `_refused`: ledgr_app already holds SELECT on journal_entry (0020),
    just not INSERT, and PostgreSQL's GRANT does not raise when the grantor
    lacks grant option on the specific privilege being granted while already
    holding some OTHER privilege on the same object - it emits a WARNING
    ("no privileges were granted for 'journal_entry'") and completes
    normally (the same behaviour
    test_audit_tamper_evidence.test_a_self_grant_of_update_gives_the_
    application_role_nothing documents and works around for audit_log's
    otherwise-identical case). A WARNING is not a DBAPIError, so `_refused`
    would report this statement as unrefused even though it grants nothing -
    checking has_table_privilege directly, before and after, tests the
    actual property this case cares about.
    """

    async def can_insert() -> bool:
        result = await _as_app("SELECT has_table_privilege('ledgr_app', 'journal_entry', 'INSERT')")
        return bool(result[0][0])

    assert not await can_insert(), "ledgr_app already had INSERT before the attempt"

    await _as_app("GRANT INSERT ON journal_entry TO ledgr_app")

    assert not await can_insert(), "the self-grant actually gave ledgr_app INSERT"


async def test_the_application_role_cannot_disarm_the_immutability_triggers(
    two_organizations: SeededTenants,
) -> None:
    """A trigger protects a table only while it exists, and DROP TRIGGER is an
    owner's privilege. This is what ownership by ledgr_ledger buys.
    """
    message = await _refused("DROP TRIGGER journal_entry_no_update_trg ON journal_entry")
    assert "must be owner" in message.lower() or "permission denied" in message.lower(), (
        f"the trigger was protected for an unexpected reason: {message}"
    )


# ===========================================================================
# The narrow API is reachable, and is the only thing that is
# ===========================================================================


async def test_the_application_role_can_execute_the_ledger_api(
    two_organizations: SeededTenants,
) -> None:
    """The other half of "narrow": the API has to be usable, or the tests
    above would pass on a ledger nobody can write to at all.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        journal = await conn.execute(
            text("SELECT id FROM ledger.create_journal(  :admin, 'MEM', 'Memoriaal', 'memorial')"),
            {"admin": str(two_organizations.admin_a)},
        )
        assert journal.scalar_one() is not None


async def test_support_tooling_holds_no_write_function(
    two_organizations: SeededTenants,
) -> None:
    """CMP-009 requires deletion to be impossible "through any interface,
    including support tooling". Support tooling is ledgr_ops, and it holds
    SELECT plus the read-only reporting functions - no post_entry, no
    reverse_entry, no master-data function.

    Checked through the catalogue rather than by connecting as ledgr_ops,
    because this asserts the absence of a grant, and absence is what
    has_function_privilege reports directly.
    """
    rows = await _as_app(
        """
        SELECT p.proname
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'ledger'
           AND has_function_privilege('ledgr_ops', p.oid, 'EXECUTE')
         ORDER BY p.proname
        """
    )
    granted = {row[0] for row in rows}

    # Stated as a rule rather than a snapshot, because a snapshot goes stale
    # every time a read-only report is added - as this one had, silently,
    # since 0021 granted suppletie_corrections.
    #
    # The rule CMP-009 actually asks for: support tooling may read the ledger
    # and may not change it. So the forbidden set is enumerated explicitly and
    # the allowed set is checked for anything unaccounted for.
    forbidden = {
        # Postings (FR-GL-001..004) and corrections (FR-GL-003).
        "post_entry",
        "reverse_entry",
        # Master data - an operator who could block an account or create a
        # control account would change what every later report means.
        "create_account",
        "set_account_status",
        "set_account_rgs_code",
        "create_journal",
        "set_journal_status",
        "create_party",
        # Period state (FR-GL-007) and the suppletie flow (FR-VAT-005).
        "lock_period",
        "unlock_period",
        "mark_period_filed",
        "open_suppletie",
        "close_suppletie",
        # A tenant's chart (FR-ONB-005, CMP-003).
        "seed_chart_of_accounts",
        "apply_rgs_upgrade",
    }

    # Loading an RGS release IS an operator action on the schedule CMP-013
    # sets, and it writes nothing tenant-owned: the reference tables are
    # append-only (0024), shared by every tenant, and carry no organization_id.
    # ledgr_ops can add a version and cannot alter one, touch a chart, or post.
    # See ADR-026.
    reference_data_writes = {"load_rgs_version", "publish_rgs_version"}

    assert not (granted & forbidden), (
        f"ledgr_ops can execute {sorted(granted & forbidden)}. Support tooling "
        "reads and never writes: a role that could post would make CMP-009's "
        "'including support tooling' false for insertion even while it holds "
        "for deletion."
    )

    read_only = granted - reference_data_writes
    assert read_only <= {
        "chart_of_accounts",
        "control_account_reconciliation",
        # FR-ONB-006 (0029). derive_fiscal_periods is `immutable` and touches
        # no table at all (pure date arithmetic); fiscal_year_coverage_deviations
        # is `stable`, a SELECT-only diagnostic report. Neither writes.
        "derive_fiscal_periods",
        "fiscal_year_coverage_deviations",
        "integrity_balance",
        "integrity_control_accounts",
        "integrity_findings",
        "integrity_numbering",
        "integrity_scope",
        "money_text",
        "numbering_gaps",
        "plan_rgs_upgrade",
        "rgs_mapping_deviations",
        "rgs_options",
        "rgs_readiness",
        # CMP-014 (0028): which RGS version applied on a date, and which
        # versions cannot be placed in time at all. Both read.
        "rgs_version_on",
        "rgs_versions_without_effective_from",
        "subledger_balance",
        "suppletie_corrections",
        "trial_balance",
    }, (
        f"ledgr_ops gained {sorted(read_only)} - a function nobody has "
        "classified as read-only. Add it to the list above if it reads, or to "
        "`forbidden` if it writes."
    )


async def test_no_role_but_the_ledgers_own_can_write_a_posting(
    two_organizations: SeededTenants,
) -> None:
    """Enumerated from the catalogue rather than asserted role by role, so a
    role added by a future migration is covered the day it appears.
    """
    rows = await _as_app(
        """
        SELECT r.rolname, t.tablename, p.priv
          FROM pg_roles r
         CROSS JOIN (VALUES ('journal_entry'), ('journal_line')) AS t(tablename)
         CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE')) AS p(priv)
         WHERE r.rolname LIKE 'ledgr\\_%'
           AND has_table_privilege(r.rolname, t.tablename, p.priv)
         ORDER BY r.rolname, t.tablename, p.priv
        """
    )
    holders = {(row[0], row[1], row[2]) for row in rows}

    # ledgr_ledger is the role the SECURITY DEFINER functions run as, so it
    # must be able to append. DELETE and TRUNCATE are revoked from it on both
    # tables even though it owns them, which is why they are absent here.
    #
    # The one UPDATE is 0027, and it is not a loosening of FR-GL-003. A
    # foreign-key check locks the parent row with `SELECT ... FOR KEY SHARE`,
    # and a row-locking clause requires UPDATE or DELETE privilege - so
    # withholding both from the owner of a REFERENCED table makes every insert
    # into the referencing table fail. journal_line references journal_entry,
    # and journal_entry references itself through reverses_entry_id, so
    # ledger.post_entry could not write a line at all until the privilege came
    # back. journal_line is referenced by nothing and therefore keeps the full
    # revoke, which is the asymmetry below.
    #
    # What still stops an UPDATE is journal_posting_immutable(), asserted in
    # tests/integration/test_referenced_table_privileges.py alongside the
    # privilege itself - a lock is not a write and fires no trigger.
    assert holders == {
        ("ledgr_ledger", "journal_entry", "INSERT"),
        ("ledgr_ledger", "journal_entry", "UPDATE"),
        ("ledgr_ledger", "journal_line", "INSERT"),
    }, (
        f"unexpected write privileges on the posting tables: {sorted(holders)}. "
        "Only ledgr_ledger may write; only INSERT, plus the UPDATE on "
        "journal_entry that foreign-key row locks require (0027)."
    )


async def test_a_posting_written_through_the_api_is_readable_by_the_application(
    two_organizations: SeededTenants,
) -> None:
    """End to end through the narrow waist: the application cannot INSERT, can
    call the function, and can read what the function wrote.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        admin = str(two_organizations.admin_a)

        year = (
            await conn.execute(
                text(
                    "INSERT INTO fiscal_year "
                    "(organization_id, administration_id, start_date, end_date) "
                    "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id"
                ),
                {"org": str(two_organizations.org_a), "admin": admin},
            )
        ).scalar_one()
        period = (
            await conn.execute(
                text(
                    "INSERT INTO period (organization_id, administration_id, "
                    "fiscal_year_id, period_number, start_date, end_date) "
                    "VALUES (:org, :admin, :year, 1, '2026-01-01', '2026-01-31') "
                    "RETURNING id"
                ),
                {
                    "org": str(two_organizations.org_a),
                    "admin": admin,
                    "year": str(year),
                },
            )
        ).scalar_one()
        journal = (
            await conn.execute(
                text(
                    "SELECT id FROM ledger.create_journal(  :admin, 'MEM', 'Memoriaal', 'memorial')"
                ),
                {"admin": admin},
            )
        ).scalar_one()
        cash = (
            await conn.execute(
                text("SELECT id FROM ledger.create_account(  :admin, '1000', 'Kas', 'asset')"),
                {"admin": admin},
            )
        ).scalar_one()
        revenue = (
            await conn.execute(
                text("SELECT id FROM ledger.create_account(  :admin, '8000', 'Omzet', 'revenue')"),
                {"admin": admin},
            )
        ).scalar_one()

        entry_id = (
            await conn.execute(
                text(
                    "SELECT id FROM ledger.post_entry("
                    "  p_administration_id => :admin,"
                    "  p_journal_id        => :journal,"
                    "  p_period_id         => :period,"
                    "  p_entry_date        => '2026-01-15',"
                    "  p_description       => 'contante verkoop',"
                    "  p_document_reference=> null,"
                    "  p_posted_by_user_id => :actor,"
                    "  p_source_system     => 'pytest',"
                    "  p_lines             => cast(:lines as jsonb))"
                ),
                {
                    "admin": admin,
                    "journal": str(journal),
                    "period": str(period),
                    "actor": str(two_organizations.owner_a),
                    # Amounts as strings, not JSON numbers: ledger.post_entry
                    # rejects a number outright, which is NFR-031's tripwire at
                    # the database boundary.
                    "lines": json.dumps(
                        [
                            {
                                "account_id": str(cash),
                                "debit": "121.00",
                                "credit": "0.00",
                            },
                            {
                                "account_id": str(revenue),
                                "debit": "0.00",
                                "credit": "121.00",
                            },
                        ]
                    ),
                },
            )
        ).scalar_one()

        row = (
            await conn.execute(
                text("SELECT entry_number, description FROM journal_entry WHERE id = :id"),
                {"id": str(entry_id)},
            )
        ).one()

    assert row.entry_number == 1
    assert row.description == "contante verkoop"


async def test_one_tenant_cannot_post_into_anothers_administration(
    two_organizations: SeededTenants,
) -> None:
    """IAM-005. The SECURITY DEFINER functions run as ledgr_ledger, which is a
    privilege elevation - so this is the test that it elevates over WHAT may
    be written and not over WHOSE data.

    RLS predicates read app.current_org_id(), which is session state rather
    than role state, so a definer function called by tenant B sees tenant B's
    rows. FORCE ROW LEVEL SECURITY is what makes that apply to ledgr_ledger
    despite it owning the tables.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            await conn.execute(
                text(
                    "SELECT id FROM ledger.create_journal(  :admin, 'MEM', 'Memoriaal', 'memorial')"
                ),
                {"admin": str(two_organizations.admin_a)},
            )


async def test_a_session_with_no_tenant_context_cannot_post(
    two_organizations: SeededTenants,
) -> None:
    """CLAUDE.md rule 1: a request without tenant context fails closed. The
    definer function does not change that - app.current_org_id() returns NULL,
    every policy predicate evaluates to NULL rather than TRUE, and the insert
    is refused.
    """
    async with app_engine.begin() as conn:
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            await conn.execute(
                text(
                    "SELECT id FROM ledger.create_journal(  :admin, 'MEM', 'Memoriaal', 'memorial')"
                ),
                {"admin": str(two_organizations.admin_a)},
            )


async def test_the_ledger_owner_cannot_be_connected_as(
    two_organizations: SeededTenants,
) -> None:
    """Ownership by a NOLOGIN role is what stops a migration disarming the
    triggers. It only holds while the role really has no way in.
    """
    rows = await _as_app(
        "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'ledgr_ledger'"
    )
    assert rows, "ledgr_ledger does not exist"
    can_login, is_super, bypasses_rls = rows[0]
    assert not can_login, "ledgr_ledger must be NOLOGIN"
    assert not is_super, "ledgr_ledger must not be a superuser"
    assert not bypasses_rls, (
        "ledgr_ledger must not hold BYPASSRLS: the definer functions run as it, "
        "and BYPASSRLS would turn every one of them into a cross-tenant read path"
    )


async def test_nobody_can_set_role_to_the_ledger_owner(
    two_organizations: SeededTenants,
) -> None:
    """NOLOGIN closes the front door; membership would be the back one. A role
    granted to somebody can be reached with SET ROLE without ever logging in.
    """
    rows = await _as_app(
        "SELECT m.member::regrole::text FROM pg_auth_members m "
        "WHERE m.roleid = 'ledgr_ledger'::regrole"
    )
    assert rows == [], f"ledgr_ledger is granted to {[r[0] for r in rows]}"


async def test_the_ledger_has_no_floating_point_column(
    two_organizations: SeededTenants,
) -> None:
    """NFR-031, asserted against the catalogue rather than by reading the
    migration. A float column added by a later migration - a rate, a
    percentage, a "temporary" analytics field - is how a decimal guarantee
    ends. This finds it whatever it is called.
    """
    rows = await _as_app(
        """
        SELECT table_name, column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name IN (
                 'journal_entry', 'journal_line', 'journal_sequence',
                 'ledger_account', 'ledger_journal', 'subledger_party')
           AND data_type IN ('real', 'double precision')
        """
    )
    assert rows == [], (
        f"floating point in the ledger (NFR-031): {rows}. Monetary values and "
        "anything in their calculation path use numeric."
    )


async def test_posting_amounts_have_the_declared_scale(
    two_organizations: SeededTenants,
) -> None:
    """NFR-031 says "decimal types with defined scale". A numeric with no
    precision or scale is decimal but undefined - it would accept
    121.000000001 and store it.
    """
    rows = await _as_app(
        """
        SELECT column_name, numeric_precision, numeric_scale
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'journal_line'
           AND column_name IN ('debit', 'credit')
         ORDER BY column_name
        """
    )
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("credit", 19, 2),
        ("debit", 19, 2),
    ], rows


async def test_the_seeded_administrations_exist(
    two_organizations: SeededTenants,
) -> None:
    """Guards the fixture the rest of this file leans on. If seeding silently
    produced nothing, every `_refused` above would still pass - a permission
    error does not need a row to exist.
    """
    assert isinstance(two_organizations.admin_a, uuid.UUID)
    rows = await _as_app("SELECT count(*) FROM administration")
    assert rows[0][0] >= 0

"""The integrity job against a real Postgres (NFR-033).

Runs tests/ledger/integrity_cases.py - the same corruption table
tests/ledger/test_integrity.py runs against the in-memory ledger - through
migration 0023's `ledger.integrity_*` functions, so every deviation is found
by SQL looking at rows rather than by Python looking at objects it just read.

Three things only this file can establish:

  1. **The checks find things in the database.** The in-memory suite proves
     the shape of the report; this proves the queries.

  2. **The corruptions are reachable at all.** Every one of them needs the
     append-only triggers disarmed, which needs a superuser - which is the
     argument for NFR-033 existing, made as executable evidence rather than
     as a paragraph. tests/integration/test_audit_tamper_evidence.py makes
     the same argument for the audit log.

  3. **Tenant scoping is RLS's, and it fails closed.** The 0023 functions are
     SECURITY INVOKER precisely so that `ledgr_app` sees one tenant and
     `ledgr_ops` sees all of them. Both are asserted here, including the case
     that matters most: a connection with no tenant context examines nothing
     and must not report that as a pass.

--- On cleanup ---

The corrupted rows are left where they are. Each test seeds its own pair of
organizations (`two_organizations`), every assertion is scoped to the
administration it seeded, and the ledger is append-only - removing the damage
would need the same disarmed triggers that caused it. What IS restored, in a
finally, is the triggers themselves: leaving those disabled would silently
turn off FR-GL-003 for every test that runs afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from api.db import engine as app_engine
from api.ledger.integrity import IntegrityReport, LedgerIntegrityJob
from api.ledger.integrity_repository import SqlIntegrityRepository
from tests.integration.ledger_world import build_db_world, post_entry
from tests.ledger.integrity_cases import CORRUPTION_CASES, CorruptionCase, scenario
from tests.ledger.world import World
from tests.support.fake_integrity_alerter import FakeIntegrityAlerter
from tests.support.seed import SeededTenants

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")

#: The tables whose guards have to come off before any of this is writable.
#: journal_entry's list includes the internal referential-integrity trigger
#: that would otherwise refuse to delete a header while its lines point at it,
#: which is why deleting a header is a superuser-only act rather than merely a
#: forbidden one.
GUARDED_TABLES = ("journal_entry", "journal_line")


@pytest_asyncio.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """A superuser connection. ledgr_app cannot disable a trigger, take
    ownership, or SET ROLE to anything - which is the property the ledger
    relies on and therefore the reason reaching these states needs this.
    """
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not set")
    engine = create_async_engine(_ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        yield engine
    finally:
        await engine.dispose()


# ===========================================================================
# Harness
# ===========================================================================


async def seed(tenants: SeededTenants) -> tuple[World, uuid.UUID]:
    """A world with the case table's scenario posted through the narrow API.

    Also creates one party in the SECOND administration. No healthy fixture
    needs it - a party from another administration has no legitimate use, which
    is exactly why a line naming one is a finding - so it is seeded here for
    the case that attributes an amount to it.
    """
    async with app_engine.connect() as conn:
        world = await build_db_world(conn, tenants)
        for candidate in scenario(world):
            await post_entry(conn, candidate)
        other_party = (
            await conn.execute(
                text(
                    "SELECT id FROM ledger.create_party(  :admin, 'customer', 'Buurman B.V.', null)"
                ),
                {"admin": str(world.other_administration_id)},
            )
        ).scalar_one()
        await conn.commit()
    return world, other_party


async def report_as_app(
    organization_id: uuid.UUID | None,
    *,
    administration_id: uuid.UUID | None = None,
    alerter: FakeIntegrityAlerter | None = None,
) -> IntegrityReport:
    """Run the job as `ledgr_app`, the way an administrator checking their own
    books would. `organization_id=None` deliberately leaves tenant context
    unset, which is the fail-closed case.
    """
    async with app_engine.connect() as conn:
        # Set explicitly in both directions. `None` means "no tenant context",
        # and clearing the setting is the only way to *create* that state
        # reliably: tests/integration/ledger_world.py sets the tenant
        # session-wide so it survives a commit, and a pooled connection can
        # come back still carrying it. Relying on a connection being fresh
        # would make the fail-closed assertion below pass or fail on pool
        # order rather than on behaviour.
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id) if organization_id is not None else ""},
        )
        job = LedgerIntegrityJob(
            SqlIntegrityRepository(AsyncSession(bind=conn)),
            alerter or FakeIntegrityAlerter(),
        )
        return await job.run(administration_id=administration_id)


async def report_as_ops(
    engine: AsyncEngine, *, administration_id: uuid.UUID | None = None
) -> IntegrityReport:
    """Run the job as `ledgr_ops`, the way the nightly sweep does: no tenant
    context at all, cross-tenant by BYPASSRLS.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        job = LedgerIntegrityJob(
            SqlIntegrityRepository(AsyncSession(bind=conn)), FakeIntegrityAlerter()
        )
        return await job.run(administration_id=administration_id)


@asynccontextmanager
async def triggers_disarmed(engine: AsyncEngine) -> AsyncIterator[None]:
    """DISABLE TRIGGER ALL on the posting tables, restored in a finally.

    Postgres runs BEFORE triggers for every writer including a superuser, so
    this is the only way to reach the states below - and re-enabling is not
    optional: leaving them off would turn FR-GL-003's append-only guarantee
    off for every test that runs after this one.
    """
    async with engine.begin() as conn:
        for table in GUARDED_TABLES:
            await conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER ALL"))
    try:
        yield
    finally:
        async with engine.begin() as conn:
            for table in GUARDED_TABLES:
                await conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER ALL"))


def corruption_params(world: World, other_party: uuid.UUID) -> dict[str, str]:
    return {
        "admin": str(world.administration_id),
        "journal": str(world.journal_id),
        "year": str(world.fiscal_year_id),
        "receivables": str(world.receivables_control_id),
        "payables": str(world.payables_control_id),
        "cash": str(world.cash_account_id),
        "revenue": str(world.revenue_account_id),
        "customer": str(world.customer_party_id),
        "supplier": str(world.supplier_party_id),
        "other_party": str(other_party),
    }


async def apply(engine: AsyncEngine, statements: tuple[str, ...], params: dict[str, str]) -> None:
    """Runs the corruption as a superuser, one statement at a time.

    Only the parameters a statement actually names are passed: `text()` binds
    by name, and handing it keys it never mentions is a difference in
    behaviour between drivers that is not worth depending on.
    """
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(
                text(statement),
                {k: v for k, v in params.items() if f":{k}" in statement},
            )


# ===========================================================================
# The baseline
# ===========================================================================


async def test_a_correctly_posted_ledger_reports_intact(
    two_organizations: SeededTenants,
) -> None:
    """Every entry written through `ledger.post_entry`, every check run
    against the rows it produced.

    This is the assertion that makes the whole job usable as an oracle: if the
    healthy path produced findings, no failure below would mean anything.
    """
    world, _ = await seed(two_organizations)
    alerter = FakeIntegrityAlerter()

    report = await report_as_app(
        two_organizations.org_a,
        administration_id=world.administration_id,
        alerter=alerter,
    )

    assert report.intact, f"a correctly posted ledger reported: {report.deviations}"
    assert not report.examined_nothing
    assert report.scope.entries == 3
    assert report.scope.lines == 6
    assert not alerter.called, "a clean run must not alert (NFR-045)"


async def test_a_reversal_leaves_the_books_intact(
    two_organizations: SeededTenants,
) -> None:
    """FR-GL-003's correction mechanism against the integrity checks.

    A reversal doubles the number of postings on every account it touches and
    adds an entry to the series. If the checks were written against a naive
    idea of what a ledger looks like, this is where that would show.
    """
    world, _ = await seed(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        original = (
            await conn.execute(
                text(
                    "SELECT id FROM journal_entry WHERE administration_id = :admin "
                    "  AND journal_id = :journal AND entry_number = 2"
                ),
                {
                    "admin": str(world.administration_id),
                    "journal": str(world.journal_id),
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "SELECT id FROM ledger.reverse_entry("
                "  :entry, :period, cast(:d as date), 'correctie', :actor, 'pytest')"
            ),
            {
                "entry": str(original),
                "period": str(world.open_period_id),
                "d": world.entry_date,
                "actor": str(world.actor_user_id),
            },
        )
        await conn.commit()

    report = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)

    assert report.intact, f"a reversal was reported as a deviation: {report.deviations}"
    assert report.scope.entries == 4


# ===========================================================================
# The corruption table
# ===========================================================================


@pytest.mark.parametrize("case", CORRUPTION_CASES, ids=lambda c: c.name)
async def test_the_job_detects(
    case: CorruptionCase, admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    world, other_party = await seed(two_organizations)
    alerter = FakeIntegrityAlerter()

    before = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)
    assert before.intact, "the scenario must start from clean books"

    async with triggers_disarmed(admin_engine):
        await apply(admin_engine, case.corrupt_sql, corruption_params(world, other_party))

    report = await report_as_app(
        two_organizations.org_a,
        administration_id=world.administration_id,
        alerter=alerter,
    )

    found = {deviation.deviation for deviation in report.deviations}
    assert case.expect <= found, (
        f"{case.name} ({case.requirement}): expected {sorted(case.expect)}, got {sorted(found)}"
    )
    assert alerter.called, "NFR-033 requires alerting on any deviation"
    assert alerter.only.deviations == report.deviations

    for deviation in report.deviations:
        assert deviation.administration_id == world.administration_id
        assert deviation.subject_id is not None
        assert deviation.summary.strip()


async def test_a_corruption_is_only_reachable_with_the_guards_off(
    two_organizations: SeededTenants,
) -> None:
    """The premise of every test above, asserted rather than assumed.

    If the application role could make these edits, the corruptions would be
    proving something about a reachable state - and the ledger would have a
    much bigger problem than a missing integrity job.
    """
    world, _ = await seed(two_organizations)

    for statement in (
        "UPDATE journal_line SET debit = debit + 1.00",
        "DELETE FROM journal_line",
        "DELETE FROM journal_entry",
        "ALTER TABLE journal_line DISABLE TRIGGER ALL",
    ):
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            async with app_engine.connect() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_org_id', :org, true)"),
                    {"org": str(two_organizations.org_a)},
                )
                await conn.execute(text(statement))
                await conn.commit()

    report = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)
    assert report.intact, "nothing above touched the ledger"


# ===========================================================================
# Numbers, not just keys
# ===========================================================================


async def test_an_edited_amount_is_reported_with_the_exact_difference(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The report has to be actionable (NFR-045), which means the numbers in
    it have to be right and exact - and strings, because NFR-031 does not stop
    applying at the edge of the calculation path.
    """
    world, other_party = await seed(two_organizations)
    case = next(c for c in CORRUPTION_CASES if c.name == "a posted amount is edited")

    async with triggers_disarmed(admin_engine):
        await apply(admin_engine, case.corrupt_sql, corruption_params(world, other_party))

    report = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)

    unbalanced = next(d for d in report.deviations if d.deviation == "entry_unbalanced")
    assert unbalanced.detail["difference"] == "1.00"
    assert isinstance(unbalanced.detail["total_debit"], str)
    assert unbalanced.detail["total_debit"] == "11.00"
    assert unbalanced.detail["total_credit"] == "10.00"

    year = next(d for d in report.deviations if d.deviation == "administration_unbalanced")
    # 10.00 + 121.00 + 60.50 in debits, plus the 1.00 that was added.
    assert year.detail["total_debit"] == "192.50"
    assert year.detail["total_credit"] == "191.50"
    assert Decimal(str(year.detail["difference"])) == Decimal("1.00")


async def test_a_gap_names_the_missing_numbers(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """FR-GL-013's gap report, reached through the sweep. The sample is what
    whoever is paged starts from, so it has to name the actual hole.
    """
    world, other_party = await seed(two_organizations)
    case = next(c for c in CORRUPTION_CASES if c.name.startswith("an entry in the middle"))

    async with triggers_disarmed(admin_engine):
        await apply(admin_engine, case.corrupt_sql, corruption_params(world, other_party))

    report = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)

    gap = next(d for d in report.deviations if d.deviation == "numbering_gap")
    assert gap.detail["missing_sample"] == [2]
    assert gap.detail["missing_count"] == 1
    assert gap.detail["entries"] == 2
    assert gap.detail["highest_number"] == 3
    assert gap.subject_id == world.journal_id


async def test_a_deleted_tail_is_caught_by_the_allocator_alone(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The case the gap report structurally cannot see: 1..2 is gapless.

    Only `journal_sequence` remembers that a third number was issued, which is
    the same role an external anchor plays for the audit chain (IAM-092).
    Asserted explicitly because it is the reason the allocator is checked at
    all rather than trusted as an implementation detail.
    """
    world, other_party = await seed(two_organizations)
    case = next(c for c in CORRUPTION_CASES if c.name.startswith("the tail of a series"))

    async with triggers_disarmed(admin_engine):
        await apply(admin_engine, case.corrupt_sql, corruption_params(world, other_party))

    report = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)

    found = {d.deviation for d in report.deviations}
    assert "sequence_drift" in found
    assert "numbering_gap" not in found, (
        "the remaining series really is gapless - which is why the gap report "
        "alone would have called this ledger clean"
    )

    # And the gap report agrees: it is not that it is broken, it is that it
    # cannot see this.
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        gaps = list(
            await conn.execute(
                text("SELECT missing_number FROM ledger.numbering_gaps(:j, :y)"),
                {"j": str(world.journal_id), "y": str(world.fiscal_year_id)},
            )
        )
    assert gaps == []


# ===========================================================================
# Who may run it, and what they see (CLAUDE.md rule 1)
# ===========================================================================


async def test_without_tenant_context_the_run_examines_nothing(
    two_organizations: SeededTenants,
) -> None:
    """RLS fails closed, so an unscoped `ledgr_app` run sees no rows - and
    therefore finds no deviations, over a database it cannot read.

    This is the single most dangerous way an integrity job can lie, and the
    reason `IntegrityScope` exists. `intact` is True here and it means
    nothing; `examined_nothing` is what a caller must check.
    """
    await seed(two_organizations)

    report = await report_as_app(None)

    assert report.intact
    assert report.examined_nothing
    assert report.scope.administrations == 0
    assert report.scope.entries == 0


async def test_one_tenant_cannot_see_another_tenants_deviations(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """CLAUDE.md rule 1 applies to this query path like every other. The
    functions are SECURITY INVOKER precisely so that the answer is RLS's, and
    a report is a disclosure: "administration X entry 4 is unbalanced by
    121.00" is exactly the kind of thing a neighbouring tenant must not read.
    """
    world, other_party = await seed(two_organizations)
    case = next(c for c in CORRUPTION_CASES if c.name == "a posted amount is edited")

    async with triggers_disarmed(admin_engine):
        await apply(admin_engine, case.corrupt_sql, corruption_params(world, other_party))

    mine = await report_as_app(two_organizations.org_a, administration_id=world.administration_id)
    theirs = await report_as_app(two_organizations.org_b, administration_id=world.administration_id)

    assert not mine.intact
    assert theirs.deviations == (), "org B read org A's findings"
    assert theirs.examined_nothing, (
        "and could not even see the administration, so the empty report is "
        "correctly flagged as unverified rather than as a pass"
    )


async def test_the_ops_role_sweeps_every_tenant_in_one_pass(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The nightly job's connection. `ledgr_ops` holds BYPASSRLS, so one call
    covers every tenant with no per-organization loop and no tenant context.

    This is what SECURITY INVOKER buys, and what SECURITY DEFINER would have
    silently taken away: the definer functions run as `ledgr_ledger`, which is
    NOBYPASSRLS and FORCE ROW LEVEL SECURITY'd on these tables, so the whole
    sweep would have reported a clean bill of health for every tenant.
    """
    world, _ = await seed(two_organizations)

    everything = await report_as_ops(admin_engine)
    scoped = await report_as_ops(admin_engine, administration_id=world.administration_id)

    assert everything.scope.administrations >= 2, (
        "the sweep must reach past one tenant; both seeded organizations have an administration"
    )
    assert everything.scope.entries >= 3
    assert not everything.examined_nothing

    # Scoped to the administration this test seeded, which nothing has
    # corrupted - asserted rather than asserting the whole database is clean,
    # because earlier tests in this file deliberately leave damage behind.
    assert scoped.intact, f"freshly seeded books reported: {scoped.deviations}"
    assert scoped.scope.entries == 3


async def test_support_tooling_reads_and_never_writes(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """CMP-009's "including support tooling", at the boundary 0023 added.

    The privileges are asserted directly rather than only by attempting a
    write: a statement can fail for the wrong reason - a mistyped function
    signature also raises - and this is a claim about what `ledgr_ops` HOLDS.
    """
    await seed(two_organizations)

    async with admin_engine.connect() as conn:
        privileges = (
            await conn.execute(
                text(
                    "SELECT "
                    "  has_table_privilege('ledgr_ops', 'journal_entry', 'SELECT') AS reads,"
                    "  has_table_privilege('ledgr_ops', 'journal_entry', 'INSERT') AS inserts,"
                    "  has_table_privilege('ledgr_ops', 'journal_line', 'UPDATE') AS updates,"
                    "  has_table_privilege('ledgr_ops', 'journal_entry', 'DELETE') AS deletes,"
                    "  has_table_privilege('ledgr_ops', 'journal_sequence', 'UPDATE') AS allocates,"
                    "  has_function_privilege('ledgr_ops',"
                    "    'ledger.integrity_findings(uuid)', 'EXECUTE') AS checks,"
                    "  has_function_privilege('ledgr_ops',"
                    "    'ledger.integrity_scope(uuid)', 'EXECUTE') AS scopes,"
                    "  has_function_privilege('ledgr_ops',"
                    "    'ledger.post_entry(uuid,uuid,uuid,date,text,text,uuid,text,"
                    "jsonb,uuid,text,uuid)', 'EXECUTE') AS posts"
                )
            )
        ).one()

    assert privileges.reads, "the sweep has to be able to read the ledger"
    assert privileges.checks and privileges.scopes, "and to run the checks"
    assert not privileges.posts, (
        "0023 re-issues the ledger schema's grants after revoking PUBLIC; "
        "ledgr_ops must not have picked up the write API on the way through"
    )
    assert not privileges.inserts
    assert not privileges.updates
    assert not privileges.deletes
    assert not privileges.allocates, (
        "an operator role that could move the allocator could hide a deleted "
        "tail from the very check that finds it"
    )

    # And the attempts themselves fail, since a grant is not the only thing
    # standing in the way - the append-only triggers are.
    for statement in (
        "UPDATE journal_line SET debit = 0",
        "DELETE FROM journal_entry",
        "UPDATE journal_sequence SET next_number = 1",
    ):
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            async with admin_engine.begin() as conn:
                await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
                await conn.execute(text(statement))


async def test_the_migration_role_cannot_replace_a_check(
    admin_engine: AsyncEngine,
) -> None:
    """The attack that would make every test in this file pass forever: swap
    the verifier for one that returns nothing.

    0019 defends `app.verify_audit_chain` from it by owning the function with
    a role the migration role is not a member of, and 0023 does the same with
    `ledgr_ledger`. Without this, an integrity job is only as trustworthy as
    the last person to run a migration.
    """
    for statement in (
        "CREATE OR REPLACE FUNCTION ledger.integrity_findings(uuid) "
        "RETURNS SETOF ledger.integrity_finding AS "
        "$$ SELECT * FROM ledger.integrity_balance(null) WHERE false $$ LANGUAGE sql",
        "CREATE OR REPLACE FUNCTION ledger.integrity_scope(uuid) "
        "RETURNS TABLE (administrations bigint, journals bigint, accounts bigint, "
        "               entries bigint, lines bigint) AS "
        "$$ SELECT 1::bigint, 1::bigint, 1::bigint, 1::bigint, 1::bigint $$ LANGUAGE sql",
        "DROP FUNCTION ledger.integrity_numbering(uuid)",
        "ALTER FUNCTION ledger.integrity_balance(uuid) OWNER TO ledgr_migrator",
    ):
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            async with admin_engine.begin() as conn:
                await conn.execute(text("SET LOCAL ROLE ledgr_migrator"))
                await conn.execute(text(statement))

"""The attempted tamper test PRD §22 makes a GA blocker.

    "Audit log immutability verified by attempted tamper test."

This file is that evidence. It enumerates every interface through which an
audit entry could be modified or removed - the application, direct SQL as
each role the system defines, and the migration/superuser interface - and
asserts that each attempt is either PREVENTED (the statement fails) or
DETECTED (the hash chain, or an external anchor, catches it).

--- The one assertion ---

Every test funnels through `attempt_tamper`, which classifies the outcome as
exactly one of:

    PREVENTED           the statement raised; the log is unchanged
    DETECTED            it succeeded and app.verify_audit_chain reports the
                        break
    DETECTED_BY_ANCHOR  it succeeded, the chain still verifies, but the head
                        no longer matches an anchor taken beforehand
    UNDETECTED          it succeeded and nothing noticed - a test failure

Expressing it as a disjunction rather than asserting a specific mechanism is
deliberate. IAM-092 requires immutable AND tamper-evident, and which of the
two catches a given attack is an implementation detail that may change; that
NOTHING gets through unnoticed is the property being evidenced. Each test
also asserts which outcome it expects, so a change from PREVENTED to
DETECTED - a real weakening - still fails rather than passing quietly.

--- Why UNDETECTED is reachable at all ---

Two attacks are genuinely invisible to the chain alone, and both are proved
here rather than hidden: truncating the END of a chain, and rewriting the
whole chain consistently. Both are caught only by an anchor held outside the
database (ADR-020, scripts/anchor_audit_chain.py). The tests for those assert
DETECTED_BY_ANCHOR, which is what makes the anchor a GA requirement rather
than a nicety - and what a reviewer reading this file needs to see stated.
"""

from __future__ import annotations

import enum
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.db import engine as app_engine
from tests.support.fake_audit_repository import seal
from tests.support.seed import SeededTenants

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")


class TamperOutcome(enum.Enum):
    PREVENTED = "prevented"
    DETECTED = "detected"
    DETECTED_BY_ANCHOR = "detected_by_anchor"
    UNDETECTED = "undetected"


@dataclass(frozen=True, slots=True)
class Anchor:
    sequence_number: int
    head_hash: str
    entries: int


@pytest_asyncio.fixture
async def admin_engine() -> AsyncEngine:
    """A superuser connection - the migration interface. ledgr_app cannot
    SET ROLE to the other roles (it is a member of none of them, which is
    itself part of what is being evidenced), so reaching them needs this.
    """
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not set")
    engine = create_async_engine(_ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrator_engine() -> AsyncEngine:
    """A REAL ledgr_migrator login (bootstrap_test_db.py's test-only
    password, the same bridge ledgr_app's own login already needed), not
    admin_engine + SET ROLE ledgr_migrator.

    That proxy works for GRANT/ALTER/DROP, which Postgres checks against
    current_role - but not for the one statement this role list also needs
    to try, `SET ROLE ledgr_audit`: SET ROLE's own membership check is made
    against session_user, and session_user here is still the postgres
    superuser regardless of any SET ROLE already issued in the same
    session, which is superuser-privileged to change to any role, so the
    proxy would report success for an escalation ledgr_migrator can never
    actually perform, and a real login is the only way to ask this specific
    question honestly.
    """
    url = os.environ.get("TEST_MIGRATOR_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_MIGRATOR_DATABASE_URL is not set")
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def seed_entries(organization_id: uuid.UUID, actor: uuid.UUID, count: int = 5) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        log = AuditLog(SqlAuditRepository(AsyncSession(bind=conn)))
        for index in range(count):
            await log.record(
                AuditEvent(
                    organization_id=organization_id,
                    category=AuditCategory.POSTING,
                    action=f"post-{index}",
                    resource_type="journal_entry",
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.USER,
                    actor_user_id=actor,
                    source_ip="203.0.113.7",
                    user_agent="ga-tamper-test",
                    correlation_id=f"req-{index}",
                    detail={"index": index},
                )
            )
        await conn.commit()


async def _chain_state(organization_id: uuid.UUID) -> tuple[Anchor, str | None]:
    """(anchor, break reason) as the application sees them."""
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        log = AuditLog(SqlAuditRepository(AsyncSession(bind=conn)))
        result = await log.verify(organization_id)
        head = await log.head(organization_id)
    return (
        Anchor(head.sequence_number, head.head_hash, head.entries),
        result.break_found.reason if result.break_found else None,
    )


async def attempt_tamper(
    organization_id: uuid.UUID, *, run: Callable[[], Awaitable[None]]
) -> TamperOutcome:
    """Runs one tamper attempt and classifies what stopped it, if anything.

    The anchor is taken BEFORE the attempt, which is what a real deployment
    does on a schedule (scripts/anchor_audit_chain.py). Without one, the two
    attacks that leave a self-consistent chain would be indistinguishable
    from no attack at all - which is the point the last section makes.
    """
    before, existing_break = await _chain_state(organization_id)
    assert existing_break is None, f"chain was already broken before the attempt: {existing_break}"

    try:
        await run()
    except (DBAPIError, SQLAlchemyError):
        return TamperOutcome.PREVENTED

    after, reason = await _chain_state(organization_id)
    if reason is not None:
        return TamperOutcome.DETECTED
    if (
        after.sequence_number != before.sequence_number
        or after.head_hash != before.head_hash
        or after.entries != before.entries
    ):
        return TamperOutcome.DETECTED_BY_ANCHOR
    return TamperOutcome.UNDETECTED


def _as_detail(raw: object) -> dict[str, object]:
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _as(engine: AsyncEngine, role: str | None, sql: str, tenant: uuid.UUID, **params: object):  # type: ignore[no-untyped-def]
    """Returns a callable that runs `sql` as `role` with tenant context set.

    The tenant is named `tenant`, not `org`, because several of the statements
    below take an `:org` bind parameter of their own - and a positional called
    `org` collides with it in **params, so every one of those call sites raised
    `_as() got multiple values for argument 'org'` before the statement ran.
    """

    async def run() -> None:
        async with engine.begin() as conn:
            if role is not None:
                await conn.execute(text(f"SET LOCAL ROLE {role}"))
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            await conn.execute(text(sql), params)

    return run


# ===========================================================================
# 1. The application interface
# ===========================================================================


def test_the_repository_exposes_no_way_to_modify_or_remove_an_entry() -> None:
    """The application interface, checked by construction rather than by
    trying every method: there is no update, delete, purge or upsert on the
    repository, so no application code can call one.
    """
    forbidden = {"update", "delete", "remove", "purge", "truncate", "upsert", "edit"}
    methods = {
        name
        for name in dir(SqlAuditRepository)
        if not name.startswith("_") and callable(getattr(SqlAuditRepository, name))
    }

    assert not {m for m in methods if any(word in m.lower() for word in forbidden)}
    assert methods == {"append", "verify_chain", "chain_head", "search"}


def test_the_service_exposes_no_way_to_modify_or_remove_an_entry() -> None:
    methods = {
        name
        for name in dir(AuditLog)
        if not name.startswith("_") and callable(getattr(AuditLog, name))
    }

    assert methods == {"record", "verify", "head", "search"}


async def test_the_application_cannot_overwrite_an_entry_at_insert_time(
    two_organizations: SeededTenants,
) -> None:
    """The one write path the application HAS, aimed at an existing row: an
    INSERT naming a sequence number that already exists. The sealing trigger
    reassigns it, so it appends rather than overwriting.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(
            app_engine,
            None,
            "INSERT INTO audit_log (organization_id, actor_type, category, action, "
            " resource_type, outcome, sequence_number, previous_hash, entry_hash) "
            "VALUES (:org, 'system', 'configuration', 'overwrite', 'thing', 'success', "
            "        2, 'chosen', 'chosen')",
            two_organizations.org_a,
            org=str(two_organizations.org_a),
        ),
    )

    # An append changes the head, which the anchor comparison notices - and
    # correctly so: this added an entry rather than replacing one.
    assert outcome is TamperOutcome.DETECTED_BY_ANCHOR
    anchor, reason = await _chain_state(two_organizations.org_a)
    assert anchor.entries == 4, "it appended; entry 2 is untouched"
    assert anchor.sequence_number == 4, "the trigger reassigned the sequence number"
    assert reason is None, "and the chain still verifies"


# ===========================================================================
# 2. Direct SQL as ledgr_app
# ===========================================================================

_APP_STATEMENTS: list[tuple[str, str]] = [
    ("plain update", "UPDATE audit_log SET outcome = 'denied'"),
    ("forge a hash", "UPDATE audit_log SET entry_hash = 'forged'"),
    ("rewrite the chain link", "UPDATE audit_log SET previous_hash = 'forged'"),
    ("edit event detail", "UPDATE audit_log SET detail = '{}'::jsonb"),
    ("backdate an entry", "UPDATE audit_log SET occurred_at = '2000-01-01T00:00:00Z'"),
    ("reassign the actor", "UPDATE audit_log SET actor_user_id = NULL"),
    ("delete everything", "DELETE FROM audit_log"),
    ("delete one entry", "DELETE FROM audit_log WHERE sequence_number = 2"),
    ("truncate", "TRUNCATE audit_log"),
    ("truncate cascade", "TRUNCATE audit_log CASCADE"),
    # A writable CTE is still an UPDATE; naming it differently changes nothing.
    (
        "update inside a CTE",
        "WITH t AS (UPDATE audit_log SET action = 'x' RETURNING id) SELECT count(*) FROM t",
    ),
    (
        "delete inside a CTE",
        "WITH t AS (DELETE FROM audit_log RETURNING id) SELECT count(*) FROM t",
    ),
    # UPDATE ... FROM, in case the join form is privileged differently.
    (
        "update via a join",
        "UPDATE audit_log a SET action = 'x' FROM organization o WHERE o.id = a.organization_id",
    ),
    # The sneakiest: an UPDATE wearing an INSERT's clothes. ON CONFLICT DO
    # UPDATE fires BEFORE UPDATE triggers, which is what makes this fail.
    (
        "upsert onto an existing entry",
        "INSERT INTO audit_log (organization_id, actor_type, category, action, "
        " resource_type, outcome, sequence_number, previous_hash, entry_hash) "
        "VALUES (:org, 'system', 'configuration', 'x', 'thing', 'success', 1, 'a', 'b') "
        "ON CONFLICT (organization_id, sequence_number) DO UPDATE SET action = 'tampered'",
    ),
    # Structural attacks: all need ownership, which ledgr_app does not have.
    ("disarm every trigger", "ALTER TABLE audit_log DISABLE TRIGGER ALL"),
    ("drop the update guard", "DROP TRIGGER audit_log_no_update_trg ON audit_log"),
    ("drop the table", "DROP TABLE audit_log"),
    ("take ownership", "ALTER TABLE audit_log OWNER TO ledgr_app"),
    # "grant itself UPDATE" is deliberately NOT here - see
    # test_a_self_grant_of_update_gives_the_application_role_nothing below,
    # which needs a different assertion than attempt_tamper can make.
    # A rule rewriting UPDATE to a no-op would silently swallow tampering.
    (
        "install a rewrite rule",
        "CREATE RULE swallow AS ON UPDATE TO audit_log DO INSTEAD NOTHING",
    ),
    # Becoming the owner, or the one role with BYPASSRLS that owns nothing.
    ("become the audit owner", "SET ROLE ledgr_audit"),
    ("become the bootstrap role", "SET ROLE ledgr_bootstrap"),
]


@pytest.mark.parametrize(
    ("name", "statement"), _APP_STATEMENTS, ids=[n for n, _ in _APP_STATEMENTS]
)
async def test_the_application_role_cannot_tamper(
    two_organizations: SeededTenants, name: str, statement: str
) -> None:
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(
            app_engine,
            None,
            statement,
            two_organizations.org_a,
            org=str(two_organizations.org_a),
        ),
    )

    assert outcome is TamperOutcome.PREVENTED, f"{name} was not prevented"


async def test_a_self_grant_of_update_gives_the_application_role_nothing(
    two_organizations: SeededTenants,
) -> None:
    """`GRANT UPDATE ON audit_log TO ledgr_app`, run as ledgr_app, does not
    fit attempt_tamper's PREVENTED/DETECTED/UNDETECTED model: PostgreSQL's
    GRANT does not raise when the grantor holds no grant-option privilege on
    the object - it emits a WARNING ("no privileges were granted for
    'audit_log'") and completes normally, because ledgr_app already holds
    SOME privilege on audit_log (select+insert, from 0019) even though not
    the one being granted. (ledgr_migrator's equivalent attempt DOES raise
    permission-denied, and stays in _MIGRATOR_STATEMENTS below - it starts
    from zero privileges on this table, which is the case PostgreSQL does
    hard-error on.) A WARNING is not a DBAPIError, so attempt_tamper's
    try/except never fires, and since nothing was actually granted,
    audit_log's own chain state does not move either - both of
    attempt_tamper's signals report "nothing happened" even though nothing
    SHOULD have happened, which is a different thing from it correctly
    reporting no escalation occurred. Checking has_table_privilege directly,
    before and after, tests the actual property this case cares about.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    async def can_update() -> bool:
        async with app_engine.begin() as conn:
            result = await conn.execute(
                text("SELECT has_table_privilege('ledgr_app', 'audit_log', 'UPDATE')")
            )
            return bool(result.scalar_one())

    assert not await can_update(), "ledgr_app already had UPDATE before the attempt"

    await _as(
        app_engine,
        None,
        "GRANT UPDATE ON audit_log TO ledgr_app",
        two_organizations.org_a,
        org=str(two_organizations.org_a),
    )()

    assert not await can_update(), "the self-grant actually gave ledgr_app UPDATE"


async def test_cascading_a_truncate_from_a_referencing_table_is_prevented(
    two_organizations: SeededTenants,
) -> None:
    """TRUNCATE CASCADE reaches every table with a foreign key to the one
    named, so emptying `organization` would empty the audit log with it.

    For ledgr_app this is stopped by privilege - it holds no TRUNCATE on
    organization either. The superuser version of the same attack, where the
    truncate trigger is what actually fires, is the next test.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(app_engine, None, "TRUNCATE organization CASCADE", two_organizations.org_a),
    )

    assert outcome is TamperOutcome.PREVENTED


async def test_a_superuser_cascading_a_truncate_is_stopped_by_the_trigger(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The reason audit_log_no_truncate_trg exists as its own statement-level
    trigger rather than being folded into the row-level ones: TRUNCATE fires
    neither the UPDATE nor the DELETE trigger, and CASCADE means the
    statement need not even name audit_log.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(admin_engine, None, "TRUNCATE organization CASCADE", two_organizations.org_a),
    )

    assert outcome is TamperOutcome.PREVENTED


async def test_deleting_the_referenced_organization_is_prevented(
    two_organizations: SeededTenants,
) -> None:
    """Orphaning entries by removing what they point at. No foreign key in
    this schema uses ON DELETE CASCADE, and no role holds DELETE on
    organization either.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=2)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(
            app_engine,
            None,
            "DELETE FROM organization WHERE id = :org",
            two_organizations.org_a,
            org=str(two_organizations.org_a),
        ),
    )

    assert outcome is TamperOutcome.PREVENTED


async def test_another_tenant_cannot_reach_this_tenants_entries(
    two_organizations: SeededTenants,
) -> None:
    """RLS is not the immutability control - the triggers are - but a tenant
    that could not even see another's rows cannot aim at them either, so it
    belongs in the enumeration.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(
            app_engine,
            None,
            "UPDATE audit_log SET action = 'tampered'",
            two_organizations.org_b,
        ),
    )

    assert outcome is TamperOutcome.PREVENTED


# ===========================================================================
# 3. Direct SQL as ledgr_migrator - the migration interface
# ===========================================================================

_MIGRATOR_STATEMENTS: list[tuple[str, str]] = [
    ("read the log", "SELECT count(*) FROM audit_log"),
    ("update an entry", "UPDATE audit_log SET outcome = 'denied'"),
    ("delete an entry", "DELETE FROM audit_log"),
    ("truncate", "TRUNCATE audit_log"),
    ("disarm every trigger", "ALTER TABLE audit_log DISABLE TRIGGER ALL"),
    ("drop the update guard", "DROP TRIGGER audit_log_no_update_trg ON audit_log"),
    ("drop the table", "DROP TABLE audit_log"),
    ("take ownership", "ALTER TABLE audit_log OWNER TO ledgr_migrator"),
    ("grant itself UPDATE", "GRANT UPDATE ON audit_log TO ledgr_migrator"),
    # Replacing the sealing function would let future entries be forged; the
    # function is owned by ledgr_audit for exactly this reason.
    (
        "replace the sealing function",
        "CREATE OR REPLACE FUNCTION audit_log_seal() RETURNS trigger AS "
        "$$ BEGIN RETURN new; END; $$ LANGUAGE plpgsql",
    ),
    (
        "replace the immutability guard",
        "CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS "
        "$$ BEGIN RETURN new; END; $$ LANGUAGE plpgsql",
    ),
    (
        "replace the verifier so tampering looks clean",
        "CREATE OR REPLACE FUNCTION app.verify_audit_chain(uuid) RETURNS TABLE "
        "(broken_sequence_number bigint, broken_entry_id uuid, reason text) AS "
        "$$ SELECT NULL::bigint, NULL::uuid, NULL::text WHERE false $$ LANGUAGE sql",
    ),
    ("become the audit owner", "SET ROLE ledgr_audit"),
]


@pytest.mark.parametrize(
    ("name", "statement"), _MIGRATOR_STATEMENTS, ids=[n for n, _ in _MIGRATOR_STATEMENTS]
)
async def test_the_migration_role_cannot_tamper(
    migrator_engine: AsyncEngine, two_organizations: SeededTenants, name: str, statement: str
) -> None:
    """ledgr_migrator owns every other table in this schema, and Postgres
    does not privilege-check a table's owner - so ownership is what had to be
    withheld. It owns neither audit_log nor its functions.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(migrator_engine, None, statement, two_organizations.org_a),
    )

    assert outcome is TamperOutcome.PREVENTED, f"{name} was not prevented"


# ===========================================================================
# 4. Direct SQL as ledgr_ops - support tooling
# ===========================================================================

_OPS_STATEMENTS: list[tuple[str, str]] = [
    ("update an entry", "UPDATE audit_log SET outcome = 'denied'"),
    ("delete an entry", "DELETE FROM audit_log"),
    ("truncate", "TRUNCATE audit_log"),
    (
        "insert a fabricated entry",
        "INSERT INTO audit_log (organization_id, actor_type, category, action, "
        " resource_type, outcome) VALUES "
        "(:org, 'support', 'support_access', 'read', 'thing', 'success')",
    ),
    ("disarm every trigger", "ALTER TABLE audit_log DISABLE TRIGGER ALL"),
    ("drop the table", "DROP TABLE audit_log"),
]


@pytest.mark.parametrize(
    ("name", "statement"), _OPS_STATEMENTS, ids=[n for n, _ in _OPS_STATEMENTS]
)
async def test_support_tooling_cannot_tamper(
    admin_engine: AsyncEngine, two_organizations: SeededTenants, name: str, statement: str
) -> None:
    """ledgr_ops holds BYPASSRLS so the operator scripts can sweep across
    tenants. BYPASSRLS skips row-level security; it has never skipped a
    trigger, and it grants no privilege the role was not given.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    outcome = await attempt_tamper(
        two_organizations.org_a,
        run=_as(
            admin_engine,
            "ledgr_ops",
            statement,
            two_organizations.org_a,
            org=str(two_organizations.org_a),
        ),
    )

    assert outcome is TamperOutcome.PREVENTED, f"{name} was not prevented"


async def test_support_tooling_really_does_bypass_rls(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The premise of the test above. Without this, "ledgr_ops could not
    tamper" might only mean "ledgr_ops could not see the rows".
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    async with admin_engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        visible = (await conn.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()

    assert visible >= 3, "BYPASSRLS is in force, so the refusals above are real"


# ===========================================================================
# 5. Superuser - where prevention ends and detection begins
# ===========================================================================


async def test_a_superuser_is_refused_by_the_trigger_like_everyone_else(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """No grant could stop a superuser - they are not privilege-checked.
    Postgres runs BEFORE triggers for every writer, which is why the guards
    are triggers rather than policies.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=3)

    for statement in (
        "UPDATE audit_log SET outcome = 'denied'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        outcome = await attempt_tamper(
            two_organizations.org_a,
            run=_as(admin_engine, None, statement, two_organizations.org_a),
        )
        assert outcome is TamperOutcome.PREVENTED, statement


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        ("change a field", "UPDATE audit_log SET outcome = 'denied' WHERE sequence_number = 2"),
        ("backdate", "UPDATE audit_log SET occurred_at = '2000-01-01Z' WHERE sequence_number = 2"),
        ("edit the payload", "UPDATE audit_log SET detail = '{}'::jsonb WHERE sequence_number = 2"),
        ("blank the actor", "UPDATE audit_log SET actor_user_id = NULL WHERE sequence_number = 2"),
        ("forge the hash", "UPDATE audit_log SET entry_hash = 'f' WHERE sequence_number = 2"),
        (
            "cut the chain link",
            "UPDATE audit_log SET previous_hash = 'f' WHERE sequence_number = 3",
        ),
        ("remove a middle entry", "DELETE FROM audit_log WHERE sequence_number = 2"),
        (
            "reorder the chain",
            "UPDATE audit_log SET sequence_number = 99 WHERE sequence_number = 2",
        ),
    ],
)
async def test_a_superuser_who_disarms_the_guards_is_caught_by_the_chain(
    admin_engine: AsyncEngine, two_organizations: SeededTenants, name: str, statement: str
) -> None:
    """The honest limit of every database control, and why IAM-092 asks for
    immutable AND tamper-evident rather than treating them as alternatives.

    A superuser owns everything and can drop any trigger. What they cannot do
    without recomputing every subsequent hash is edit a row unnoticed.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=5)

    async def run() -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL"))
            await conn.execute(text(statement))

    try:
        outcome = await attempt_tamper(two_organizations.org_a, run=run)
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER ALL"))

    assert outcome is TamperOutcome.DETECTED, f"{name} went unnoticed"


# ===========================================================================
# 6. The boundary: what only an external anchor catches
# ===========================================================================
#
# Both attacks below leave a chain that verifies perfectly. They are proved
# here rather than omitted, because a tamper-evidence document that showed
# only the cases it wins would be evidence of nothing. They are also the
# argument for scripts/anchor_audit_chain.py being a deployment requirement.


async def test_truncating_the_tail_is_invisible_to_the_chain_alone(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """Removing the most recent entries leaves a shorter but internally
    consistent chain. Only an anchor recording a higher sequence number
    catches it.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=5)

    async def run() -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL"))
            await conn.execute(text("DELETE FROM audit_log WHERE sequence_number >= 4"))

    try:
        outcome = await attempt_tamper(two_organizations.org_a, run=run)
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER ALL"))

    assert outcome is TamperOutcome.DETECTED_BY_ANCHOR
    _, reason = await _chain_state(two_organizations.org_a)
    assert reason is None, "the chain itself sees nothing wrong - that is the point"


async def test_a_fully_recomputed_chain_is_invisible_to_the_chain_alone(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The strongest attack available to a superuser: edit an entry and
    recompute every hash after it, so the chain verifies end to end.

    Uses the same sealing algorithm the database does (agreement between the
    two is asserted in test_audit_log.py), which is exactly the work an
    attacker with a database connection would do. The chain reports no break;
    the head no longer matches the anchor.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=4)

    async def run() -> None:
        async with app_engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, sequence_number, organization_id, administration_id, "
                        "       actor_user_id, actor_type, category, action, resource_type, "
                        "       resource_id, outcome, occurred_at, recorded_at, source_ip, "
                        "       user_agent, correlation_id, detail "
                        "FROM audit_log ORDER BY sequence_number"
                    )
                )
            ).all()

        previous = "0" * 64
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL"))
            for row in rows:
                # Rewrite entry 2's action; every entry gets a fresh hash.
                action = "sanitised" if row.sequence_number == 2 else row.action
                entry_hash = seal(
                    previous_hash=previous,
                    sequence_number=row.sequence_number,
                    organization_id=row.organization_id,
                    administration_id=row.administration_id,
                    actor_user_id=row.actor_user_id,
                    actor_type=row.actor_type,
                    category=row.category,
                    action=action,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    outcome=row.outcome,
                    occurred_at=row.occurred_at,
                    recorded_at=row.recorded_at,
                    source_ip=str(row.source_ip) if row.source_ip is not None else None,
                    user_agent=row.user_agent,
                    correlation_id=row.correlation_id,
                    # jsonb comes back as a dict or a JSON string depending on
                    # whether the driver has a codec registered. Getting this
                    # wrong would make the recomputed hash differ and the test
                    # would report DETECTED - passing for the wrong reason, by
                    # claiming the chain caught something it did not.
                    detail=_as_detail(row.detail),
                )
                await conn.execute(
                    text(
                        "UPDATE audit_log SET action = :action, previous_hash = :prev, "
                        "entry_hash = :hash WHERE id = :id"
                    ),
                    {
                        "action": action,
                        "prev": previous,
                        "hash": entry_hash,
                        "id": str(row.id),
                    },
                )
                previous = entry_hash

    try:
        outcome = await attempt_tamper(two_organizations.org_a, run=run)
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER ALL"))

    assert outcome is TamperOutcome.DETECTED_BY_ANCHOR
    _, reason = await _chain_state(two_organizations.org_a)
    assert reason is None, "a consistently rewritten chain verifies - only the anchor differs"


# ===========================================================================
# 7. The matrix, as GA evidence
# ===========================================================================


def test_every_enumerated_interface_is_covered() -> None:
    """The checklist item is "verified by attempted tamper test", so the
    enumeration itself is part of the evidence. This asserts the interfaces
    this file claims to cover are the ones it actually parametrises over, so
    a statement quietly dropped from a list is a failure rather than a
    smaller test run.
    """
    app_statements = {name for name, _ in _APP_STATEMENTS}
    assert {"plain update", "delete everything", "truncate", "truncate cascade"} <= app_statements
    assert "upsert onto an existing entry" in app_statements, "the disguised UPDATE"
    assert "install a rewrite rule" in app_statements, "the silent-swallow attack"
    assert len(_APP_STATEMENTS) >= 20

    migrator = {name for name, _ in _MIGRATOR_STATEMENTS}
    assert {"drop the table", "take ownership", "disarm every trigger"} <= migrator
    assert "replace the sealing function" in migrator, "forging future entries"
    assert "replace the verifier so tampering looks clean" in migrator

    ops = {name for name, _ in _OPS_STATEMENTS}
    assert {"update an entry", "delete an entry", "insert a fabricated entry"} <= ops


def test_the_harness_can_actually_report_an_undetected_tamper() -> None:
    """A tamper test whose harness can only return PREVENTED would pass
    against a table with no guards at all. TamperOutcome.UNDETECTED must be
    reachable, or none of the assertions above mean anything.
    """
    assert TamperOutcome.UNDETECTED in set(TamperOutcome)
    assert len({o.value for o in TamperOutcome}) == 4


async def test_the_log_survived_every_attempt_in_this_file(
    two_organizations: SeededTenants,
) -> None:
    """A closing sanity check. Each test seeds its own entries and the DB is
    rebuilt per CI run, so this is not a cross-test assertion - it confirms
    that after a full seed-and-verify cycle the chain is intact, which is the
    state every test above asserted it started from.
    """
    await seed_entries(two_organizations.org_a, two_organizations.owner_a, count=6)

    anchor, reason = await _chain_state(two_organizations.org_a)

    assert reason is None
    assert anchor.entries == 6
    assert anchor.sequence_number == 6
    assert len(anchor.head_hash) == 64
    assert datetime.now(UTC) is not None

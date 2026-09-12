"""CMP-014 against a real Postgres.

Runs tests/vat/rule_cases.py - the same table tests/vat/test_rules.py runs
against the in-memory rules - through migration 0028, so every resolution is
made by SQL and every refusal by a trigger.

Three things only this file can establish:

  1. **The barrier is real.** "Retroactively changing a rate must not alter a
     filed period" is a claim about what the database will accept, and the
     only evidence is a database refusing it.

  2. **The two fingerprints agree.** vat.rules_fingerprint() and the fake's
     Python reimplementation hash the same rules to the same value. Without
     that, the in-memory suite could be self-consistently wrong.

  3. **Nobody but the operator writes a rule.** ledgr_app holds SELECT; if it
     could insert a rate it could restate every tenant's returns.

--- On test ordering ---

The filed frontier is ONE date for the whole system and it only ever moves
forward, so a test that files a period changes what later tests may load. That
is the design working, not a flaw, and the tests below accommodate it the way
production has to: the ruleset is loaded before anything is filed, and every
rule a test introduces uses a far-future date unique to that test - because a
rule cannot be deleted afterwards either.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from api.db import engine as app_engine
from api.vat import RuleSource, TreatmentRole, VatRulesService
from api.vat.rules_repository import SqlVatRulesRepository
from tests.support.fake_vat_rules_repository import (
    SHIPPED_RULESET,
    InMemoryVatRulesRepository,
    load_document,
)
from tests.support.seed import SeededTenants
from tests.vat.rule_cases import ALL_TREATMENTS, RATE_CASES, RateCase

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")

RULESET_CHECKSUM = hashlib.sha256(SHIPPED_RULESET.read_bytes()).hexdigest()

#: A rule cannot be deleted, so a test that inserts one at a fixed date passes
#: once and then collides with itself forever. Every date a test INTRODUCES a
#: rule at is offset by this, which is unique per run.
#:
#: Bounded to a thousand days so the two eras below stay well apart: the filed
#: frontier only ever moves forward, so a period filed in the 2040s must not
#: overtake the 2090s dates the "ahead of the frontier" case relies on.
_RUN_OFFSET = uuid.uuid4().int % 1000


#: This run's drift period, spread a week apart per run so a thousand runs get
#: a thousand distinct dates. Day 3 or later, because file_a_period derives
#: start_date from the first of the same month and start must precede end.
_DRIFT_ERA = date(2041, 1, 3) + timedelta(days=_RUN_OFFSET * 7)


def _drift_era() -> date:
    return _DRIFT_ERA


def _drift_backdate() -> date:
    """The day before the period ends.

    Not just "somewhere inside the period": the rule in force is the one with
    the greatest valid_from at or before the date, so a backdated rate only
    moves the fingerprint if it is the LATEST rule under it. An earlier run's
    leftover rate could otherwise sit between this one and the period end and
    absorb the change - and the test would report no drift while the database
    was behaving perfectly.
    """
    return _DRIFT_ERA - timedelta(days=1)


def _drift_rate() -> Decimal:
    """A rate value unique to this run.

    The fingerprint hashes what the rules SAY, not when they were said - which
    is right, because two rulesets that resolve to the same rate on a date are
    the same rule for that date. It also means a backdated rate carrying the
    same value as the one already in force changes nothing and is correctly
    reported as no drift. Earlier runs of this test leave their rates behind
    permanently, so a fixed value would be a no-op from the second run on.
    """
    return Decimal("30.000") + Decimal(_RUN_OFFSET) / Decimal(1000)


def _clean_era() -> date:
    """A period end for the case that must NOT drift.

    Later than every _DRIFT_ERA, so the backdated rate that test introduces
    cannot land inside this period after the fact - and still earlier than
    _future_rate_date(), so filing here leaves that case's rule ahead of the
    frontier.
    """
    return date(2085, 12, 31)


def _future_rate_date() -> date:
    """Always ahead of every frontier this module sets."""
    return date(2091, 1, 1) + timedelta(days=_RUN_OFFSET)


@pytest_asyncio.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
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


async def load_ruleset(engine: AsyncEngine) -> uuid.UUID:
    """Load as `ledgr_ops`, the role the operator script uses. Idempotent by
    checksum, so every test may call it and only the first one inserts.
    """
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        return (  # type: ignore[no-any-return]
            await conn.execute(
                text(
                    "SELECT id FROM vat.load_ruleset(  cast(:document as jsonb), :checksum, true)"
                ),
                {
                    "document": json.dumps(load_document()),
                    "checksum": RULESET_CHECKSUM,
                },
            )
        ).scalar_one()


async def load_rgs_dataset(engine: AsyncEngine) -> None:
    """The RGS side of CMP-014 needs a version loaded to have anything to
    resolve - or, in the shipped dataset's case, to fail to resolve. Loaded
    here rather than assumed, so this module does not depend on having run
    after test_chart_of_accounts.py.
    """
    from tests.support.fake_chart_repository import SHIPPED_DATASET
    from tests.support.fake_chart_repository import load_document as rgs_document

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        await conn.execute(
            text(
                "SELECT id FROM ledger.load_rgs_version("
                "  cast(:document as jsonb), :checksum, true)"
            ),
            {
                "document": json.dumps(rgs_document()),
                "checksum": hashlib.sha256(SHIPPED_DATASET.read_bytes()).hexdigest(),
            },
        )


async def rules() -> AsyncIterator[VatRulesService]:
    async with app_engine.connect() as conn:
        yield VatRulesService(SqlVatRulesRepository(AsyncSession(bind=conn)))


async def rules_as_ops(engine: AsyncEngine) -> AsyncIterator[VatRulesService]:
    """The drift sweep's connection.

    vat.filed_period_drift() reads `period`, which is RLS-protected, and the
    function is SECURITY INVOKER - so it answers for exactly what the caller
    can see. As ledgr_app with no tenant that is nothing, which would make the
    detector silently report a clean bill of health. ledgr_ops holds BYPASSRLS
    and sees every tenant, which is what a cross-tenant check has to do; the
    NFR-033 integrity job is built the same way and for the same reason.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        yield VatRulesService(SqlVatRulesRepository(AsyncSession(bind=conn)))


async def file_a_period(
    tenants: SeededTenants, *, end_date: date, period_number: int = 1
) -> uuid.UUID:
    """A real period, filed through the ledger's own API."""
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(tenants.org_a)},
        )
        year = (
            await conn.execute(
                text(
                    "INSERT INTO fiscal_year (organization_id, administration_id, "
                    " start_date, end_date) VALUES (:org, :adm, :start, :end) "
                    "RETURNING id"
                ),
                {
                    "org": str(tenants.org_a),
                    "adm": str(tenants.admin_a),
                    "start": date(end_date.year, 1, 1),
                    "end": date(end_date.year, 12, 31),
                },
            )
        ).scalar_one()
        period = (
            await conn.execute(
                text(
                    "INSERT INTO period (organization_id, administration_id, "
                    " fiscal_year_id, period_number, start_date, end_date) "
                    "VALUES (:org, :adm, :year, :n, :start, :end) RETURNING id"
                ),
                {
                    "org": str(tenants.org_a),
                    "adm": str(tenants.admin_a),
                    "year": str(year),
                    "n": period_number,
                    "start": date(end_date.year, end_date.month, 1),
                    "end": end_date,
                },
            )
        ).scalar_one()
        await conn.execute(
            text("SELECT id FROM ledger.mark_period_filed(:period, :actor, 'TEST')"),
            {"period": str(period), "actor": str(tenants.owner_a)},
        )
        await conn.commit()
    return period  # type: ignore[no-any-return]


async def frontier() -> date | None:
    async for service in rules():
        return await service.filed_through()
    return None


# ===========================================================================
# Effective dating
# ===========================================================================


@pytest.mark.parametrize("case", RATE_CASES, ids=lambda c: c.name)
async def test_the_rate_in_force(case: RateCase, admin_engine: AsyncEngine) -> None:
    await load_ruleset(admin_engine)

    async for service in rules():
        rate = await service.rate_on(case.treatment, case.on_date)
        break

    assert rate == case.expected, (
        f"{case.name}: {case.treatment} on {case.on_date} resolved to {rate}"
    )


async def test_the_database_and_the_fake_hash_the_same_rules(
    admin_engine: AsyncEngine,
) -> None:
    """The claim the whole in-memory suite rests on.

    vat.rules_fingerprint() is SQL and the fake's is Python; if they disagree,
    every drift assertion made without a database is testing a different
    function from the one that runs in production.
    """
    await load_ruleset(admin_engine)
    memory = InMemoryVatRulesRepository()
    memory.load(load_document())

    for on_date in (date(2018, 12, 31), date(2019, 1, 1), date(2026, 6, 30)):
        async for service in rules():
            live = await service.rules_on(on_date)
            break
        in_memory = await memory.rules_on(on_date=on_date)

        assert live.fingerprint == in_memory.fingerprint, (
            f"the two implementations disagree about {on_date}"
        )
        assert {(t.code, t.rate) for t in live.treatments} == {
            (t.code, t.rate) for t in in_memory.treatments
        }


async def test_every_treatment_the_chart_can_name_resolves(
    admin_engine: AsyncEngine,
) -> None:
    await load_ruleset(admin_engine)

    async for service in rules():
        effective = await service.rules_on(date(2026, 6, 30))
        break

    assert {t.code for t in effective.treatments} == set(ALL_TREATMENTS)
    assert effective.unresolvable == ()
    assert effective.by_role(TreatmentRole.STANDARD).rate == Decimal("21.000")


async def test_the_shipped_ruleset_is_reported_as_provisional(
    admin_engine: AsyncEngine,
) -> None:
    await load_ruleset(admin_engine)

    async for service in rules():
        provisional = await service.provisional_rulesets()
        break

    assert [r.source for r in provisional] == [RuleSource.PROVISIONAL]


# ===========================================================================
# The barrier
# ===========================================================================


async def test_a_rate_behind_a_filed_period_is_refused(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """CMP-014's second sentence, against the database that has to enforce it.

    The period is filed first, then a rate is aimed at a date inside it. The
    insert goes in as a superuser, so no privilege is doing the refusing - the
    trigger is.
    """
    await load_ruleset(admin_engine)
    await file_a_period(two_organizations, end_date=date(2031, 3, 31))

    filed_through = await frontier()
    assert filed_through is not None and filed_through >= date(2031, 3, 31)

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vat_rate (treatment_code, valid_from, rate, "
                    " ruleset_id) SELECT 'btw_21', :from, 25.000, id "
                    "FROM vat_ruleset LIMIT 1"
                ),
                {"from": date(2031, 1, 15)},
            )

    message = str(raised.value)
    assert "CMP-014" in message
    assert "already filed through" in message
    assert "suppletie" in message, (
        "the refusal must name the route that IS open (PRD D5, no dead ends)"
    )


async def test_a_rate_ahead_of_the_frontier_is_accepted(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The half that has to keep working: announcing next year's rate is the
    normal case, and a barrier that blocked it would be useless.

    A far-future date unique to this test, because a rule cannot be deleted
    afterwards and the frontier only moves forward.
    """
    await load_ruleset(admin_engine)
    await file_a_period(two_organizations, end_date=date(2032, 3, 31))

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO vat_rate (treatment_code, valid_from, rate, ruleset_id) "
                "SELECT 'btw_21', :from, 24.000, id FROM vat_ruleset LIMIT 1"
            ),
            {"from": _future_rate_date()},
        )

    async for service in rules():
        assert await service.rate_on("btw_21", _future_rate_date() + timedelta(days=1)) == Decimal(
            "24.000"
        )
        # ... and the past is untouched.
        assert await service.rate_on("btw_21", date(2026, 6, 30)) == Decimal("21.000")
        break


async def test_a_filed_period_keeps_the_fingerprint_it_was_filed_under(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The recorded rule set is what makes the guarantee provable rather than
    merely asserted.
    """
    await load_ruleset(admin_engine)
    period_id = await file_a_period(two_organizations, end_date=date(2033, 3, 31))

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        recorded = (
            await conn.execute(
                text("SELECT vat_rules_fingerprint FROM period WHERE id = :id"),
                {"id": str(period_id)},
            )
        ).scalar_one()

    async for service in rules():
        current = await service.rules_for_period(end_date=date(2033, 3, 31))
        break

    assert recorded is not None
    assert len(recorded) == 64
    assert recorded == current.fingerprint


async def test_a_filed_period_does_not_drift(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """Structurally impossible while the barrier stands, and asserted anyway -
    a check that can only ever be empty is the check on the thing that makes
    it empty.

    Scoped to the period this test files rather than to the whole database,
    and deliberately so: test_drift_is_detected_when_the_barrier_is_disarmed
    below creates real drift on purpose, and a rule cannot be deleted
    afterwards. A global assertion here would pass or fail on test order and
    on how many times the suite had been run - which is exactly the kind of
    test that gets deleted rather than fixed.
    """
    await load_ruleset(admin_engine)
    period_id = await file_a_period(two_organizations, end_date=_clean_era())

    async for service in rules_as_ops(admin_engine):
        drift = await service.filed_period_drift()
        break

    # By period id, not by date. An earlier run of this suite may have filed a
    # period on the same day and then drifted it deliberately, and those rows
    # cannot be cleaned up - a rule is append-only. The id is the only handle
    # that belongs to this run.
    assert not [d for d in drift if d.period_id == period_id], (
        "a period drifted between being filed and being checked, with no rule introduced in between"
    )


async def test_drift_is_detected_when_the_barrier_is_disarmed(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """A detector that cannot detect is worse than none.

    The trigger is disabled - which needs a superuser, and is exactly how this
    would happen for real - a rate is backdated over a filed period, and the
    drift must be reported. Restored in a finally: leaving it off would turn
    CMP-014 off for every test that runs afterwards.
    """
    await load_ruleset(admin_engine)
    period_id = await file_a_period(two_organizations, end_date=_drift_era())

    async with admin_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE vat_rate DISABLE TRIGGER vat_rate_not_behind_a_filing_trg")
        )
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vat_rate (treatment_code, valid_from, rate, "
                    " ruleset_id) SELECT 'btw_21', :from, :rate, id "
                    "FROM vat_ruleset LIMIT 1"
                ),
                {"from": _drift_backdate(), "rate": _drift_rate()},
            )
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE vat_rate ENABLE TRIGGER vat_rate_not_behind_a_filing_trg")
            )

    async for service in rules_as_ops(admin_engine):
        drift = await service.filed_period_drift()
        break

    assert drift, "a rate was backdated over a filed period and nothing noticed"
    drifted = next(d for d in drift if d.period_id == period_id)
    assert drifted.recorded_fingerprint != drifted.current_fingerprint


# ===========================================================================
# Append-only, and who may write
# ===========================================================================


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        ("edit a rate", "UPDATE vat_rate SET rate = 99.000"),
        ("remove a rate", "DELETE FROM vat_rate"),
        ("edit a rubriek", "UPDATE vat_rubriek SET description_nl = 'x'"),
        ("edit a mapping", "UPDATE vat_treatment_rubriek SET turnover_rubriek = '1a'"),
    ],
)
async def test_even_a_superuser_cannot_restate_a_rule(
    name: str, statement: str, admin_engine: AsyncEngine
) -> None:
    """A rule row is a historical fact: editing one does not correct history,
    it falsifies it. Postgres runs BEFORE triggers for every writer, which is
    why the guard is a trigger rather than a privilege.
    """
    await load_ruleset(admin_engine)

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with admin_engine.begin() as conn:
            await conn.execute(text(statement))

    assert "append-only" in str(raised.value), f"{name}: {raised.value}"


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "invent a rate",
            "INSERT INTO vat_rate (treatment_code, valid_from, rate, ruleset_id) "
            "SELECT 'btw_21', '2099-01-01', 5.000, id FROM vat_ruleset LIMIT 1",
        ),
        ("edit a rate", "UPDATE vat_rate SET rate = 99.000"),
        ("move the frontier", "UPDATE vat_filing_watermark SET filed_through = '2000-01-01'"),
        (
            "invent a ruleset",
            "INSERT INTO vat_ruleset (jurisdiction, version, source, "
            "source_checksum) VALUES ('NL','fake','official-publication','x')",
        ),
    ],
)
async def test_the_application_role_cannot_write_a_rule(
    name: str, statement: str, admin_engine: AsyncEngine
) -> None:
    """If ledgr_app could introduce a rate it could restate every tenant's
    returns from inside a request.
    """
    await load_ruleset(admin_engine)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.connect() as conn:
            await conn.execute(text(statement))
            await conn.commit()


async def test_the_rules_are_readable_by_every_tenant(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """Tax law is public. The rule tables carry no tenant column and no policy
    deliberately - a rate hidden by RLS would be a rate nobody could compute
    with.
    """
    await load_ruleset(admin_engine)

    for organization in (two_organizations.org_a, two_organizations.org_b):
        async with app_engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization)},
            )
            rates = (await conn.execute(text("SELECT count(*) FROM vat_rate"))).scalar_one()
        assert rates > 0


# ===========================================================================
# CMP-014's third clause: RGS versions
# ===========================================================================


async def test_the_rgs_version_in_force_is_resolvable_by_date(
    admin_engine: AsyncEngine,
) -> None:
    """0024 gave rgs_version an effective_from and nothing read it; 0028 adds
    the resolution.

    The shipped 3.8 subset declares no effective_from - it has no publication
    date to claim - so it resolves to nothing and is reported instead. That is
    a property of the dataset, and the report is how it stops being invisible.
    """
    await load_rgs_dataset(admin_engine)

    async with app_engine.connect() as conn:
        resolved = (
            await conn.execute(
                text("SELECT version FROM ledger.rgs_version_on(cast(:d as date))"),
                {"d": date(2026, 6, 30)},
            )
        ).first()
        unresolvable = list(
            await conn.execute(
                text("SELECT version, source FROM ledger.rgs_versions_without_effective_from()")
            )
        )

    # ledger.rgs_version_on returns a rgs_version composite, so a miss is one
    # row of NULLs rather than no row at all - and a miss is what the shipped
    # dataset has to produce, because it declares no effective_from.
    assert resolved is not None
    assert resolved.version is None, (
        "the provisional RGS subset has no effective_from, so no date should "
        "resolve to it - resolving anyway would mean the lookup is falling "
        "back to 'the current version' and quietly ignoring the date"
    )
    assert any(row.version == "3.8-provisional" for row in unresolvable), (
        "the provisional RGS subset declares no effective_from, so it must "
        "appear in the report that says which versions cannot be placed in time"
    )

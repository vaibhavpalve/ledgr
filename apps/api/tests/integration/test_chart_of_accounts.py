"""The chart of accounts against a real Postgres (FR-GL-005, FR-ONB-004,
FR-ONB-005, CMP-003).

Runs tests/ledger/chart_cases.py - the same table tests/ledger/test_chart.py
runs against the in-memory chart - through migration 0024's triggers and
foreign keys, so every refusal is made by the database rather than by Python.

Three things only this file can establish:

  1. **The mapping rules bind every writer.** FR-ONB-005's "may not break RGS
     mapping integrity" is a claim about what the database will accept, and
     the only evidence is a database refusing it.

  2. **The reference data is not writable by the application.** If `ledgr_app`
     could insert a row into rgs_element, it could invent an RGS code and then
     map an account to it - satisfying every foreign key and breaking the
     requirement completely.

  3. **Keeping the fake honest.** A rule tests/support/fake_chart_repository.py
     enforces and 0024 does not - or the reverse - fails here.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from api.db import engine as app_engine
from api.ledger.chart import LegalForm, RgsSource, UpgradeResolution
from api.ledger.chart_repository import SqlChartRepository
from api.ledger.model import AccountType
from tests.ledger.chart_cases import (
    COMMON_ACCOUNTS,
    DISTINCTIVE_ACCOUNTS,
    MAPPING_CASES,
    MappingCase,
    successor,
)
from tests.support.fake_chart_repository import (
    LEGAL_FORM_ALIASES,
    SHIPPED_DATASET,
    load_document,
)
from tests.support.seed import SeededTenants

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")

#: The checksum the loader would compute. Any value works for a test - the
#: database only compares it against itself - but using the real one means the
#: row these tests create is the row `make load-rgs` would create.
import hashlib  # noqa: E402

DATASET_CHECKSUM = hashlib.sha256(SHIPPED_DATASET.read_bytes()).hexdigest()

#: A version name per scenario. Reusing one across tests fails on purpose:
#: ledger.load_rgs_version refuses a second document under a loaded version
#: name, because a published RGS release is frozen (CMP-003). That is the
#: behaviour test_a_version_cannot_be_reloaded_from_a_different_document
#: asserts, so everything else has to keep out of its way.
SUCCESSOR_VERSION = "4.0-integration-test"
RENAMED_VERSION = "4.1-renamed-test"
WITHDRAWN_VERSION = "4.2-withdrawn-test"


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


async def load_dataset(
    engine: AsyncEngine,
    document: dict[str, Any] | None = None,
    *,
    checksum: str = DATASET_CHECKSUM,
    publish: bool = True,
) -> uuid.UUID:
    """Load a version as `ledgr_ops`, the role the operator script uses.

    Idempotent by checksum, which is what lets every test call it: the second
    call returns the row the first one made rather than failing.
    """
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
        version_id = (
            await conn.execute(
                text(
                    "SELECT id FROM ledger.load_rgs_version("
                    "  cast(:document as jsonb), :checksum, true)"
                ),
                {
                    "document": json.dumps(document or load_document()),
                    "checksum": checksum,
                },
            )
        ).scalar_one()
        if publish:
            await conn.execute(
                text("SELECT version FROM ledger.publish_rgs_version(cast(:id as uuid))"),
                {"id": str(version_id)},
            )
    return version_id  # type: ignore[no-any-return]


async def seed_chart(
    tenants: SeededTenants, administration_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Seed as `ledgr_app` with tenant context set - the path a request takes."""
    administration_id = administration_id or tenants.admin_a
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT seeded_count, skipped_count, rgs_version_name, "
                    "       legal_form_code "
                    "FROM ledger.seed_chart_of_accounts(:admin, null, :actor, 'mkb')"
                ),
                {
                    "admin": str(administration_id),
                    "actor": str(tenants.owner_a),
                },
            )
        ).one()
        await conn.commit()
    return {
        "seeded": int(row.seeded_count),
        "skipped": int(row.skipped_count),
        "version": row.rgs_version_name,
        "legal_form": row.legal_form_code,
    }


async def chart_codes(tenants: SeededTenants) -> set[str]:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        result = await conn.execute(
            text("SELECT code FROM ledger.chart_of_accounts(:admin, true)"),
            {"admin": str(tenants.admin_a)},
        )
        return {row.code for row in result}


async def repository(organization_id: uuid.UUID) -> AsyncIterator[SqlChartRepository]:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        yield SqlChartRepository(AsyncSession(bind=conn))


# ===========================================================================
# FR-ONB-005: seeding
# ===========================================================================


async def test_seeding_produces_the_profile_for_the_captured_legal_form(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The seeded administrations carry legal_form 'BV' - the KvK's spelling,
    which is why legal_form_alias exists.
    """
    await load_dataset(admin_engine)

    result = await seed_chart(two_organizations)

    assert result["legal_form"] == LegalForm.BV.value
    assert result["seeded"] > 0
    assert result["skipped"] == 0
    assert result["version"] == load_document()["rgs_version"]

    codes = await chart_codes(two_organizations)
    assert set(COMMON_ACCOUNTS) <= codes
    assert set(DISTINCTIVE_ACCOUNTS["bv"]) <= codes


async def test_seeding_is_idempotent(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """FR-ONB-010 makes onboarding resumable, so this WILL be called twice."""
    await load_dataset(admin_engine)

    first = await seed_chart(two_organizations)
    second = await seed_chart(two_organizations)

    assert second["seeded"] == 0
    assert second["skipped"] == first["seeded"]


async def test_every_seeded_account_carries_a_complete_rgs_reference(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """FR-GL-005's four fields, and the composite foreign key that makes the
    RGS code mean something: (rgs_version_id, rgs_code) resolves to a real
    element whose account_type matches the account's.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        rows = list(
            await conn.execute(
                text(
                    "SELECT a.code, a.account_type, a.rgs_code, a.status, "
                    "       e.account_type AS element_type, e.is_postable "
                    "FROM ledger_account a "
                    "LEFT JOIN rgs_element e "
                    "  ON e.rgs_version_id = a.rgs_version_id AND e.code = a.rgs_code "
                    "WHERE a.administration_id = :admin"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        )

    assert rows
    for row in rows:
        assert row.rgs_code is not None, f"{row.code} was seeded unmapped"
        assert row.element_type == row.account_type, (
            f"{row.code} is {row.account_type} but its element is {row.element_type}"
        )
        assert row.is_postable
        assert row.status == "active"


async def test_a_legal_form_with_no_profile_is_refused(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    await load_dataset(admin_engine)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        other = (
            await conn.execute(
                text(
                    "INSERT INTO administration (organization_id, legal_name, legal_form) "
                    "VALUES (:org, 'Onbekend N.V.', 'Naamloze Vennootschap') RETURNING id"
                ),
                {"org": str(two_organizations.org_a)},
            )
        ).scalar_one()
        await conn.commit()

        # The commit ended the transaction, and set_config(..., true) is
        # transaction-local - so the tenant context set above is gone. Without
        # setting it again the definer function reads `administration` under
        # RLS with no tenant and reports "does not exist", which is a correct
        # refusal for the wrong reason and would hide the one under test.
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT seeded_count FROM ledger.seed_chart_of_accounts("
                    "  :admin, null, null, 'mkb')"
                ),
                {"admin": str(other)},
            )
            await conn.commit()

        assert "FR-ONB-004" in str(raised.value)
        assert "Naamloze Vennootschap" in str(raised.value)
        await conn.rollback()


# ===========================================================================
# FR-ONB-005: the mapping-integrity table
# ===========================================================================


@pytest.mark.parametrize("case", MAPPING_CASES, ids=lambda c: c.name)
async def test_mapping_integrity(
    case: MappingCase, admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )

        statement = text(
            "SELECT id, rgs_code, rgs_version_id FROM ledger.create_account("
            "  p_administration_id => :admin, p_code => :code, p_name => 'Test',"
            "  p_account_type => :account_type, p_rgs_code => :rgs_code)"
        )
        params = {
            "admin": str(two_organizations.admin_a),
            "code": case.account_code,
            "account_type": case.account_type.value,
            "rgs_code": case.rgs_code,
        }

        if case.is_accepted:
            row = (await conn.execute(statement, params)).one()
            await conn.commit()
            assert row.rgs_code == case.rgs_code
            # The pair is complete or empty, never half - and the version is
            # the administration's, not the caller's.
            assert (row.rgs_version_id is None) == (case.rgs_code is None)
            return

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(statement, params)
            await conn.commit()

        assert case.expect is not None
        assert case.expect in str(raised.value), (
            f"{case.name} ({case.requirement}): refused, but not for the reason "
            f"under test. Expected {case.expect!r}, got: {raised.value}"
        )
        await conn.rollback()


async def test_an_account_cannot_be_mapped_before_the_chart_is_pinned(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """The version is derived from the administration's pin, so there is
    nothing to derive it from yet. Refusing is right: an account mapped before
    a version was chosen would have to guess one.
    """
    await load_dataset(admin_engine)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT id FROM ledger.create_account("
                    "  :admin, '1000', 'Kas', 'asset', 'BLimKas')"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
            await conn.commit()

        assert "has no RGS version" in str(raised.value)
        await conn.rollback()

    # An UNMAPPED account is still fine before seeding - "may extend" does not
    # depend on a chart having been seeded first.
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        created = (
            await conn.execute(
                text(
                    "SELECT id, rgs_version_id FROM ledger.create_account("
                    "  :admin, '9100', 'Eigen rekening', 'expense')"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        ).one()
        await conn.commit()
    assert created.rgs_version_id is None


async def test_the_caller_cannot_choose_the_rgs_version(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """Derive-don't-trust, the same rule journal_entry_validate() applies to
    entry numbers. `ledger.create_account` has no parameter for it, so this
    reaches past the API and writes the column directly, as a superuser - and
    the trigger simply overwrites what it was told with the administration's
    pinned version.

    Note what is asserted: not that the statement fails, but that it cannot
    take effect. ledger_account is a mutable table - status and rgs_code are
    meant to change - so the trigger normalises the row rather than rejecting
    it. A caller naming a version is not refused, it is ignored, which is the
    stronger property: there is no input that can put an account on a version
    its administration is not using.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)
    successor_id = await load_dataset(
        admin_engine,
        successor(load_document(), version=SUCCESSOR_VERSION),
        checksum="successor",
        publish=False,
    )

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE ledger_account SET rgs_version_id = :other "
                " WHERE administration_id = :admin AND code = '1000'"
            ),
            {
                "other": str(successor_id),
                "admin": str(two_organizations.admin_a),
            },
        )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        pinned = (
            await conn.execute(
                text(
                    "SELECT a.rgs_version_id = p.rgs_version_id AS agrees "
                    "FROM ledger_account a "
                    "JOIN administration_rgs_version p "
                    "  ON p.administration_id = a.administration_id "
                    "WHERE a.administration_id = :admin AND a.code = '1000'"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        ).one()
    assert pinned.agrees


# ===========================================================================
# The reference data is not the application's to write
# ===========================================================================


_APP_STATEMENTS: list[tuple[str, str]] = [
    (
        "invent an RGS element",
        "INSERT INTO rgs_element (rgs_version_id, code, description_nl, level, "
        " is_postable, account_type) SELECT id, 'BFake', 'Verzonnen', 3, true, "
        " 'asset' FROM rgs_version LIMIT 1",
    ),
    ("retype an element", "UPDATE rgs_element SET account_type = 'revenue'"),
    ("remove an element", "DELETE FROM rgs_element"),
    ("edit a profile", "UPDATE rgs_profile_account SET account_code = '9999'"),
    (
        "add a profile row",
        "INSERT INTO rgs_profile_account (rgs_version_id, "
        " legal_form, account_code, rgs_code, name_nl) SELECT id, 'bv', '9999', "
        " 'BLimKas', 'x' FROM rgs_version LIMIT 1",
    ),
    ("rewrite a mapping", "UPDATE rgs_code_mapping SET to_code = 'BLimKas'"),
    ("publish a version", "UPDATE rgs_version SET status = 'current'"),
    ("remove a version", "DELETE FROM rgs_version"),
    (
        "repin an administration",
        "UPDATE administration_rgs_version SET rgs_version_id "
        " = (SELECT id FROM rgs_version LIMIT 1)",
    ),
]


@pytest.mark.parametrize(
    ("name", "statement"), _APP_STATEMENTS, ids=[n for n, _ in _APP_STATEMENTS]
)
async def test_the_application_role_cannot_write_the_reference_data(
    name: str,
    statement: str,
    admin_engine: AsyncEngine,
    two_organizations: SeededTenants,
) -> None:
    """The load-bearing grant. If `ledgr_app` could add an rgs_element it
    could invent a code and map an account to it, satisfying every foreign key
    and breaking FR-ONB-005 completely.

    `administration_rgs_version` is the exception that proves it: the app CAN
    write it, through the seeding and upgrade functions, so the statement in
    the list is a direct UPDATE - which it holds no privilege for.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(text(statement))
            await conn.commit()


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        ("retype an element", "UPDATE rgs_element SET account_type = 'revenue'"),
        ("remove an element", "DELETE FROM rgs_element"),
        ("truncate the elements", "TRUNCATE rgs_element"),
        ("rewrite a profile", "UPDATE rgs_profile_account SET rgs_code = 'BLimKas'"),
        ("remove a version", "DELETE FROM rgs_version"),
    ],
)
async def test_even_a_superuser_is_refused_by_the_append_only_triggers(
    name: str,
    statement: str,
    admin_engine: AsyncEngine,
    two_organizations: SeededTenants,
) -> None:
    """A published RGS version is a fixed publication. If an element's
    account_type could be edited after accounts map to it, every one of those
    accounts would silently change meaning - and the XAF export and the SBR
    filing would both move with nothing recording that they had.

    Postgres runs BEFORE triggers for every writer, which is why the guards
    are triggers rather than privileges.
    """
    await load_dataset(admin_engine)

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with admin_engine.begin() as conn:
            await conn.execute(text(statement))

    message = str(raised.value)
    # TRUNCATE on a table something references is refused by the foreign key
    # before the statement-level trigger is reached. A different guard, the
    # same outcome - and worth allowing for explicitly rather than loosening
    # the assertion to "something went wrong".
    expected = ("append-only", "CMP-003", "referenced in a foreign key constraint")
    assert any(fragment in message for fragment in expected), message


async def test_a_version_cannot_be_reloaded_from_a_different_document(
    admin_engine: AsyncEngine,
) -> None:
    await load_dataset(admin_engine)

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        await load_dataset(admin_engine, checksum="something-else")

    assert "already loaded" in str(raised.value)
    assert "frozen" in str(raised.value)


async def test_the_provisional_dataset_needs_an_explicit_flag(
    admin_engine: AsyncEngine,
) -> None:
    """The gate that stops the starter file quietly becoming a filing basis."""
    document = {**load_document(), "rgs_version": "9.9-gate-test"}

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with admin_engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE ledgr_ops"))
            await conn.execute(
                text(
                    "SELECT id FROM ledger.load_rgs_version(  cast(:document as jsonb), 'x', false)"
                ),
                {"document": json.dumps(document)},
            )

    assert "provisional subset" in str(raised.value)
    assert "CMP-002" in str(raised.value)


async def test_the_alias_table_matches_the_one_the_fake_uses(
    two_organizations: SeededTenants,
) -> None:
    """tests/support/fake_chart_repository.py duplicates legal_form_alias's
    rows so the in-memory suite can resolve a legal form without a database.
    This is what keeps the copy honest.
    """
    async with app_engine.connect() as conn:
        rows = list(await conn.execute(text("SELECT alias, legal_form FROM legal_form_alias")))

    assert {row.alias: row.legal_form for row in rows} == LEGAL_FORM_ALIASES


# ===========================================================================
# CLAUDE.md rule 1: tenancy
# ===========================================================================


async def test_another_tenant_cannot_read_this_chart(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """A chart of accounts names what a business does. RLS scopes it, and the
    reference data it points at is deliberately NOT scoped - RGS is a public
    taxonomy and every tenant reads the same rows.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        theirs = list(
            await conn.execute(
                text("SELECT code FROM ledger.chart_of_accounts(:admin, true)"),
                {"admin": str(two_organizations.admin_a)},
            )
        )
        pins = list(
            await conn.execute(
                text(
                    "SELECT administration_id FROM administration_rgs_version "
                    "WHERE administration_id = :admin"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        )
        # ... while the taxonomy itself is readable by everyone.
        elements = (await conn.execute(text("SELECT count(*) FROM rgs_element"))).scalar_one()

    assert theirs == []
    assert pins == []
    assert elements > 0


# ===========================================================================
# CMP-003: the upgrade path
# ===========================================================================


async def test_an_upgrade_remaps_the_chart_and_moves_the_pin(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)
    version_id = await load_dataset(
        admin_engine,
        successor(
            load_document(),
            version=RENAMED_VERSION,
            changes={"WBedKan": ("WBedKanKan", "renamed")},
        ),
        checksum="successor-renamed",
        publish=False,
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        plan = list(
            await conn.execute(
                text(
                    "SELECT account_code, resolution, to_rgs_code "
                    "FROM ledger.plan_rgs_upgrade(:admin, :version)"
                ),
                {
                    "admin": str(two_organizations.admin_a),
                    "version": str(version_id),
                },
            )
        )
        assert plan
        assert not [
            row for row in plan if row.resolution == UpgradeResolution.NEEDS_A_DECISION.value
        ]

        result = (
            await conn.execute(
                text(
                    "SELECT remapped_count, rgs_version_name "
                    "FROM ledger.apply_rgs_upgrade(:admin, :version, :actor)"
                ),
                {
                    "admin": str(two_organizations.admin_a),
                    "version": str(version_id),
                    "actor": str(two_organizations.owner_a),
                },
            )
        ).one()
        await conn.commit()

        # set_config(..., true) is transaction-local and the commit above ended
        # the transaction, so the tenant context has to be re-established
        # before reading the result back - otherwise RLS hides the chart and
        # the assertion fails with "no row" rather than a wrong value.
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )

        office = (
            await conn.execute(
                text(
                    "SELECT rgs_code, rgs_version_name "
                    "FROM ledger.chart_of_accounts(:admin, true) WHERE code = '4100'"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        ).one()
        deviations = list(
            await conn.execute(
                text("SELECT deviation FROM ledger.rgs_mapping_deviations(:admin)"),
                {"admin": str(two_organizations.admin_a)},
            )
        )

    assert result.rgs_version_name == RENAMED_VERSION
    assert result.remapped_count > 0
    assert office.rgs_code == "WBedKanKan"
    assert office.rgs_version_name == RENAMED_VERSION
    assert deviations == [], "an upgrade must leave no account behind its pin"


async def test_a_withdrawn_code_blocks_the_upgrade(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)
    version_id = await load_dataset(
        admin_engine,
        successor(
            load_document(),
            version=WITHDRAWN_VERSION,
            changes={"WBedKan": (None, "withdrawn")},
        ),
        checksum="successor-withdrawn",
        publish=False,
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        blocked = list(
            await conn.execute(
                text(
                    "SELECT account_code, detail FROM ledger.plan_rgs_upgrade("
                    "  :admin, :version) WHERE resolution = 'needs_a_decision'"
                ),
                {
                    "admin": str(two_organizations.admin_a),
                    "version": str(version_id),
                },
            )
        )
        assert {row.account_code for row in blocked} == {"4100"}

        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "SELECT remapped_count FROM ledger.apply_rgs_upgrade(  :admin, :version, null)"
                ),
                {
                    "admin": str(two_organizations.admin_a),
                    "version": str(version_id),
                },
            )
            await conn.commit()

        assert "need a decision" in str(raised.value)
        await conn.rollback()


async def test_readiness_reports_the_provisional_dataset(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT legal_form_code, version_source, is_provisional, "
                    "       accounts_total, accounts_unmapped "
                    "FROM ledger.rgs_readiness(:admin)"
                ),
                {"admin": str(two_organizations.admin_a)},
            )
        ).one()

    assert row.legal_form_code == "bv"
    assert row.version_source == RgsSource.PROVISIONAL.value
    assert row.is_provisional
    assert row.accounts_total > 0
    assert row.accounts_unmapped == 0


async def test_the_options_offered_are_the_ones_the_trigger_accepts(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """PRD D5, no dead ends: ledger.rgs_options() and
    ledger_account_rgs_integrity() have to agree, or a picker offers choices
    that fail on save.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        offered = list(
            await conn.execute(
                text("SELECT code FROM ledger.rgs_options(:admin, 'expense')"),
                {"admin": str(two_organizations.admin_a)},
            )
        )
        assert offered

        for index, row in enumerate(offered):
            created = (
                await conn.execute(
                    text(
                        "SELECT rgs_code FROM ledger.create_account("
                        "  :admin, :code, 'Test', 'expense', :rgs_code)"
                    ),
                    {
                        "admin": str(two_organizations.admin_a),
                        # 95xx: outside every range the MKB profile seeds, so
                        # the loop tests the picker rather than colliding with
                        # ledger_account_code_unique on a seeded account.
                        "code": f"95{index:02d}",
                        "rgs_code": row.code,
                    },
                )
            ).one()
            assert created.rgs_code == row.code
        await conn.rollback()


async def test_the_repository_reads_what_the_functions_return(
    admin_engine: AsyncEngine, two_organizations: SeededTenants
) -> None:
    """SqlChartRepository against the real functions: column names, enum
    values and the jsonb-free shapes all have to line up, and a rename in a
    migration should fail here rather than at runtime.
    """
    await load_dataset(admin_engine)
    await seed_chart(two_organizations)

    async for repo in repository(two_organizations.org_a):
        chart = await repo.chart(administration_id=two_organizations.admin_a, include_blocked=True)
        current = await repo.current_version()
        pinned = await repo.pinned_version(administration_id=two_organizations.admin_a)
        options = await repo.rgs_options(
            administration_id=two_organizations.admin_a,
            account_type=AccountType.REVENUE,
        )
        readiness = await repo.readiness(administration_id=two_organizations.admin_a)

        assert chart and all(account.is_mapped for account in chart)
        assert current is not None and current.is_provisional
        assert pinned is not None and pinned.version == current.version
        assert options and all(e.account_type is AccountType.REVENUE for e in options)
        assert readiness and readiness[0].legal_form is LegalForm.BV
        assert not readiness[0].ready_to_file
        break

"""FR-ONB-006 against a real Postgres.

Runs tests/ledger/fiscal_cases.py - the same table tests/ledger/test_fiscal.py
runs against the pure Python derivation - through migration 0029's SQL, and
asserts the two agree row for row.

That agreement is the point of this file. Two implementations exist because
neither can borrow the other's answer: the database derives periods inside the
transaction that creates the year, and an onboarding screen has to show them
before anything is written. Comparing them is what stops one drifting.

What only a database can establish is the rest: that a year and its periods
are created atomically, that years and periods cannot overlap, and that a
one-day period is now storable - which needed 0029 to relax a check 0001
wrote.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from api.db import engine as app_engine
from api.ledger.fiscal import PeriodScheme, derive_periods
from api.ledger.fiscal_repository import SqlFiscalYearRepository
from tests.ledger.fiscal_cases import FISCAL_YEAR_CASES, INVALID_YEARS, FiscalYearCase
from tests.support.seed import SeededTenants

_ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "")


@pytest_asyncio.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_ADMIN_URL is not set")
    engine = create_async_engine(_ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        yield engine
    finally:
        await engine.dispose()


async def repository(organization_id: uuid.UUID) -> AsyncIterator[SqlFiscalYearRepository]:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(organization_id)},
        )
        yield SqlFiscalYearRepository(AsyncSession(bind=conn))


async def open_year_committed(
    tenants: SeededTenants,
    administration: uuid.UUID,
    *,
    start_date: date,
    end_date: date,
    scheme: PeriodScheme = PeriodScheme.MONTHLY,
) -> uuid.UUID:
    """Open a year and COMMIT it.

    `repository()` above is a generator, and `async for ... break` throws
    GeneratorExit at the yield - so anything after it, a commit included, never
    runs. That is fine for the read-only cases and wrong for anything a later
    connection has to see, which is every test about two years colliding.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(tenants.org_a)},
        )
        year = await SqlFiscalYearRepository(AsyncSession(bind=conn)).open_year(
            administration_id=administration,
            start_date=start_date,
            end_date=end_date,
            scheme=scheme,
            actor_user_id=tenants.owner_a,
        )
        await conn.commit()
    return year.id


async def fresh_administration(tenants: SeededTenants, name: str) -> uuid.UUID:
    """A second administration in org A.

    Each test that opens a year needs one of its own: 0029 forbids overlapping
    fiscal years per administration, and `two_organizations` hands every test
    the same admin_a which other fixtures have already given a 2026 year.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(tenants.org_a)},
        )
        administration = (
            await conn.execute(
                text(
                    "INSERT INTO administration (organization_id, legal_name, "
                    " legal_form) VALUES (:org, :name, 'BV') RETURNING id"
                ),
                {"org": str(tenants.org_a), "name": name},
            )
        ).scalar_one()
        await conn.commit()
    return administration  # type: ignore[no-any-return]


# ===========================================================================
# The derivation, and the agreement between the two implementations
# ===========================================================================


@pytest.mark.parametrize("case", FISCAL_YEAR_CASES, ids=lambda c: c.name)
async def test_the_database_derives_the_expected_periods(
    case: FiscalYearCase, two_organizations: SeededTenants
) -> None:
    async for repo in repository(two_organizations.org_a):
        derived = await repo.derive(
            start_date=case.start_date, end_date=case.end_date, scheme=case.scheme
        )
        break

    assert tuple((p.period_number, p.start_date, p.end_date) for p in derived) == case.expected


@pytest.mark.parametrize("case", FISCAL_YEAR_CASES, ids=lambda c: c.name)
async def test_the_database_and_python_derive_the_same_periods(
    case: FiscalYearCase, two_organizations: SeededTenants
) -> None:
    """The claim the whole pure-Python suite rests on. If these disagree, the
    periods a user is shown before creating a year are not the periods they
    get.
    """
    async for repo in repository(two_organizations.org_a):
        from_sql = await repo.derive(
            start_date=case.start_date, end_date=case.end_date, scheme=case.scheme
        )
        break
    from_python = derive_periods(case.start_date, case.end_date, case.scheme)

    assert list(from_sql) == list(from_python)


# ===========================================================================
# Opening a year
# ===========================================================================


async def test_opening_a_year_creates_it_with_its_periods(
    two_organizations: SeededTenants,
) -> None:
    administration = await fresh_administration(two_organizations, "Kalenderjaar BV")

    async for repo in repository(two_organizations.org_a):
        year = await repo.open_year(
            administration_id=administration,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 12, 31),
            scheme=PeriodScheme.MONTHLY,
            actor_user_id=two_organizations.owner_a,
        )
        periods = await repo.periods_of(fiscal_year_id=year.id)
        deviations = await repo.coverage_deviations(administration_id=administration)
        break

    assert year.period_scheme is PeriodScheme.MONTHLY
    assert year.is_calendar_year
    assert not year.is_short_year
    assert len(periods) == 12
    assert periods[0].start_date == date(2030, 1, 1)
    assert periods[-1].end_date == date(2030, 12, 31)
    assert deviations == []


async def test_a_short_first_year_end_to_end(
    two_organizations: SeededTenants,
) -> None:
    """FR-ONB-006's named case, through the function an onboarding flow calls."""
    administration = await fresh_administration(two_organizations, "Kort Boekjaar BV")

    async for repo in repository(two_organizations.org_a):
        year = await repo.open_year(
            administration_id=administration,
            start_date=date(2030, 3, 15),
            end_date=date(2030, 12, 31),
            scheme=PeriodScheme.MONTHLY,
            actor_user_id=two_organizations.owner_a,
        )
        periods = await repo.periods_of(fiscal_year_id=year.id)
        deviations = await repo.coverage_deviations(administration_id=administration)
        break

    assert year.is_short_year
    assert not year.is_calendar_year
    assert len(periods) == 10
    assert periods[0].start_date == date(2030, 3, 15)
    assert periods[0].end_date == date(2030, 3, 31)
    assert periods[0].days == 17
    assert periods[0].is_stub
    assert not periods[1].is_stub
    assert deviations == []


async def test_a_non_calendar_year_end_to_end(
    two_organizations: SeededTenants,
) -> None:
    administration = await fresh_administration(two_organizations, "Gebroken Boekjaar BV")

    async for repo in repository(two_organizations.org_a):
        year = await repo.open_year(
            administration_id=administration,
            start_date=date(2030, 7, 1),
            end_date=date(2031, 6, 30),
            scheme=PeriodScheme.QUARTERLY,
            actor_user_id=two_organizations.owner_a,
        )
        periods = await repo.periods_of(fiscal_year_id=year.id)
        break

    assert not year.is_calendar_year
    assert not year.is_short_year
    assert len(periods) == 4
    assert periods[0].start_date == date(2030, 7, 1)
    assert periods[0].end_date == date(2030, 9, 30)
    assert periods[-1].end_date == date(2031, 6, 30)


async def test_a_year_starting_on_a_month_end_stores_a_one_day_period(
    two_organizations: SeededTenants,
) -> None:
    """0001 wrote `check (end_date > start_date)` on period, which made this
    year unrepresentable. 0029 relaxed it to `>=` rather than merging the stub
    forward, because a merged period would straddle two calendar months and
    could not be filed as a VAT period.
    """
    administration = await fresh_administration(two_organizations, "Laatste Dag BV")

    async for repo in repository(two_organizations.org_a):
        year = await repo.open_year(
            administration_id=administration,
            start_date=date(2030, 3, 31),
            end_date=date(2030, 12, 31),
            scheme=PeriodScheme.MONTHLY,
            actor_user_id=two_organizations.owner_a,
        )
        periods = await repo.periods_of(fiscal_year_id=year.id)
        break

    assert periods[0].start_date == periods[0].end_date == date(2030, 3, 31)
    assert periods[0].days == 1


@pytest.mark.parametrize(
    ("name", "start", "end", "expect"), INVALID_YEARS, ids=[c[0] for c in INVALID_YEARS]
)
async def test_a_span_that_is_not_a_fiscal_year_is_refused(
    name: str, start: date, end: date, expect: str, two_organizations: SeededTenants
) -> None:
    """The same messages the Python validation raises, so a caller cannot tell
    which layer refused - and does not need to.
    """
    administration = await fresh_administration(
        two_organizations, f"Ongeldig {uuid.uuid4().hex[:8]} BV"
    )

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async for repo in repository(two_organizations.org_a):
            await repo.open_year(
                administration_id=administration,
                start_date=start,
                end_date=end,
                scheme=PeriodScheme.MONTHLY,
                actor_user_id=two_organizations.owner_a,
            )
            break

    assert expect in str(raised.value), f"{name}: {raised.value}"


# ===========================================================================
# Years and periods may not overlap
# ===========================================================================


async def test_two_fiscal_years_cannot_overlap(
    two_organizations: SeededTenants,
) -> None:
    """0001's `unique (administration_id, start_date)` stops two years sharing
    a start date and permits one year to sit on top of another. A posting in
    the overlap would belong to two fiscal years at once.
    """
    administration = await fresh_administration(two_organizations, "Overlap BV")
    await open_year_committed(
        two_organizations,
        administration,
        start_date=date(2030, 1, 1),
        end_date=date(2030, 12, 31),
    )

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        await open_year_committed(
            two_organizations,
            administration,
            start_date=date(2030, 7, 1),
            end_date=date(2031, 6, 30),
        )

    assert "fiscal_year_no_overlap" in str(raised.value)


async def test_consecutive_years_are_adjacent_not_overlapping(
    two_organizations: SeededTenants,
) -> None:
    """The other side of the constraint: a year ending on the 31st and the next
    starting on the 1st must be allowed, which is what makes the half-open
    daterange the right encoding.
    """
    administration = await fresh_administration(two_organizations, "Opeenvolgend BV")

    async for repo in repository(two_organizations.org_a):
        first = await repo.open_year(
            administration_id=administration,
            start_date=date(2030, 1, 1),
            end_date=date(2030, 12, 31),
            scheme=PeriodScheme.MONTHLY,
            actor_user_id=two_organizations.owner_a,
        )
        second = await repo.open_year(
            administration_id=administration,
            start_date=date(2031, 1, 1),
            end_date=date(2031, 12, 31),
            scheme=PeriodScheme.MONTHLY,
            actor_user_id=two_organizations.owner_a,
        )
        years = await repo.years(administration_id=administration)
        break

    assert first.end_date + (second.start_date - first.end_date) == second.start_date
    assert [y.start_date for y in years] == [date(2030, 1, 1), date(2031, 1, 1)]


async def test_periods_cannot_overlap_within_an_administration(
    two_organizations: SeededTenants,
) -> None:
    """`period` has been INSERT-able by the application role since 0001, so the
    constraint is what stops a hand-written period landing on top of a derived
    one - and a posting belonging to two periods at once (FR-GL-002).
    """
    administration = await fresh_administration(two_organizations, "Periodeoverlap BV")
    year_id = await open_year_committed(
        two_organizations,
        administration,
        start_date=date(2030, 1, 1),
        end_date=date(2030, 12, 31),
    )

    with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
        async with app_engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO period (organization_id, administration_id, "
                    " fiscal_year_id, period_number, start_date, end_date) "
                    "VALUES (:org, :adm, :year, 99, '2030-02-10', '2030-02-20')"
                ),
                {
                    "org": str(two_organizations.org_a),
                    "adm": str(administration),
                    "year": str(year_id),
                },
            )
            await conn.commit()

    assert "period_no_overlap" in str(raised.value)


# ===========================================================================
# The check on the derivation
# ===========================================================================


async def test_a_year_assembled_by_hand_with_a_gap_is_reported(
    two_organizations: SeededTenants,
) -> None:
    """`ledger.open_fiscal_year` cannot produce this. A hand-written year can,
    because 0001 granted the application INSERT on both tables - so the
    coverage check exists for the same reason 0020's gap report does.
    """
    administration = await fresh_administration(two_organizations, "Handmatig BV")

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(two_organizations.org_a)},
        )
        year = (
            await conn.execute(
                text(
                    "INSERT INTO fiscal_year (organization_id, administration_id, "
                    " start_date, end_date) VALUES (:org, :adm, "
                    " '2030-01-01', '2030-12-31') RETURNING id"
                ),
                {"org": str(two_organizations.org_a), "adm": str(administration)},
            )
        ).scalar_one()
        # January only: eleven months of the year belong to no period at all.
        await conn.execute(
            text(
                "INSERT INTO period (organization_id, administration_id, "
                " fiscal_year_id, period_number, start_date, end_date) "
                "VALUES (:org, :adm, :year, 1, '2030-01-01', '2030-01-31')"
            ),
            {
                "org": str(two_organizations.org_a),
                "adm": str(administration),
                "year": str(year),
            },
        )
        await conn.commit()

    async for repo in repository(two_organizations.org_a):
        deviations = await repo.coverage_deviations(administration_id=administration)
        break

    assert len(deviations) == 1
    assert deviations[0].deviation == "last_period_short"
    assert "2030-12-31" in deviations[0].detail


async def test_a_year_with_no_periods_at_all_is_reported(
    two_organizations: SeededTenants,
) -> None:
    """Nothing can be posted into it: FR-GL-004 makes every posting name a
    period.
    """
    administration = await fresh_administration(two_organizations, "Leeg Boekjaar BV")

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text(
                "INSERT INTO fiscal_year (organization_id, administration_id, "
                " start_date, end_date) VALUES (:org, :adm, "
                " '2030-01-01', '2030-12-31')"
            ),
            {"org": str(two_organizations.org_a), "adm": str(administration)},
        )
        await conn.commit()

    async for repo in repository(two_organizations.org_a):
        deviations = await repo.coverage_deviations(administration_id=administration)
        break

    assert [d.deviation for d in deviations] == ["no_periods"]


async def test_every_derived_year_tiles_exactly(
    two_organizations: SeededTenants,
) -> None:
    """The coverage check over every case in the table, opened for real.

    One administration per case, because years may not overlap - which is
    itself the constraint under test in the file above.
    """
    for index, case in enumerate(FISCAL_YEAR_CASES):
        administration = await fresh_administration(two_organizations, f"Dekking {index} BV")
        async for repo in repository(two_organizations.org_a):
            year = await repo.open_year(
                administration_id=administration,
                start_date=case.start_date,
                end_date=case.end_date,
                scheme=case.scheme,
                actor_user_id=two_organizations.owner_a,
            )
            periods = await repo.periods_of(fiscal_year_id=year.id)
            deviations = await repo.coverage_deviations(administration_id=administration)
            break

        assert deviations == [], f"{case.name}: {deviations}"
        assert len(periods) == len(case.expected), case.name

"""FR-ONB-006 against the pure derivation.

Runs tests/ledger/fiscal_cases.py - the same table
tests/integration/test_fiscal_year.py runs against migration 0029's SQL - plus
the properties that have to hold for spans nobody wrote a case for.

The case table pins the answers a reader can check by eye. The property tests
below cover the ones nobody would think to write: a year is a span of two
dates, and the derivation has to tile it for every span, not just the eleven
that made it into the table.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.authz.matrix import ROLES, permissions_for_role
from api.authz.service import AuthorizationService
from api.ledger.fiscal import (
    MANAGE_FISCAL_YEAR,
    MAX_YEAR_MONTHS,
    DerivedPeriod,
    FiscalYear,
    FiscalYearService,
    FiscalYearStatus,
    InvalidFiscalYear,
    NotAuthorizedToDefineYear,
    PeriodScheme,
    derive_periods,
    validate_fiscal_year,
)
from tests.authz.helpers import build_world
from tests.ledger.fiscal_cases import FISCAL_YEAR_CASES, INVALID_YEARS, FiscalYearCase
from tests.support.fake_audit_repository import InMemoryAuditRepository

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)

#: A span wide enough to cross year, leap-year and quarter boundaries.
DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2032, 12, 31))
SCHEMES = st.sampled_from(list(PeriodScheme))


# ===========================================================================
# The case table
# ===========================================================================


@pytest.mark.parametrize("case", FISCAL_YEAR_CASES, ids=lambda c: c.name)
def test_the_periods_a_year_derives_to(case: FiscalYearCase) -> None:
    derived = derive_periods(case.start_date, case.end_date, case.scheme)

    assert tuple((p.period_number, p.start_date, p.end_date) for p in derived) == case.expected


@pytest.mark.parametrize(
    ("name", "start", "end", "expect"), INVALID_YEARS, ids=[c[0] for c in INVALID_YEARS]
)
def test_a_span_that_is_not_a_fiscal_year(name: str, start: date, end: date, expect: str) -> None:
    with pytest.raises(InvalidFiscalYear) as raised:
        validate_fiscal_year(start, end)

    assert expect in str(raised.value), f"{name}: {raised.value}"


def test_a_short_first_year_is_recognisable_as_one() -> None:
    """FR-ONB-006 names short first years specifically, so "is this one" has
    to be answerable without counting periods.
    """

    def year(start: date, end: date) -> FiscalYear:
        return FiscalYear(
            id=uuid.uuid4(),
            administration_id=uuid.uuid4(),
            start_date=start,
            end_date=end,
            period_scheme=PeriodScheme.MONTHLY,
            status=FiscalYearStatus.OPEN,
        )

    assert year(date(2026, 3, 15), date(2026, 12, 31)).is_short_year
    assert not year(date(2026, 1, 1), date(2026, 12, 31)).is_short_year
    assert not year(date(2026, 7, 1), date(2027, 6, 30)).is_short_year
    assert not year(date(2026, 11, 1), date(2027, 12, 31)).is_short_year

    assert year(date(2026, 1, 1), date(2026, 12, 31)).is_calendar_year
    assert not year(date(2026, 7, 1), date(2027, 6, 30)).is_calendar_year


# ===========================================================================
# The properties, for every span nobody wrote a case for
# ===========================================================================


@given(start=DATES, length=st.integers(min_value=1, max_value=730), scheme=SCHEMES)
@SETTINGS
def test_the_periods_always_tile_the_year(start: date, length: int, scheme: PeriodScheme) -> None:
    """The one property everything else rests on: no gap, no overlap, and the
    edges are the year's own.

    A gap is a date that belongs to no period, which FR-GL-004 makes
    unpostable. An overlap is a date that belongs to two, which makes "exactly
    one period" (FR-GL-002) false.
    """
    end = start + timedelta(days=length)
    periods = derive_periods(start, end, scheme)

    assert periods, "a span of at least one day derives at least one period"
    assert periods[0].start_date == start
    assert periods[-1].end_date == end

    for earlier, later in zip(periods, periods[1:], strict=False):
        assert later.start_date == earlier.end_date + timedelta(days=1), (
            "consecutive periods are adjacent: no gap, no overlap"
        )

    assert sum(p.days for p in periods) == (end - start).days + 1


@given(start=DATES, length=st.integers(min_value=1, max_value=730), scheme=SCHEMES)
@SETTINGS
def test_every_period_lies_inside_one_calendar_block(
    start: date, length: int, scheme: PeriodScheme
) -> None:
    """The property VAT filing depends on.

    A period is also the unit a return is filed for (0021, CMP-014), and Dutch
    returns are filed for calendar months and calendar quarters. A period that
    straddled two of them could not be filed as either - which is exactly what
    fiscal quarters counted from a May year start would produce.
    """
    periods = derive_periods(start, end := start + timedelta(days=length), scheme)
    assert periods[-1].end_date == end

    for period in periods:
        assert period.start_date.year == period.end_date.year
        if scheme is PeriodScheme.MONTHLY:
            assert period.start_date.month == period.end_date.month
        else:
            assert (period.start_date.month - 1) // 3 == (period.end_date.month - 1) // 3


@given(start=DATES, length=st.integers(min_value=1, max_value=730), scheme=SCHEMES)
@SETTINGS
def test_period_numbers_are_one_to_n_without_gaps(
    start: date, length: int, scheme: PeriodScheme
) -> None:
    """`period_unique_number` is a unique constraint, not a contiguity one, so
    the derivation is what makes the sequence dense.
    """
    periods = derive_periods(start, end := start + timedelta(days=length), scheme)
    assert periods[-1].end_date == end

    assert [p.period_number for p in periods] == list(range(1, len(periods) + 1))


@given(start=DATES, length=st.integers(min_value=1, max_value=730), scheme=SCHEMES)
@SETTINGS
def test_only_the_first_and_last_period_can_be_partial(
    start: date, length: int, scheme: PeriodScheme
) -> None:
    """Everything in the middle is a whole calendar block. A stub anywhere else
    would mean the walk lost its alignment partway through - the failure mode
    that a case table of eleven examples would very likely miss.
    """
    periods = derive_periods(start, start + timedelta(days=length), scheme)

    for period in periods[1:-1]:
        assert period.start_date.day == 1
        last_day = calendar.monthrange(period.end_date.year, period.end_date.month)[1]
        assert period.end_date.day == last_day


@given(start=DATES, scheme=SCHEMES)
@SETTINGS
def test_a_single_day_year_derives_a_single_day_period(start: date, scheme: PeriodScheme) -> None:
    """Not reachable through the service - validate_fiscal_year refuses a year
    that does not end after it starts - but the derivation is a separate,
    total function and has to behave for the degenerate input rather than
    loop or return nothing.
    """
    periods = derive_periods(start, start, scheme)

    assert periods == (DerivedPeriod(1, start, start),)
    assert periods[0].days == 1


def test_the_derivation_refuses_a_backwards_span() -> None:
    with pytest.raises(InvalidFiscalYear):
        derive_periods(date(2026, 12, 31), date(2026, 1, 1), PeriodScheme.MONTHLY)


# ===========================================================================
# The service: who may define a year, and what it records
# ===========================================================================


@dataclass
class InMemoryFiscalYearRepository:
    """Stores what open_fiscal_year would, deriving with the same function the
    migration's SQL mirrors.
    """

    organization_id: uuid.UUID
    #: Named for storage, not for the protocol method: a dataclass field and a
    #: method of the same name collide, and the instance attribute wins - so
    #: `await repo.years(...)` would try to call a list.
    stored_years: list[FiscalYear] = field(default_factory=list)
    periods: dict[uuid.UUID, tuple[DerivedPeriod, ...]] = field(default_factory=dict)

    async def open_year(
        self,
        *,
        administration_id: uuid.UUID,
        start_date: date,
        end_date: date,
        scheme: PeriodScheme,
        actor_user_id: uuid.UUID | None,
    ) -> FiscalYear:
        year = FiscalYear(
            id=uuid.uuid4(),
            administration_id=administration_id,
            start_date=start_date,
            end_date=end_date,
            period_scheme=scheme,
            status=FiscalYearStatus.OPEN,
        )
        # The year and its periods together - separately is a state nothing
        # else can read, since every posting names a period.
        self.periods[year.id] = derive_periods(start_date, end_date, scheme)
        self.stored_years.append(year)
        return year

    async def years(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return [y for y in self.stored_years if y.administration_id == administration_id]

    async def periods_of(self, *, fiscal_year_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.periods.get(fiscal_year_id, ())

    async def coverage_deviations(self, *, administration_id):  # type: ignore[no-untyped-def]
        return []

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID:
        return self.organization_id


def _service(role: str = "Owner") -> tuple[FiscalYearService, object, AuditLog]:
    authz = build_world()
    role_definition = next(r for r in ROLES if r.name == role)
    authz.repository.assign(
        user_id=authz.user,
        role=role,
        scope_id=(authz.acme if role_definition.scope_type == "organization" else authz.acme_books),
    )
    audit = AuditLog(InMemoryAuditRepository())
    repository = InMemoryFiscalYearRepository(organization_id=authz.acme)
    return (
        FiscalYearService(repository, AuthorizationService(authz.repository), audit),
        authz,
        audit,
    )


def test_appendix_a_places_the_fiscal_year_capability() -> None:
    """Pinned because FR-ONB-006 rides on "Year-end close" - Appendix A's only
    fiscal-year row - rather than on a permission invented for it. If the PRD
    grows one, this is the test that says where to change.
    """
    holders = {r.name for r in ROLES if MANAGE_FISCAL_YEAR in set(permissions_for_role(r))}
    assert holders == {"Owner", "Accountant"}


@pytest.mark.parametrize("role", sorted(r.name for r in ROLES))
async def test_only_roles_holding_the_capability_may_open_a_year(role: str) -> None:
    service, authz, _ = _service(role)
    allowed = MANAGE_FISCAL_YEAR in set(
        permissions_for_role(next(r for r in ROLES if r.name == role))
    )

    if allowed:
        year = await service.open_year(
            administration_id=authz.acme_books,
            actor_user_id=authz.user,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        assert year.is_calendar_year
    else:
        with pytest.raises(NotAuthorizedToDefineYear):
            await service.open_year(
                administration_id=authz.acme_books,
                actor_user_id=authz.user,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )


async def test_opening_a_year_is_audited() -> None:
    """IAM-090. A fiscal year is configuration: every later figure is reported
    inside the boundaries it sets, so a change to one with nobody's name on it
    is what an audit log is for.
    """
    service, authz, audit = _service()

    await service.open_year(
        administration_id=authz.acme_books,
        actor_user_id=authz.user,
        start_date=date(2026, 3, 15),
        end_date=date(2026, 12, 31),
    )

    entries = await audit.search(organization_id=authz.acme)
    opened = next(e for e in entries if e.action == "open_fiscal_year")
    assert opened.category is AuditCategory.CONFIGURATION
    assert opened.outcome is AuditOutcome.SUCCESS
    assert opened.detail["periods"] == 10
    assert opened.detail["is_short_year"] is True
    assert opened.detail["period_scheme"] == "monthly"


async def test_previewing_a_year_needs_no_administration() -> None:
    """Onboarding shows the periods before the year exists to authorize
    against - it is arithmetic on two dates and touches nothing.
    """
    service, _, audit = _service("Viewer")

    periods = service.preview(
        start_date=date(2026, 7, 1),
        end_date=date(2027, 6, 30),
        scheme=PeriodScheme.QUARTERLY,
    )

    assert len(periods) == 4
    assert await audit.search(organization_id=uuid.uuid4()) == []


async def test_the_years_of_an_administration_come_back_in_order() -> None:
    """Exercises the protocol method as well as the answer: a repository whose
    `years` is a stored list rather than a method passes every other test here
    and fails only this one.
    """
    service, authz, _ = _service()
    for start, end in (
        (date(2027, 1, 1), date(2027, 12, 31)),
        (date(2026, 3, 15), date(2026, 12, 31)),
    ):
        await service.open_year(
            administration_id=authz.acme_books,
            actor_user_id=authz.user,
            start_date=start,
            end_date=end,
        )

    years = await service.years(administration_id=authz.acme_books, actor_user_id=authz.user)

    assert [y.start_date for y in years] == [date(2027, 1, 1), date(2026, 3, 15)]
    assert [y.is_short_year for y in years] == [False, True]

    periods = await service.periods_of(fiscal_year_id=years[1].id)
    assert len(periods) == 10


async def test_the_service_refuses_a_span_that_is_not_a_year() -> None:
    service, authz, _ = _service()

    with pytest.raises(InvalidFiscalYear):
        await service.open_year(
            administration_id=authz.acme_books,
            actor_user_id=authz.user,
            start_date=date(2026, 1, 1),
            end_date=date(2030, 1, 1),
        )


def test_the_maximum_year_is_stated_once() -> None:
    """The 24-month ceiling is a Dutch rule about long first book years, and
    the SQL in 0029 repeats it. Pinned here so a change to one is visibly a
    change to a shared number.
    """
    assert MAX_YEAR_MONTHS == 24
    validate_fiscal_year(date(2026, 1, 1), date(2028, 1, 1))
    with pytest.raises(InvalidFiscalYear):
        validate_fiscal_year(date(2026, 1, 1), date(2028, 1, 2))

"""Fiscal year definition and period derivation: FR-ONB-006 (PRD §6.1).

    FR-ONB-006  Fiscal year definition, including non-calendar and short first
                years.

--- The derivation ---

Walk from the year's start, taking whole calendar blocks - months or quarters -
and truncating the first and last to the year's actual bounds. That one rule
produces every case the requirement names:

    2026-01-01 .. 2026-12-31  monthly    twelve whole months
    2026-07-01 .. 2027-06-30  monthly    twelve months, July to June
    2026-03-15 .. 2026-12-31  monthly    a 17-day stub, then nine whole months
    2026-05-01 .. 2027-04-30  quarterly  five periods, two of them stubs

`derive_periods` here and `ledger.derive_fiscal_periods` in migration 0029 are
two implementations of it, and tests/ledger/fiscal_cases.py runs the same table
against both. That is deliberate rather than redundant: the database has to
derive periods inside the transaction that creates the year, and an onboarding
screen has to show them before anything is written. Neither can borrow the
other's answer, so the answers are compared instead.

--- Why blocks are calendar-aligned ---

The obvious alternative for quarterly is fiscal quarters counted from the
year's own start - a May year giving May-Jul, Aug-Oct, Nov-Jan, Feb-Apr, and
exactly four periods.

It is wrong in this schema. `period.status` includes 'vat_filed' and CMP-014
keys a filing's rule fingerprint on the period end date, so a period is also
the unit a VAT return is filed for - and Dutch VAT returns are filed for
calendar months and calendar quarters whatever the fiscal year does. A
May-July period straddles two calendar quarters and cannot be filed as either.

So every period lies inside exactly one calendar month or quarter, and the
visible cost is that a quarterly year starting in May has five periods rather
than four. That is the year genuinely straddling calendar quarters; showing it
beats hiding it behind a period nobody can file.
"""

from __future__ import annotations

import calendar
import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.ledger.model import LedgerError

#: Appendix A, "Year-end close": Full for Owner and Accountant, and the only
#: fiscal-year capability the matrix has. Defining a year rides on it, which is
#: a wider grant than opening one deserves and still the right audience -
#: inventing a permission the PRD does not name is what ADR-012 forbids. See
#: ADR-028.
MANAGE_FISCAL_YEAR = ("close", "fiscal_year")

#: The Netherlands permits a long first book year of up to about two years.
#: Anything beyond it is a typo, not a policy.
MAX_YEAR_MONTHS = 24


class PeriodScheme(enum.Enum):
    """How a year is divided. Per YEAR, not per administration: a business
    that moves from quarterly to monthly filing does so from a date, and the
    years already closed keep the periods they were kept in.
    """

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class FiscalYearStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class FiscalYearError(LedgerError):
    """Base for this module's refusals."""


class InvalidFiscalYear(FiscalYearError):
    """The dates do not describe a year anything could be posted into."""


class NotAuthorizedToDefineYear(FiscalYearError):
    def __init__(self, action: str, resource_type: str, detail: str) -> None:
        self.action = action
        self.resource_type = resource_type
        self.detail = detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")


@dataclass(frozen=True, slots=True)
class DerivedPeriod:
    """One period, before it exists."""

    period_number: int
    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def is_stub(self) -> bool:
        """Shorter than the calendar block it sits in - the first or last
        period of a year that does not begin or end on a block boundary.
        """
        return not (
            self.start_date.day == 1
            and self.end_date.day == calendar.monthrange(self.end_date.year, self.end_date.month)[1]
        )


@dataclass(frozen=True, slots=True)
class FiscalYear:
    id: uuid.UUID
    administration_id: uuid.UUID
    start_date: date
    end_date: date
    period_scheme: PeriodScheme
    status: FiscalYearStatus = FiscalYearStatus.OPEN
    created_at: datetime | None = None

    @property
    def months(self) -> int:
        return (
            (self.end_date.year - self.start_date.year) * 12
            + self.end_date.month
            - self.start_date.month
            + 1
        )

    @property
    def is_calendar_year(self) -> bool:
        return (
            self.start_date.month == 1
            and self.start_date.day == 1
            and self.end_date.month == 12
            and self.end_date.day == 31
        )

    @property
    def is_short_year(self) -> bool:
        """FR-ONB-006's "short first year": anything under twelve months.

        Named on the year rather than inferred by callers, because "is this a
        short year" is asked by reporting, by the year-end close, and by
        anyone reading a trial balance that covers nine months.
        """
        return self.end_date < _add_months(self.start_date, 12) - _ONE_DAY


@dataclass(frozen=True, slots=True)
class CoverageDeviation:
    """A fiscal year whose periods do not tile it.

    Structurally impossible for a year opened through the service, and
    reported anyway: `period` has been INSERT-able by the application role
    since 0001, so a hand-assembled year is still reachable.
    """

    fiscal_year_id: uuid.UUID
    administration_id: uuid.UUID
    deviation: str
    detail: str


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------

_ONE_DAY = date(2000, 1, 2) - date(2000, 1, 1)


def _add_months(anchor: date, months: int) -> date:
    """Anchor shifted by whole months, clamped to the target month's length -
    so 31 January plus one month is 28 (or 29) February rather than an error.
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _block_end(day: date, scheme: PeriodScheme) -> date:
    """The last day of the calendar block `day` falls in."""
    # The last month of the block: the quarter's third month, or the day's own.
    month = ((day.month - 1) // 3) * 3 + 3 if scheme is PeriodScheme.QUARTERLY else day.month
    return date(day.year, month, calendar.monthrange(day.year, month)[1])


def derive_periods(
    start_date: date, end_date: date, scheme: PeriodScheme = PeriodScheme.MONTHLY
) -> tuple[DerivedPeriod, ...]:
    """The periods a fiscal year is divided into.

    Pure: no database, no tenant, no clock. It is the piece worth testing
    exhaustively, and it is what an onboarding screen calls to show a year's
    periods before committing to it.

    A one-day period is a legitimate result - a year beginning on the last day
    of a month produces one - and migration 0029 relaxed period's date check to
    allow it. Merging that stub into the following period was the alternative
    and is worse: the merged period would straddle two calendar months and
    could not be filed as a VAT period.
    """
    if end_date < start_date:
        raise InvalidFiscalYear(f"a fiscal year ends after it starts: {start_date} .. {end_date}")

    periods: list[DerivedPeriod] = []
    cursor = start_date
    number = 1
    while cursor <= end_date:
        finish = min(_block_end(cursor, scheme), end_date)
        periods.append(DerivedPeriod(number, cursor, finish))
        cursor = finish + _ONE_DAY
        number += 1
    return tuple(periods)


def validate_fiscal_year(start_date: date, end_date: date) -> None:
    """The two rules a year has to satisfy before its periods are worth
    deriving. Separate from `derive_periods` so a preview can show the periods
    for a span the caller has not committed to yet.
    """
    if end_date <= start_date:
        raise InvalidFiscalYear(
            f"a fiscal year ends after it starts: {start_date} .. {end_date} (FR-ONB-006)"
        )
    if end_date > _add_months(start_date, MAX_YEAR_MONTHS):
        raise InvalidFiscalYear(
            f"a fiscal year of {start_date} .. {end_date} is longer than "
            f"{MAX_YEAR_MONTHS} months; a long first book year is permitted, an "
            "arbitrary one is not (FR-ONB-006)"
        )


class FiscalYearRepository(Protocol):
    async def open_year(
        self,
        *,
        administration_id: uuid.UUID,
        start_date: date,
        end_date: date,
        scheme: PeriodScheme,
        actor_user_id: uuid.UUID | None,
    ) -> FiscalYear: ...

    async def years(self, *, administration_id: uuid.UUID) -> Sequence[FiscalYear]: ...

    async def periods_of(self, *, fiscal_year_id: uuid.UUID) -> Sequence[DerivedPeriod]: ...

    async def coverage_deviations(
        self, *, administration_id: uuid.UUID | None
    ) -> Sequence[CoverageDeviation]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class FiscalYearService:
    """FR-ONB-006's entry point.

    The year and its periods are created together, in one transaction, by
    `ledger.open_fiscal_year`. Separately, an administration could be left
    holding a year with no periods - which nothing else in this schema knows
    how to read, because every posting names a period (FR-GL-004).
    """

    def __init__(
        self,
        repository: FiscalYearRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log

    def preview(
        self,
        *,
        start_date: date,
        end_date: date,
        scheme: PeriodScheme = PeriodScheme.MONTHLY,
    ) -> tuple[DerivedPeriod, ...]:
        """What opening this year would produce. No authorization, because it
        touches nothing and names no administration - it is arithmetic on two
        dates, and onboarding needs it before a year exists to be authorized
        against.
        """
        validate_fiscal_year(start_date, end_date)
        return derive_periods(start_date, end_date, scheme)

    async def open_year(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        start_date: date,
        end_date: date,
        scheme: PeriodScheme = PeriodScheme.MONTHLY,
        correlation_id: str | None = None,
    ) -> FiscalYear:
        await self._require(
            MANAGE_FISCAL_YEAR,
            user_id=actor_user_id,
            administration_id=administration_id,
        )
        # Checked here for a precise error and again by the database, which is
        # what makes it true for any writer.
        validate_fiscal_year(start_date, end_date)

        year = await self._repository.open_year(
            administration_id=administration_id,
            start_date=start_date,
            end_date=end_date,
            scheme=scheme,
            actor_user_id=actor_user_id,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="open_fiscal_year",
            resource_id=year.id,
            correlation_id=correlation_id,
            detail={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "period_scheme": scheme.value,
                "periods": len(derive_periods(start_date, end_date, scheme)),
                "is_short_year": year.is_short_year,
                "is_calendar_year": year.is_calendar_year,
            },
        )
        return year

    async def years(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Sequence[FiscalYear]:
        await self._require(
            MANAGE_FISCAL_YEAR,
            user_id=actor_user_id,
            administration_id=administration_id,
        )
        return await self._repository.years(administration_id=administration_id)

    async def periods_of(self, *, fiscal_year_id: uuid.UUID) -> Sequence[DerivedPeriod]:
        return await self._repository.periods_of(fiscal_year_id=fiscal_year_id)

    async def coverage_deviations(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[CoverageDeviation]:
        """Years whose periods do not tile them. An operator sweep, so no user
        is required - the same shape the NFR-033 job has.
        """
        return await self._repository.coverage_deviations(administration_id=administration_id)

    # -- internals ---------------------------------------------------------

    async def _require(
        self,
        permission: tuple[str, str],
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> None:
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToDefineYear(
                action, resource_type, decision.detail or decision.reason
            )

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, object] | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise FiscalYearError(f"administration {administration_id} does not exist")
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                # A fiscal year is configuration: every later figure is
                # reported inside the boundaries it sets.
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="fiscal_year",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=dict(detail or {}),
            )
        )

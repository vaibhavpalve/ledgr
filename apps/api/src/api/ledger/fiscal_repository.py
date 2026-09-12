"""SQLAlchemy-backed FiscalYearRepository over migration 0029 (FR-ONB-006).

Every write goes through `ledger.open_fiscal_year`, which creates the year and
its periods in one statement. `ledgr_app` holds INSERT on both tables from
0001, so this is a convention here rather than a privilege boundary - but the
atomicity is not: a year without periods is a state nothing else in this schema
can read, since every posting names a period (FR-GL-004).

Note what is absent: no method deletes a fiscal year or a period, and none
rewrites a period's dates. A year is closed, never removed, and re-deriving the
periods of a year that already has postings would move those postings between
periods without touching them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.ledger.fiscal import (
    CoverageDeviation,
    DerivedPeriod,
    FiscalYear,
    FiscalYearStatus,
    PeriodScheme,
)

_YEAR_COLUMNS = """
    id, administration_id, start_date, end_date, period_scheme, status, created_at
"""


def _year(row: Any) -> FiscalYear:
    return FiscalYear(
        id=row.id,
        administration_id=row.administration_id,
        start_date=row.start_date,
        end_date=row.end_date,
        period_scheme=PeriodScheme(row.period_scheme),
        status=FiscalYearStatus(row.status),
        created_at=row.created_at,
    )


class SqlFiscalYearRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_year(
        self,
        *,
        administration_id: uuid.UUID,
        start_date: date,
        end_date: date,
        scheme: PeriodScheme,
        actor_user_id: uuid.UUID | None,
    ) -> FiscalYear:
        result = await self._session.execute(
            text(
                f"SELECT {_YEAR_COLUMNS} FROM ledger.open_fiscal_year("
                "  p_administration_id => :administration_id,"
                "  p_start_date        => cast(:start_date as date),"
                "  p_end_date          => cast(:end_date as date),"
                "  p_period_scheme     => :scheme,"
                "  p_actor_user_id     => cast(:actor as uuid))"
            ),
            {
                "administration_id": str(administration_id),
                "start_date": start_date,
                "end_date": end_date,
                "scheme": scheme.value,
                "actor": str(actor_user_id) if actor_user_id else None,
            },
        )
        return _year(result.one())

    async def years(self, *, administration_id: uuid.UUID) -> Sequence[FiscalYear]:
        result = await self._session.execute(
            text(
                f"SELECT {_YEAR_COLUMNS} FROM fiscal_year "
                "WHERE administration_id = :administration_id "
                "ORDER BY start_date"
            ),
            {"administration_id": str(administration_id)},
        )
        return [_year(row) for row in result]

    async def periods_of(self, *, fiscal_year_id: uuid.UUID) -> Sequence[DerivedPeriod]:
        result = await self._session.execute(
            text(
                "SELECT period_number, start_date, end_date FROM period "
                "WHERE fiscal_year_id = :fiscal_year_id ORDER BY period_number"
            ),
            {"fiscal_year_id": str(fiscal_year_id)},
        )
        return [
            DerivedPeriod(
                period_number=int(row.period_number),
                start_date=row.start_date,
                end_date=row.end_date,
            )
            for row in result
        ]

    async def derive(
        self, *, start_date: date, end_date: date, scheme: PeriodScheme
    ) -> Sequence[DerivedPeriod]:
        """The database's own derivation, which is what
        tests/ledger/fiscal_cases.py compares the Python one against.

        Not used by the service - `preview()` computes it locally, because
        showing somebody the periods of a year they are still choosing should
        not need a round trip.
        """
        result = await self._session.execute(
            text(
                "SELECT period_number, start_date, end_date "
                "FROM ledger.derive_fiscal_periods("
                "  cast(:start_date as date), cast(:end_date as date), :scheme)"
            ),
            {"start_date": start_date, "end_date": end_date, "scheme": scheme.value},
        )
        return [
            DerivedPeriod(
                period_number=int(row.period_number),
                start_date=row.start_date,
                end_date=row.end_date,
            )
            for row in result
        ]

    async def coverage_deviations(
        self, *, administration_id: uuid.UUID | None
    ) -> Sequence[CoverageDeviation]:
        result = await self._session.execute(
            text(
                "SELECT fiscal_year_id, administration_id, deviation, detail "
                "FROM ledger.fiscal_year_coverage_deviations("
                "  cast(:administration_id as uuid))"
            ),
            {"administration_id": (str(administration_id) if administration_id else None)},
        )
        return [
            CoverageDeviation(
                fiscal_year_id=row.fiscal_year_id,
                administration_id=row.administration_id,
                deviation=row.deviation,
                detail=row.detail,
            )
            for row in result
        ]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

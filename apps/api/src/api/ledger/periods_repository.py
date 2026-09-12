"""SQLAlchemy-backed PeriodRepository over migration 0021.

Every transition goes through a `ledger.*` SECURITY DEFINER function. Not as a
style choice: `period_status_transition()` refuses a status change unless
`current_user` is `ledgr_ledger`, which is only true inside those functions.
An `UPDATE period SET status = ...` issued from here would be rejected by the
database.

Note what is absent: no `set_status`, no generic `update`. Every method is a
named lifecycle operation, so the set of transitions that exist in the code is
the set FR-GL-007 defines - and adding a new one means adding a function to
the migration, which is where it can be reviewed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.ledger.periods import (
    Period,
    PeriodStatus,
    Suppletie,
    SuppletieCorrection,
    SuppletieStatus,
)

_PERIOD_COLUMNS = """
    id, administration_id, fiscal_year_id, period_number, start_date, end_date,
    status, locked_at, locked_by_user_id, filed_at, filed_by_user_id,
    filing_reference
"""

_SUPPLETIE_COLUMNS = """
    id, administration_id, period_id, status, reason, opened_by_user_id,
    opened_at, submitted_at, filed_at, filing_reference
"""


def _period(row: Any) -> Period:
    return Period(
        id=row.id,
        administration_id=row.administration_id,
        fiscal_year_id=row.fiscal_year_id,
        period_number=int(row.period_number),
        start_date=row.start_date,
        end_date=row.end_date,
        status=PeriodStatus(row.status),
        locked_at=row.locked_at,
        locked_by_user_id=row.locked_by_user_id,
        filed_at=row.filed_at,
        filed_by_user_id=row.filed_by_user_id,
        filing_reference=row.filing_reference,
    )


def _suppletie(row: Any) -> Suppletie:
    return Suppletie(
        id=row.id,
        administration_id=row.administration_id,
        period_id=row.period_id,
        status=SuppletieStatus(row.status),
        reason=row.reason,
        opened_by_user_id=row.opened_by_user_id,
        opened_at=row.opened_at,
        submitted_at=row.submitted_at,
        filed_at=row.filed_at,
        filing_reference=row.filing_reference,
    )


class SqlPeriodRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def period(self, period_id: uuid.UUID) -> Period | None:
        result = await self._session.execute(
            text(f"SELECT {_PERIOD_COLUMNS} FROM period WHERE id = :id"),
            {"id": str(period_id)},
        )
        row = result.first()
        return _period(row) if row is not None else None

    async def organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        return result.scalar_one()  # type: ignore[no-any-return]

    # -- transitions ------------------------------------------------------

    async def lock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period:
        result = await self._session.execute(
            text(f"SELECT {_PERIOD_COLUMNS} FROM ledger.lock_period(:period_id, :user_id)"),
            {"period_id": str(period_id), "user_id": str(user_id)},
        )
        return _period(result.one())

    async def unlock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period:
        result = await self._session.execute(
            text(f"SELECT {_PERIOD_COLUMNS} FROM ledger.unlock_period(:period_id, :user_id)"),
            {"period_id": str(period_id), "user_id": str(user_id)},
        )
        return _period(result.one())

    async def mark_filed(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        filing_reference: str | None,
    ) -> Period:
        result = await self._session.execute(
            text(
                f"SELECT {_PERIOD_COLUMNS} FROM ledger.mark_period_filed("
                "  :period_id, :user_id, :filing_reference)"
            ),
            {
                "period_id": str(period_id),
                "user_id": str(user_id),
                "filing_reference": filing_reference,
            },
        )
        return _period(result.one())

    # -- suppletie --------------------------------------------------------

    async def open_suppletie(
        self, *, period_id: uuid.UUID, reason: str, user_id: uuid.UUID
    ) -> Suppletie:
        result = await self._session.execute(
            text(
                f"SELECT {_SUPPLETIE_COLUMNS} FROM ledger.open_suppletie("
                "  :period_id, :reason, :user_id)"
            ),
            {
                "period_id": str(period_id),
                "reason": reason,
                "user_id": str(user_id),
            },
        )
        return _suppletie(result.one())

    async def close_suppletie(
        self,
        *,
        suppletie_id: uuid.UUID,
        status: SuppletieStatus,
        filing_reference: str | None,
    ) -> Suppletie:
        result = await self._session.execute(
            text(
                f"SELECT {_SUPPLETIE_COLUMNS} FROM ledger.close_suppletie("
                "  :suppletie_id, :status, :filing_reference)"
            ),
            {
                "suppletie_id": str(suppletie_id),
                "status": status.value,
                "filing_reference": filing_reference,
            },
        )
        return _suppletie(result.one())

    async def suppletie(self, suppletie_id: uuid.UUID) -> Suppletie | None:
        result = await self._session.execute(
            text(f"SELECT {_SUPPLETIE_COLUMNS} FROM vat_suppletie WHERE id = :id"),
            {"id": str(suppletie_id)},
        )
        row = result.first()
        return _suppletie(row) if row is not None else None

    async def open_suppletie_for(self, period_id: uuid.UUID) -> Suppletie | None:
        result = await self._session.execute(
            text(
                f"SELECT {_SUPPLETIE_COLUMNS} FROM vat_suppletie "
                "WHERE period_id = :period_id AND status = 'open'"
            ),
            {"period_id": str(period_id)},
        )
        row = result.first()
        return _suppletie(row) if row is not None else None

    async def suppletie_corrections(self, suppletie_id: uuid.UUID) -> Sequence[SuppletieCorrection]:
        result = await self._session.execute(
            text(
                "SELECT entry_id, entry_number, entry_date, period_id, description, "
                "       total_debit, total_credit "
                "FROM ledger.suppletie_corrections(:suppletie_id)"
            ),
            {"suppletie_id": str(suppletie_id)},
        )
        return [
            SuppletieCorrection(
                entry_id=row.entry_id,
                entry_number=int(row.entry_number),
                entry_date=row.entry_date,
                period_id=row.period_id,
                description=row.description,
                total_debit=Decimal(row.total_debit),
                total_credit=Decimal(row.total_credit),
            )
            for row in result
        ]

"""SQL for the fixed-asset register - migration 0064.

A plain tenant-scoped table, not the ledger's bounded context: no query here
names `journal_entry`/`journal_line` for a WRITE (only a foreign key column
pointing at one, and a read joining to it), so this module carries none of
`api.ledger.repository`'s import restriction. The actual posting happens in
`api.assets.service` via `LedgerService.post()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.assets.model import (
    AssetDetails,
    AssetStatus,
    DepreciationAlreadyPosted,
    DepreciationMethod,
    DepreciationRun,
    FixedAsset,
)

_ASSET_COLUMNS = """
    id, administration_id, name, category, acquisition_date, acquisition_cost,
    residual_value, useful_life_months, depreciation_method, asset_account_id,
    depreciation_expense_account_id, accumulated_depreciation_account_id,
    status, disposal_date, disposal_proceeds, disposal_journal_entry_id,
    created_at, updated_at
"""


def _asset(row: Any) -> FixedAsset:
    return FixedAsset(
        id=row.id,
        administration_id=row.administration_id,
        name=row.name,
        category=row.category,
        acquisition_date=row.acquisition_date,
        acquisition_cost=Decimal(row.acquisition_cost),
        residual_value=Decimal(row.residual_value),
        useful_life_months=int(row.useful_life_months),
        depreciation_method=DepreciationMethod(row.depreciation_method),
        asset_account_id=row.asset_account_id,
        depreciation_expense_account_id=row.depreciation_expense_account_id,
        accumulated_depreciation_account_id=row.accumulated_depreciation_account_id,
        status=AssetStatus(row.status),
        disposal_date=row.disposal_date,
        disposal_proceeds=(
            Decimal(row.disposal_proceeds) if row.disposal_proceeds is not None else None
        ),
        disposal_journal_entry_id=row.disposal_journal_entry_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        details: AssetDetails,
        user_id: uuid.UUID,
    ) -> FixedAsset:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO fixed_asset (
                    organization_id, administration_id, name, category, acquisition_date,
                    acquisition_cost, residual_value, useful_life_months, asset_account_id,
                    depreciation_expense_account_id, accumulated_depreciation_account_id,
                    created_by_user_id
                ) VALUES (
                    :org, :admin, :name, :category, :acquisition_date,
                    :acquisition_cost, :residual_value, :useful_life_months, :asset_account_id,
                    :depreciation_expense_account_id, :accumulated_depreciation_account_id,
                    :user
                )
                RETURNING {_ASSET_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "name": details.name,
                "category": details.category,
                "acquisition_date": details.acquisition_date,
                "acquisition_cost": details.acquisition_cost,
                "residual_value": details.residual_value,
                "useful_life_months": details.useful_life_months,
                "asset_account_id": str(details.asset_account_id),
                "depreciation_expense_account_id": str(details.depreciation_expense_account_id),
                "accumulated_depreciation_account_id": str(
                    details.accumulated_depreciation_account_id
                ),
                "user": str(user_id),
            },
        )
        return _asset(result.one())

    async def get(self, *, administration_id: uuid.UUID, asset_id: uuid.UUID) -> FixedAsset | None:
        result = await self._session.execute(
            text(
                f"SELECT {_ASSET_COLUMNS} FROM fixed_asset "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(asset_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _asset(row)

    async def list(
        self, *, administration_id: uuid.UUID, include_disposed: bool
    ) -> Sequence[FixedAsset]:
        result = await self._session.execute(
            text(
                f"""
                SELECT {_ASSET_COLUMNS} FROM fixed_asset
                 WHERE administration_id = :admin
                   AND (:include_disposed OR status = 'active')
                 ORDER BY acquisition_date, name
                """
            ),
            {"admin": str(administration_id), "include_disposed": include_disposed},
        )
        return [_asset(row) for row in result]

    async def accumulated_depreciation(self, *, fixed_asset_id: uuid.UUID) -> Decimal:
        result = await self._session.execute(
            text(
                "SELECT coalesce(sum(amount), 0) FROM fixed_asset_depreciation_run "
                "WHERE fixed_asset_id = :id"
            ),
            {"id": str(fixed_asset_id)},
        )
        return Decimal(result.scalar_one())

    async def depreciation_runs(self, *, fixed_asset_id: uuid.UUID) -> Sequence[DepreciationRun]:
        result = await self._session.execute(
            text(
                "SELECT id, fixed_asset_id, period_id, amount, journal_entry_id, posted_at "
                "FROM fixed_asset_depreciation_run "
                "WHERE fixed_asset_id = :id ORDER BY posted_at"
            ),
            {"id": str(fixed_asset_id)},
        )
        return [
            DepreciationRun(
                id=row.id,
                fixed_asset_id=row.fixed_asset_id,
                period_id=row.period_id,
                amount=Decimal(row.amount),
                journal_entry_id=row.journal_entry_id,
                posted_at=row.posted_at,
            )
            for row in result
        ]

    async def record_depreciation_run(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        fixed_asset_id: uuid.UUID,
        period_id: uuid.UUID,
        amount: Decimal,
        journal_entry_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> DepreciationRun:
        try:
            result = await self._session.execute(
                text(
                    """
                    INSERT INTO fixed_asset_depreciation_run (
                        organization_id, administration_id, fixed_asset_id, period_id,
                        amount, journal_entry_id, posted_by_user_id
                    ) VALUES (
                        :org, :admin, :asset, :period, :amount, :entry, :user
                    )
                    RETURNING id, fixed_asset_id, period_id, amount, journal_entry_id, posted_at
                    """
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "asset": str(fixed_asset_id),
                    "period": str(period_id),
                    "amount": amount,
                    "entry": str(journal_entry_id),
                    "user": str(user_id),
                },
            )
        except IntegrityError as exc:
            if "fixed_asset_depreciation_run_once_idx" in str(exc.orig):
                raise DepreciationAlreadyPosted(
                    f"asset {fixed_asset_id} already has a depreciation run for period {period_id}"
                ) from exc
            raise
        row = result.one()
        return DepreciationRun(
            id=row.id,
            fixed_asset_id=row.fixed_asset_id,
            period_id=row.period_id,
            amount=Decimal(row.amount),
            journal_entry_id=row.journal_entry_id,
            posted_at=row.posted_at,
        )

    async def dispose(
        self,
        *,
        administration_id: uuid.UUID,
        asset_id: uuid.UUID,
        disposal_date: date,
        disposal_proceeds: Decimal,
        disposal_journal_entry_id: uuid.UUID,
    ) -> FixedAsset:
        result = await self._session.execute(
            text(
                f"""
                UPDATE fixed_asset SET
                    status = 'disposed',
                    disposal_date = :disposal_date,
                    disposal_proceeds = :disposal_proceeds,
                    disposal_journal_entry_id = :entry
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_ASSET_COLUMNS}
                """
            ),
            {
                "id": str(asset_id),
                "admin": str(administration_id),
                "disposal_date": disposal_date,
                "disposal_proceeds": disposal_proceeds,
                "entry": str(disposal_journal_entry_id),
            },
        )
        return _asset(result.one())

    async def account_types(
        self, *, administration_id: uuid.UUID, account_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Which `account_type` each of these ledger accounts has, for the
        create-time check that an asset's three accounts are the kinds a
        depreciation posting needs. A plain read of reference-ish data
        `ledgr_app` already holds SELECT on (0020) - not a posting-table
        write, so this carries none of `api.ledger.repository`'s import
        restriction (see this module's docstring).
        """
        if not account_ids:
            return {}
        result = await self._session.execute(
            text(
                "SELECT id, account_type FROM ledger_account "
                "WHERE administration_id = :admin AND id = ANY(cast(:ids as uuid[]))"
            ),
            {"admin": str(administration_id), "ids": [str(i) for i in account_ids]},
        )
        return {row.id: row.account_type for row in result}

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

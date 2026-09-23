"""The fixed-asset register's service - creation, depreciation, disposal.

Depreciation and disposal are POSTED, not merely recorded: both call
`LedgerService.post()`, the same public entry point `api.expenses.posting`
and `api.invoicing.service` already use to reach the ledger's bounded
context. Nothing in this module writes to a posting table, imports
`api.ledger.repository`, or invents a second way into the ledger -
tests/ledger/test_bounded_context.py would fail the build if it tried.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from api.assets.model import (
    AssetAlreadyDisposed,
    AssetDetails,
    AssetNotFound,
    AssetStatus,
    DepreciationRun,
    FixedAsset,
    FullyDepreciated,
    InvalidAssetField,
    NoOpenPeriod,
    next_depreciation_amount,
)
from api.assets.repository import SqlAssetRepository
from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.ledger.model import EntryInput, JournalType, LineInput
from api.ledger.periods import PeriodService
from api.ledger.service import LedgerService


class AssetService:
    def __init__(
        self,
        repository: SqlAssetRepository,
        ledger: LedgerService,
        periods: PeriodService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._periods = periods
        self._audit = audit_log

    # -- master data --------------------------------------------------------

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        details: AssetDetails,
    ) -> FixedAsset:
        """`AssetDetails.__post_init__` already refused a non-positive cost,
        a negative or excessive residual value, and a non-positive useful
        life. What is checked here is the one thing a dataclass cannot: that
        the three accounts named actually exist in THIS administration's
        chart and are the kinds a depreciation/disposal posting needs - an
        expense account named as the asset account would balance, post, and
        file wrongly, the same failure `ledger_account_rgs_integrity()`
        guards against for RGS mapping.
        """
        types = await self._repository.account_types(
            administration_id=administration_id,
            account_ids=[
                details.asset_account_id,
                details.depreciation_expense_account_id,
                details.accumulated_depreciation_account_id,
            ],
        )
        for field, account_id, expected in (
            ("asset_account_id", details.asset_account_id, "asset"),
            (
                "accumulated_depreciation_account_id",
                details.accumulated_depreciation_account_id,
                "asset",
            ),
            (
                "depreciation_expense_account_id",
                details.depreciation_expense_account_id,
                "expense",
            ),
        ):
            actual = types.get(account_id)
            if actual is None:
                raise InvalidAssetField(field, f"account {account_id} does not exist")
            if actual != expected:
                raise InvalidAssetField(
                    field, f"account {account_id} is {actual}, expected {expected}"
                )

        asset = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            details=details,
            user_id=actor_user_id,
        )
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action="create_fixed_asset",
                resource_type="fixed_asset",
                resource_id=asset.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={"name": asset.name, "acquisition_cost": str(asset.acquisition_cost)},
            )
        )
        return asset

    async def get(self, *, administration_id: uuid.UUID, asset_id: uuid.UUID) -> FixedAsset:
        asset = await self._repository.get(administration_id=administration_id, asset_id=asset_id)
        if asset is None:
            raise AssetNotFound(f"fixed asset {asset_id} does not exist")
        return asset

    async def list(
        self, *, administration_id: uuid.UUID, include_disposed: bool = True
    ) -> Sequence[FixedAsset]:
        return await self._repository.list(
            administration_id=administration_id, include_disposed=include_disposed
        )

    async def depreciation_runs(self, *, asset: FixedAsset) -> Sequence[DepreciationRun]:
        return await self._repository.depreciation_runs(fixed_asset_id=asset.id)

    # -- posting --------------------------------------------------------

    async def _single_memorial_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID:
        """FR-GL-002's single-active-journal rule, applied the way write-off
        (api.invoicing.bad_debt) and manual journal entries already do:
        depreciation and disposal are memorial postings, so they need
        exactly one active memorial journal to land in.
        """
        journals = await self._ledger.journals(administration_id=administration_id)
        candidates = [
            j for j in journals if j.journal_type is JournalType.MEMORIAL and j.status == "active"
        ]
        if len(candidates) != 1:
            raise InvalidAssetField(
                "journal",
                "this administration has no single active memorial journal to post "
                "asset entries into",
            )
        return candidates[0].id

    async def _open_period_for(self, *, administration_id: uuid.UUID, period_id: uuid.UUID):  # type: ignore[no-untyped-def]
        period = await self._periods.period(period_id)
        if period is None or period.administration_id != administration_id:
            raise NoOpenPeriod(period_id)
        if not period.is_open:
            raise NoOpenPeriod(period_id)
        return period

    async def depreciate(
        self,
        *,
        administration_id: uuid.UUID,
        asset_id: uuid.UUID,
        period_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> DepreciationRun:
        """One period's straight-line charge: Dr depreciation expense,
        Cr accumulated depreciation. Refuses (rather than posting zero) once
        the asset is fully depreciated, and 0064's unique index refuses a
        second attempt at a period already run - see
        `api.assets.model.next_depreciation_amount`.
        """
        asset = await self.get(administration_id=administration_id, asset_id=asset_id)
        if asset.status is AssetStatus.DISPOSED:
            raise AssetAlreadyDisposed(f"fixed asset {asset_id} has been disposed")

        period = await self._open_period_for(
            administration_id=administration_id, period_id=period_id
        )

        accumulated = await self._repository.accumulated_depreciation(fixed_asset_id=asset.id)
        amount = next_depreciation_amount(asset, accumulated)
        if amount <= 0:
            raise FullyDepreciated(f"fixed asset {asset_id} is already fully depreciated")

        journal_id = await self._single_memorial_journal(administration_id=administration_id)

        entry = EntryInput(
            administration_id=administration_id,
            journal_id=journal_id,
            period_id=period_id,
            entry_date=period.end_date,
            description=f"Depreciation: {asset.name}",
            lines=[
                LineInput(account_id=asset.depreciation_expense_account_id, debit=amount),
                LineInput(account_id=asset.accumulated_depreciation_account_id, credit=amount),
            ],
        )
        posted = await self._ledger.post(entry, actor_user_id=actor_user_id)

        organization_id = await self._organization_of(administration_id)
        run = await self._repository.record_depreciation_run(
            organization_id=organization_id,
            administration_id=administration_id,
            fixed_asset_id=asset.id,
            period_id=period_id,
            amount=amount,
            journal_entry_id=posted.id,
            user_id=actor_user_id,
        )
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.POSTING,
                action="depreciate_fixed_asset",
                resource_type="fixed_asset",
                resource_id=asset.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={
                    "period_id": str(period_id),
                    "amount": str(amount),
                    "journal_entry_id": str(posted.id),
                },
            )
        )
        return run

    async def dispose(
        self,
        *,
        administration_id: uuid.UUID,
        asset_id: uuid.UUID,
        period_id: uuid.UUID,
        disposal_date: date,
        proceeds: Decimal,
        proceeds_account_id: uuid.UUID | None,
        gain_loss_account_id: uuid.UUID | None,
        actor_user_id: uuid.UUID,
    ) -> FixedAsset:
        """Clears the asset's original cost and its accumulated depreciation,
        books whatever proceeds were received, and plugs the difference to a
        gain/loss account - real double-entry, not a status flip:

            Dr accumulated depreciation (to date)
            Dr proceeds account            (if proceeds > 0)
            Dr/Cr gain-or-loss account      (the balancing plug, if not zero)
            Cr asset account               (the full acquisition cost)
        """
        asset = await self.get(administration_id=administration_id, asset_id=asset_id)
        if asset.status is AssetStatus.DISPOSED:
            raise AssetAlreadyDisposed(f"fixed asset {asset_id} has already been disposed")
        if proceeds < 0:
            raise InvalidAssetField("proceeds", "disposal proceeds cannot be negative")
        if proceeds > 0 and proceeds_account_id is None:
            raise InvalidAssetField(
                "proceeds_account_id", "an account is needed to book the disposal proceeds"
            )

        period = await self._open_period_for(
            administration_id=administration_id, period_id=period_id
        )

        accumulated = await self._repository.accumulated_depreciation(fixed_asset_id=asset.id)
        net_book_value = asset.acquisition_cost - accumulated
        plug = proceeds - net_book_value  # positive: gain: negative: loss
        if plug != 0 and gain_loss_account_id is None:
            raise InvalidAssetField(
                "gain_loss_account_id",
                "an account is needed for the gain or loss on disposal",
            )

        lines = [LineInput(account_id=asset.asset_account_id, credit=asset.acquisition_cost)]
        if accumulated > 0:
            lines.append(
                LineInput(account_id=asset.accumulated_depreciation_account_id, debit=accumulated)
            )
        if proceeds > 0:
            assert proceeds_account_id is not None
            lines.append(LineInput(account_id=proceeds_account_id, debit=proceeds))
        if plug > 0:
            assert gain_loss_account_id is not None
            lines.append(LineInput(account_id=gain_loss_account_id, credit=plug))
        elif plug < 0:
            assert gain_loss_account_id is not None
            lines.append(LineInput(account_id=gain_loss_account_id, debit=-plug))

        journal_id = await self._single_memorial_journal(administration_id=administration_id)
        entry = EntryInput(
            administration_id=administration_id,
            journal_id=journal_id,
            period_id=period_id,
            entry_date=disposal_date,
            description=f"Disposal: {asset.name}",
            lines=lines,
        )
        posted = await self._ledger.post(entry, actor_user_id=actor_user_id)

        disposed = await self._repository.dispose(
            administration_id=administration_id,
            asset_id=asset.id,
            disposal_date=disposal_date,
            disposal_proceeds=proceeds,
            disposal_journal_entry_id=posted.id,
        )
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.POSTING,
                action="dispose_fixed_asset",
                resource_type="fixed_asset",
                resource_id=asset.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=actor_user_id,
                detail={
                    "disposal_date": disposal_date.isoformat(),
                    "proceeds": str(proceeds),
                    "journal_entry_id": str(posted.id),
                },
            )
        )
        del period  # only its end_date/open-ness mattered above
        return disposed

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        assert organization_id is not None
        return organization_id

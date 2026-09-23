"""Value types for the fixed-asset register (migration 0064).

Depreciation and disposal are not a second bounded context: `AssetService`
posts through `LedgerService.post()`, the same public entry point expense and
invoice posting already use (CLAUDE.md non-negotiable #1 names the LEDGER as
the narrow-API context; a fixed asset is master data pointing at postings,
the same relationship a customer or a sales invoice has to the ledger).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

#: Matches api.ledger.model.SCALE - the ledger's own posted amounts are the
#: authority this rounds toward, so the two must agree.
SCALE = Decimal("0.01")


class DepreciationMethod(enum.Enum):
    """Closed, and mirrored by a CHECK in 0064. Declining-balance and units-
    of-production are real methods a later administration may need; adding
    one is a schema change, the same posture api.ledger.model.JournalType
    takes toward its own closed list.
    """

    STRAIGHT_LINE = "straight_line"


class AssetStatus(enum.Enum):
    ACTIVE = "active"
    DISPOSED = "disposed"


class AssetError(Exception):
    """Base for every refusal this module raises."""


class AssetNotFound(AssetError):
    pass


class AssetAlreadyDisposed(AssetError):
    pass


class DepreciationAlreadyPosted(AssetError):
    """0064's fixed_asset_depreciation_run_once_idx, named. A second attempt
    at the same asset's same period would double the expense.
    """


class FullyDepreciated(AssetError):
    """The asset's book value already equals its residual value; there is
    nothing left to charge.
    """


class NoOpenPeriod(AssetError):
    def __init__(self, period_id: uuid.UUID) -> None:
        self.period_id = period_id
        super().__init__(f"period {period_id} is not open")


class InvalidAssetField(AssetError):
    def __init__(self, field: str, message: str = "") -> None:
        self.field = field
        super().__init__(message or field)


@dataclass(frozen=True, slots=True)
class FixedAsset:
    id: uuid.UUID
    administration_id: uuid.UUID
    name: str
    category: str | None
    acquisition_date: date
    acquisition_cost: Decimal
    residual_value: Decimal
    useful_life_months: int
    depreciation_method: DepreciationMethod
    asset_account_id: uuid.UUID
    depreciation_expense_account_id: uuid.UUID
    accumulated_depreciation_account_id: uuid.UUID
    status: AssetStatus
    disposal_date: date | None
    disposal_proceeds: Decimal | None
    disposal_journal_entry_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @property
    def depreciable_amount(self) -> Decimal:
        return self.acquisition_cost - self.residual_value


@dataclass(frozen=True, slots=True)
class DepreciationRun:
    id: uuid.UUID
    fixed_asset_id: uuid.UUID
    period_id: uuid.UUID
    amount: Decimal
    journal_entry_id: uuid.UUID
    posted_at: datetime


@dataclass(frozen=True, slots=True)
class AssetDetails:
    """What a create call supplies."""

    name: str
    category: str | None
    acquisition_date: date
    acquisition_cost: Decimal
    residual_value: Decimal
    useful_life_months: int
    asset_account_id: uuid.UUID
    depreciation_expense_account_id: uuid.UUID
    accumulated_depreciation_account_id: uuid.UUID

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidAssetField("name", "an asset needs a name")
        if self.acquisition_cost <= 0:
            raise InvalidAssetField("acquisition_cost", "acquisition cost must be positive")
        if self.residual_value < 0:
            raise InvalidAssetField("residual_value", "residual value cannot be negative")
        if self.residual_value > self.acquisition_cost:
            raise InvalidAssetField(
                "residual_value", "residual value cannot exceed acquisition cost"
            )
        if self.useful_life_months <= 0:
            raise InvalidAssetField("useful_life_months", "useful life must be at least one month")


def next_depreciation_amount(asset: FixedAsset, accumulated_so_far: Decimal) -> Decimal:
    """The next straight-line charge - capped so the asset never depreciates
    below its residual value, however many periods are run or however the
    rounding drifted along the way.

    Deliberately NOT indexed by "which run number is this": a flat monthly
    rate that always caps at the remaining depreciable amount reaches the
    same total (cost - residual) whether it is charged for exactly
    `useful_life_months` periods or run a few extra times by mistake - the
    cap is what makes a duplicate-looking call safe to reject at the
    database (0064's unique index) rather than needing to be safe on its own.
    """
    remaining = asset.depreciable_amount - accumulated_so_far
    if remaining <= 0:
        return Decimal("0.00")
    per_month = (asset.depreciable_amount / asset.useful_life_months).quantize(
        SCALE, rounding=ROUND_DOWN
    )
    return min(per_month, remaining)

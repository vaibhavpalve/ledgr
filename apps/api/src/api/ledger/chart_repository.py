"""SQLAlchemy-backed ChartRepository over migration 0024.

Every write goes through a `ledger.*` SECURITY DEFINER function, for the
reason api/ledger/repository.py gives: `ledgr_app` holds SELECT on
ledger_account and the RGS reference tables and nothing else, so an UPDATE
issued from here would be refused by the database before any rule in this
codebase was consulted.

That matters more here than usual. FR-ONB-005's "may extend but not break RGS
mapping integrity" would be unenforceable if application code could write
ledger_account.rgs_code directly - or, worse, write a row into rgs_element and
then map an account to it, which satisfies every foreign key and breaks the
requirement completely.

Note what is absent: no way to modify or remove an RGS version, element,
profile row or mapping. They are append-only (0024), no role holds the
privilege, and triggers reject it. A repository method for a statement the
database refuses would be a lie about what these tables can do.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.ledger.chart import (
    ChartAccount,
    LegalForm,
    MappingDeviation,
    RgsElement,
    RgsReadiness,
    RgsSource,
    RgsVersion,
    RgsVersionStatus,
    SeedResult,
    UpgradeResolution,
    UpgradeResult,
    UpgradeStep,
    VatTreatment,
)
from api.ledger.model import AccountStatus, AccountType, ControlKind

_VERSION_COLUMNS = """
    id, version, status, published_at, effective_from, source, source_note,
    source_checksum, supersedes_id, loaded_at
"""

_CHART_COLUMNS = """
    account_id, code, name, account_type, status, rgs_code, rgs_description_nl,
    rgs_description_en, rgs_version_name, default_vat_code, control_kind,
    is_seeded
"""


def _vat(value: str | None) -> VatTreatment | None:
    return VatTreatment(value) if value else None


def _version(row: Any) -> RgsVersion:
    return RgsVersion(
        id=row.id,
        version=row.version,
        status=RgsVersionStatus(row.status),
        source=RgsSource(row.source),
        source_checksum=row.source_checksum,
        published_at=row.published_at,
        effective_from=row.effective_from,
        source_note=row.source_note,
        supersedes_id=row.supersedes_id,
        loaded_at=row.loaded_at,
    )


def _chart_account(row: Any) -> ChartAccount:
    return ChartAccount(
        account_id=row.account_id,
        code=row.code,
        name=row.name,
        account_type=AccountType(row.account_type),
        status=AccountStatus(row.status),
        rgs_code=row.rgs_code,
        rgs_description_nl=row.rgs_description_nl,
        rgs_description_en=row.rgs_description_en,
        rgs_version=row.rgs_version_name,
        default_vat_code=_vat(row.default_vat_code),
        control_kind=ControlKind(row.control_kind) if row.control_kind else None,
        is_seeded=bool(row.is_seeded),
    )


class SqlChartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- FR-ONB-005 --------------------------------------------------------

    async def seed(
        self,
        *,
        administration_id: uuid.UUID,
        rgs_version_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        profile_code: str,
    ) -> SeedResult:
        result = await self._session.execute(
            text(
                "SELECT seeded_count, skipped_count, rgs_version_name, "
                "       legal_form_code "
                "FROM ledger.seed_chart_of_accounts("
                "  p_administration_id => :administration_id,"
                "  p_rgs_version_id    => cast(:rgs_version_id as uuid),"
                "  p_actor_user_id     => cast(:actor_user_id as uuid),"
                "  p_profile_code      => :profile_code)"
            ),
            {
                "administration_id": str(administration_id),
                "rgs_version_id": str(rgs_version_id) if rgs_version_id else None,
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
                "profile_code": profile_code,
            },
        )
        row = result.one()
        return SeedResult(
            seeded_count=int(row.seeded_count),
            skipped_count=int(row.skipped_count),
            rgs_version=row.rgs_version_name,
            legal_form=LegalForm(row.legal_form_code),
        )

    # -- FR-GL-005 ---------------------------------------------------------

    async def chart(
        self, *, administration_id: uuid.UUID, include_blocked: bool
    ) -> Sequence[ChartAccount]:
        result = await self._session.execute(
            text(
                f"SELECT {_CHART_COLUMNS} "
                "FROM ledger.chart_of_accounts(:administration_id, :include_blocked)"
            ),
            {
                "administration_id": str(administration_id),
                "include_blocked": include_blocked,
            },
        )
        return [_chart_account(row) for row in result]

    async def add_account(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None,
        default_vat_code: VatTreatment | None,
        control_kind: ControlKind | None,
    ) -> ChartAccount:
        # ledger.create_account returns a ledger_account row, which does not
        # carry the RGS element's description. Re-read through
        # ledger.chart_of_accounts so a caller gets the same shape whether an
        # account was just created or listed - a UI that had to render two
        # shapes for the same thing would grow a branch for no reason.
        created = await self._session.execute(
            text(
                "SELECT id FROM ledger.create_account("
                "  p_administration_id => :administration_id,"
                "  p_code              => :code,"
                "  p_name              => :name,"
                "  p_account_type      => :account_type,"
                "  p_rgs_code          => :rgs_code,"
                "  p_default_vat_code  => :default_vat_code,"
                "  p_control_kind      => :control_kind)"
            ),
            {
                "administration_id": str(administration_id),
                "code": code,
                "name": name,
                "account_type": account_type.value,
                "rgs_code": rgs_code,
                "default_vat_code": (default_vat_code.value if default_vat_code else None),
                "control_kind": control_kind.value if control_kind else None,
            },
        )
        return await self._one(administration_id, created.scalar_one())

    async def set_account_status(
        self, *, account_id: uuid.UUID, status: AccountStatus
    ) -> ChartAccount:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id FROM ledger.set_account_status(:account_id, :status)"
            ),
            {"account_id": str(account_id), "status": status.value},
        )
        row = result.one()
        return await self._one(row.administration_id, row.id)

    async def set_account_rgs_code(
        self, *, account_id: uuid.UUID, rgs_code: str | None
    ) -> ChartAccount:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id "
                "FROM ledger.set_account_rgs_code(:account_id, :rgs_code)"
            ),
            {"account_id": str(account_id), "rgs_code": rgs_code},
        )
        row = result.one()
        return await self._one(row.administration_id, row.id)

    async def rgs_options(
        self, *, administration_id: uuid.UUID, account_type: AccountType
    ) -> Sequence[RgsElement]:
        result = await self._session.execute(
            text(
                "SELECT code, description_nl, description_en, level, parent_code "
                "FROM ledger.rgs_options(:administration_id, :account_type)"
            ),
            {
                "administration_id": str(administration_id),
                "account_type": account_type.value,
            },
        )
        return [
            RgsElement(
                code=row.code,
                description_nl=row.description_nl,
                description_en=row.description_en,
                level=int(row.level),
                parent_code=row.parent_code,
                is_postable=True,  # rgs_options returns only postable elements
                account_type=account_type,
            )
            for row in result
        ]

    # -- CMP-003 -----------------------------------------------------------

    async def current_version(self) -> RgsVersion | None:
        result = await self._session.execute(
            text(f"SELECT {_VERSION_COLUMNS} FROM rgs_version WHERE status = 'current'")
        )
        row = result.first()
        return _version(row) if row is not None else None

    async def version(self, *, version: str) -> RgsVersion | None:
        result = await self._session.execute(
            text(f"SELECT {_VERSION_COLUMNS} FROM rgs_version WHERE version = :version"),
            {"version": version},
        )
        row = result.first()
        return _version(row) if row is not None else None

    async def pinned_version(self, *, administration_id: uuid.UUID) -> RgsVersion | None:
        result = await self._session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM rgs_version v "
                "WHERE v.id = (SELECT rgs_version_id FROM administration_rgs_version "
                "               WHERE administration_id = :administration_id)"
            ),
            {"administration_id": str(administration_id)},
        )
        row = result.first()
        return _version(row) if row is not None else None

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

    async def plan_upgrade(
        self, *, administration_id: uuid.UUID, to_version_id: uuid.UUID
    ) -> Sequence[UpgradeStep]:
        result = await self._session.execute(
            text(
                "SELECT account_id, account_code, account_name, account_type, "
                "       from_rgs_code, to_rgs_code, change_kind, resolution, detail "
                "FROM ledger.plan_rgs_upgrade(:administration_id, :to_version_id)"
            ),
            {
                "administration_id": str(administration_id),
                "to_version_id": str(to_version_id),
            },
        )
        return [
            UpgradeStep(
                account_id=row.account_id,
                account_code=row.account_code,
                account_name=row.account_name,
                account_type=AccountType(row.account_type),
                resolution=UpgradeResolution(row.resolution),
                change_kind=row.change_kind,
                detail=row.detail,
                from_rgs_code=row.from_rgs_code,
                to_rgs_code=row.to_rgs_code,
            )
            for row in result
        ]

    async def apply_upgrade(
        self,
        *,
        administration_id: uuid.UUID,
        to_version_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> UpgradeResult:
        result = await self._session.execute(
            text(
                "SELECT remapped_count, unmapped_count, rgs_version_name "
                "FROM ledger.apply_rgs_upgrade("
                "  :administration_id, :to_version_id, cast(:actor_user_id as uuid))"
            ),
            {
                "administration_id": str(administration_id),
                "to_version_id": str(to_version_id),
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
            },
        )
        row = result.one()
        return UpgradeResult(
            remapped_count=int(row.remapped_count),
            unmapped_count=int(row.unmapped_count),
            rgs_version=row.rgs_version_name,
        )

    async def readiness(self, *, administration_id: uuid.UUID | None) -> Sequence[RgsReadiness]:
        result = await self._session.execute(
            text(
                "SELECT administration_id, legal_form_code, rgs_version_name, "
                "       version_status, version_source, is_provisional, "
                "       is_behind_current, accounts_total, accounts_unmapped "
                "FROM ledger.rgs_readiness(cast(:administration_id as uuid))"
            ),
            {"administration_id": (str(administration_id) if administration_id else None)},
        )
        return [
            RgsReadiness(
                administration_id=row.administration_id,
                legal_form=LegalForm(row.legal_form_code),
                rgs_version=row.rgs_version_name,
                version_status=RgsVersionStatus(row.version_status),
                version_source=RgsSource(row.version_source),
                is_provisional=bool(row.is_provisional),
                is_behind_current=bool(row.is_behind_current),
                accounts_total=int(row.accounts_total),
                accounts_unmapped=int(row.accounts_unmapped),
            )
            for row in result
        ]

    async def mapping_deviations(
        self, *, administration_id: uuid.UUID | None
    ) -> Sequence[MappingDeviation]:
        result = await self._session.execute(
            text(
                "SELECT account_id, administration_id, account_code, deviation, detail "
                "FROM ledger.rgs_mapping_deviations(cast(:administration_id as uuid))"
            ),
            {"administration_id": (str(administration_id) if administration_id else None)},
        )
        return [
            MappingDeviation(
                account_id=row.account_id,
                administration_id=row.administration_id,
                account_code=row.account_code,
                deviation=row.deviation,
                detail=row.detail,
            )
            for row in result
        ]

    # -- internals ---------------------------------------------------------

    async def _one(self, administration_id: uuid.UUID, account_id: uuid.UUID) -> ChartAccount:
        result = await self._session.execute(
            text(
                f"SELECT {_CHART_COLUMNS} "
                "FROM ledger.chart_of_accounts(:administration_id, true) "
                "WHERE account_id = :account_id"
            ),
            {
                "administration_id": str(administration_id),
                "account_id": str(account_id),
            },
        )
        return _chart_account(result.one())

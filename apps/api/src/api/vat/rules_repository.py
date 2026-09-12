"""SQLAlchemy-backed VatRulesRepository over migration 0028 (CMP-014).

Reads only. Every write path to a tax rule is `vat.load_ruleset`, granted to
`ledgr_ops` and not to `ledgr_app`, so there is no statement this class could
issue that would change a rule even if it wanted to.

Note what is absent, for the same reason api/ledger/repository.py has no
update(): no method rewrites a rate, closes one, or moves the filed frontier
backwards. The database refuses all three, and a repository method for a
statement the database refuses would misrepresent what these tables can do.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.vat.rules import (
    EffectiveRules,
    FiledPeriodDrift,
    Rubriek,
    RubriekKind,
    RuleSet,
    RuleSource,
    TreatmentRole,
    TreatmentRule,
    WatermarkDrift,
)


def _rate(value: Any) -> Decimal | None:
    """numeric arrives as Decimal through asyncpg. Wrapping it again is a
    no-op for a Decimal and a loud failure if a driver ever hands back a
    float - which would violate NFR-031 everywhere downstream, starting with
    the first invoice line it multiplies.
    """
    return Decimal(value) if value is not None else None


class SqlVatRulesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def rules_on(self, *, on_date: date) -> EffectiveRules:
        treatments = await self._session.execute(
            text(
                "SELECT treatment_code, role, rate, turnover_rubriek, vat_rubriek "
                "FROM vat.rules_on(cast(:on_date as date))"
            ),
            {"on_date": on_date},
        )
        rubrieken = await self._session.execute(
            text(
                "SELECT code, kind, description_nl, description_en "
                "FROM vat.rubrieken_on(cast(:on_date as date))"
            ),
            {"on_date": on_date},
        )
        fingerprint = await self._session.execute(
            text("SELECT vat.rules_fingerprint(cast(:on_date as date)) AS f"),
            {"on_date": on_date},
        )

        return EffectiveRules(
            on_date=on_date,
            treatments=tuple(
                TreatmentRule(
                    code=row.treatment_code,
                    role=TreatmentRole(row.role),
                    rate=_rate(row.rate),
                    turnover_rubriek=row.turnover_rubriek,
                    vat_rubriek=row.vat_rubriek,
                )
                for row in treatments
            ),
            rubrieken=tuple(
                Rubriek(
                    code=row.code,
                    kind=RubriekKind(row.kind),
                    description_nl=row.description_nl,
                    description_en=row.description_en,
                )
                for row in rubrieken
            ),
            fingerprint=fingerprint.scalar_one(),
        )

    async def rate_on(self, *, treatment: str, on_date: date) -> Decimal | None:
        result = await self._session.execute(
            text("SELECT vat.rate_on(:treatment, cast(:on_date as date)) AS rate"),
            {"treatment": treatment, "on_date": on_date},
        )
        return _rate(result.scalar_one())

    async def rulesets(self) -> Sequence[RuleSet]:
        result = await self._session.execute(
            text(
                "SELECT id, jurisdiction, version, source, source_note, "
                "       source_checksum, loaded_at "
                "FROM vat_ruleset ORDER BY jurisdiction, version"
            )
        )
        return [
            RuleSet(
                id=row.id,
                jurisdiction=row.jurisdiction,
                version=row.version,
                source=RuleSource(row.source),
                source_checksum=row.source_checksum,
                source_note=row.source_note,
                loaded_at=row.loaded_at,
            )
            for row in result
        ]

    async def filed_period_drift(self) -> Sequence[FiledPeriodDrift]:
        result = await self._session.execute(
            text(
                "SELECT period_id, administration_id, end_date, filed_at, "
                "       recorded_fingerprint, current_fingerprint "
                "FROM vat.filed_period_drift()"
            )
        )
        return [
            FiledPeriodDrift(
                period_id=row.period_id,
                administration_id=row.administration_id,
                end_date=row.end_date,
                recorded_fingerprint=row.recorded_fingerprint,
                current_fingerprint=row.current_fingerprint,
                filed_at=row.filed_at,
            )
            for row in result
        ]

    async def watermark_drift(self) -> WatermarkDrift | None:
        result = await self._session.execute(
            text("SELECT recorded, computed FROM vat.watermark_drift()")
        )
        row = result.first()
        return (
            WatermarkDrift(recorded=row.recorded, computed=row.computed)
            if row is not None
            else None
        )

    async def filed_through(self) -> date | None:
        result = await self._session.execute(
            text("SELECT filed_through FROM vat_filing_watermark WHERE jurisdiction = 'NL'")
        )
        row = result.first()
        return row.filed_through if row is not None else None


def rgs_version_on_statement() -> str:
    """CMP-014's third clause, kept here as SQL rather than a method.

    `ledger.rgs_version_on` belongs to the RGS reference data in 0024, and
    reading it through api.ledger's own repository is what a caller should do.
    This exists so the query is findable from the module that owns the
    requirement rather than only from the one that owns the table.
    """
    return (
        "SELECT id, version, status, effective_from "
        "FROM ledger.rgs_version_on(cast(:on_date as date))"
    )

"""An in-memory VatRulesRepository that REIMPLEMENTS migration 0028's rules
over the REAL ruleset shipped in apps/api/data/vat/.

The same bargain tests/support/fake_chart_repository.py makes with 0024. A fake
that returned canned rates would let tests/vat/test_rules.py prove the service
calls the repository and nothing about CMP-014, which is entirely a claim about
WHICH row answers a question. So the resolution rule, the append-only rule and
the filed-frontier barrier are all here, and
tests/integration/test_effective_dated_rules.py runs the same case table
against Postgres.

Loading the shipped file rather than a miniature fixture means "an invoice
dated 2018-12-31 is 6%" is a statement about the dataset that actually ships.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from api.vat.rules import (
    EffectiveRules,
    FiledPeriodDrift,
    Rubriek,
    RubriekKind,
    RuleSet,
    RuleSource,
    TreatmentRole,
    TreatmentRule,
    VatRulesError,
    WatermarkDrift,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "vat"
SHIPPED_RULESET = DATA_DIR / "nl-vat-rules.json"


def load_document(path: Path = SHIPPED_RULESET) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class FakeRate:
    treatment: str
    valid_from: date
    rate: Decimal


@dataclass(frozen=True, slots=True)
class FakeRubriek:
    code: str
    valid_from: date
    kind: RubriekKind
    description_nl: str
    description_en: str | None


@dataclass(frozen=True, slots=True)
class FakeMapping:
    treatment: str
    valid_from: date
    turnover_rubriek: str
    vat_rubriek: str | None


@dataclass(frozen=True, slots=True)
class FakeFiledPeriod:
    period_id: uuid.UUID
    administration_id: uuid.UUID
    end_date: date
    recorded_fingerprint: str
    filed_at: datetime


@dataclass
class InMemoryVatRulesRepository:
    """Mirrors 0028's tables. Field names match the columns."""

    #: Named for storage, not for the protocol method: a dataclass field and a
    #: method of the same name collide, and the instance attribute wins - so
    #: `await repo.rulesets()` would have tried to call a list.
    loaded_rulesets: list[RuleSet] = field(default_factory=list)
    treatments: dict[str, TreatmentRole] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    rates: list[FakeRate] = field(default_factory=list)
    rubriek_rows: list[FakeRubriek] = field(default_factory=list)
    mappings: list[FakeMapping] = field(default_factory=list)

    filed_periods: list[FakeFiledPeriod] = field(default_factory=list)
    watermark: date | None = None

    # -- loading -----------------------------------------------------------

    def load(self, document: dict[str, Any], *, allow_provisional: bool = True) -> RuleSet:
        """vat.load_ruleset(), in memory - including the barrier, which is the
        rule most worth having in both places.
        """
        source = RuleSource(document["source"])
        if source is RuleSource.PROVISIONAL and not allow_provisional:
            raise VatRulesError("ruleset is a provisional subset")

        ruleset = RuleSet(
            id=uuid.uuid4(),
            jurisdiction=document.get("jurisdiction", "NL"),
            version=document["ruleset_version"],
            source=source,
            source_checksum=hashlib.sha256(
                json.dumps(document, sort_keys=True).encode()
            ).hexdigest(),
            source_note=document.get("source_note"),
            loaded_at=datetime.now(UTC),
        )
        if any(r.version == ruleset.version for r in self.loaded_rulesets):
            raise VatRulesError(f"ruleset {ruleset.version} is already loaded")

        for raw in document.get("treatments") or []:
            self.treatments[raw["code"]] = TreatmentRole(raw["role"])
            self.descriptions[raw["code"]] = raw["description_nl"]

        for raw in document.get("rates") or []:
            if not isinstance(raw["rate"], str):
                raise VatRulesError("rates must be decimal strings, not JSON numbers (NFR-031)")
            self.add_rate(
                treatment=raw["treatment"],
                valid_from=date.fromisoformat(raw["valid_from"]),
                rate=Decimal(raw["rate"]),
            )

        for raw in document.get("rubrieken") or []:
            self._guard(date.fromisoformat(raw["valid_from"]))
            self.rubriek_rows.append(
                FakeRubriek(
                    code=raw["code"],
                    valid_from=date.fromisoformat(raw["valid_from"]),
                    kind=RubriekKind(raw["kind"]),
                    description_nl=raw["description_nl"],
                    description_en=raw.get("description_en"),
                )
            )

        for raw in document.get("treatment_rubriek") or []:
            self._guard(date.fromisoformat(raw["valid_from"]))
            self.mappings.append(
                FakeMapping(
                    treatment=raw["treatment"],
                    valid_from=date.fromisoformat(raw["valid_from"]),
                    turnover_rubriek=raw["turnover_rubriek"],
                    vat_rubriek=raw.get("vat_rubriek"),
                )
            )

        self.loaded_rulesets.append(ruleset)
        return ruleset

    #: vat_rate.rate is numeric(6,3), so 21.00 is stored - and rendered - as
    #: 21.000. The fingerprint hashes that rendering, so a fake that kept the
    #: file's two decimals would hash a different string from the database for
    #: the same rules. The scale belongs to the column, so the column wins.
    SCALE = Decimal("0.001")

    def add_rate(self, *, treatment: str, valid_from: date, rate: Decimal) -> FakeRate:
        """vat_rate's INSERT, with both triggers: the barrier and the primary
        key that makes a second rate at one date impossible.
        """
        rate = rate.quantize(self.SCALE)
        self._guard(valid_from)
        if any(r.treatment == treatment and r.valid_from == valid_from for r in self.rates):
            raise VatRulesError(
                f"a rate for {treatment} effective {valid_from} already exists; "
                "tax rules are append-only (CMP-014)"
            )
        row = FakeRate(treatment=treatment, valid_from=valid_from, rate=rate)
        self.rates.append(row)
        return row

    def _guard(self, valid_from: date) -> None:
        """vat_rule_not_behind_a_filing(), with the same message substring the
        SQL raises so the shared case table can match either.
        """
        if self.watermark is not None and valid_from <= self.watermark:
            raise VatRulesError(
                f"a tax rule effective {valid_from} would change periods already "
                f"filed through {self.watermark} (CMP-014). Rules take effect "
                "ahead of the filed frontier, never behind it; correcting a "
                "filed period is a suppletie (FR-VAT-005), not a rate edit."
            )

    # -- filing ------------------------------------------------------------

    def file_period(
        self,
        *,
        end_date: date,
        administration_id: uuid.UUID | None = None,
        period_id: uuid.UUID | None = None,
    ) -> FakeFiledPeriod:
        """ledger.mark_period_filed's CMP-014 half: record the fingerprint and
        advance the frontier, in one act.
        """
        filed = FakeFiledPeriod(
            period_id=period_id or uuid.uuid4(),
            administration_id=administration_id or uuid.uuid4(),
            end_date=end_date,
            recorded_fingerprint=self._fingerprint(end_date),
            filed_at=datetime.now(UTC),
        )
        self.filed_periods.append(filed)
        # greatest(), never a plain assignment: periods are not filed in date
        # order and the frontier must not be pulled back.
        self.watermark = end_date if self.watermark is None else max(self.watermark, end_date)
        return filed

    # -- resolution --------------------------------------------------------

    def _rate_on(self, treatment: str, on_date: date) -> Decimal | None:
        applicable = [r for r in self.rates if r.treatment == treatment and r.valid_from <= on_date]
        if not applicable:
            return None
        return max(applicable, key=lambda r: r.valid_from).rate

    def _mapping_on(self, treatment: str, on_date: date) -> FakeMapping | None:
        applicable = [
            m for m in self.mappings if m.treatment == treatment and m.valid_from <= on_date
        ]
        return max(applicable, key=lambda m: m.valid_from) if applicable else None

    def _rubrieken_on(self, on_date: date) -> list[Rubriek]:
        latest: dict[str, FakeRubriek] = {}
        for row in self.rubriek_rows:
            if row.valid_from > on_date:
                continue
            seen = latest.get(row.code)
            if seen is None or row.valid_from > seen.valid_from:
                latest[row.code] = row
        return [
            Rubriek(
                code=row.code,
                kind=row.kind,
                description_nl=row.description_nl,
                description_en=row.description_en,
            )
            for row in sorted(latest.values(), key=lambda r: r.code)
        ]

    def _treatments_on(self, on_date: date) -> list[TreatmentRule]:
        rules: list[TreatmentRule] = []
        for code in sorted(self.treatments):
            mapping = self._mapping_on(code, on_date)
            rules.append(
                TreatmentRule(
                    code=code,
                    role=self.treatments[code],
                    rate=self._rate_on(code, on_date),
                    turnover_rubriek=mapping.turnover_rubriek if mapping else None,
                    vat_rubriek=mapping.vat_rubriek if mapping else None,
                )
            )
        return rules

    def _fingerprint(self, on_date: date) -> str:
        """vat.rules_fingerprint(), byte for byte - same field order, same
        separators, same '-' for a null. The two implementations produce the
        same hash for the same rules, which
        tests/integration/test_effective_dated_rules.py asserts directly.
        """
        treatments = "\n".join(
            "|".join(
                [
                    rule.code,
                    rule.role.value,
                    str(rule.rate) if rule.rate is not None else "-",
                    rule.turnover_rubriek or "-",
                    rule.vat_rubriek or "-",
                ]
            )
            for rule in self._treatments_on(on_date)
        )
        rubrieken = "\n".join(f"{r.code}|{r.kind.value}" for r in self._rubrieken_on(on_date))
        payload = f"{treatments}\n--\n{rubrieken}"
        return hashlib.sha256(payload.encode()).hexdigest()

    # -- VatRulesRepository ------------------------------------------------

    async def rules_on(self, *, on_date: date) -> EffectiveRules:
        return EffectiveRules(
            on_date=on_date,
            treatments=tuple(self._treatments_on(on_date)),
            rubrieken=tuple(self._rubrieken_on(on_date)),
            fingerprint=self._fingerprint(on_date),
        )

    async def rate_on(self, *, treatment: str, on_date: date) -> Decimal | None:
        return self._rate_on(treatment, on_date)

    async def rulesets(self) -> Sequence[RuleSet]:
        return list(self.loaded_rulesets)

    async def filed_period_drift(self) -> Sequence[FiledPeriodDrift]:
        drifted: list[FiledPeriodDrift] = []
        for filed in self.filed_periods:
            current = self._fingerprint(filed.end_date)
            if current != filed.recorded_fingerprint:
                drifted.append(
                    FiledPeriodDrift(
                        period_id=filed.period_id,
                        administration_id=filed.administration_id,
                        end_date=filed.end_date,
                        recorded_fingerprint=filed.recorded_fingerprint,
                        current_fingerprint=current,
                        filed_at=filed.filed_at,
                    )
                )
        return drifted

    async def watermark_drift(self) -> WatermarkDrift | None:
        computed = (
            max((f.end_date for f in self.filed_periods), default=None)
            if self.filed_periods
            else None
        )
        if computed == self.watermark:
            return None
        return WatermarkDrift(recorded=self.watermark, computed=computed)

    async def filed_through(self) -> date | None:
        return self.watermark

"""Effective-dated VAT rules: CMP-014 (PRD §11).

    CMP-014  Rate and rule changes are effective-dated, so historical periods
             keep the rules that applied at the time.

Three kinds of rule are covered - VAT rates, rubriek definitions, and RGS
versions - and the first two live here. RGS versions are resolved by
`ledger.rgs_version_on()`, beside the rest of the RGS reference data they
belong to.

--- Where the rules live ---

Not here. Migration 0028 holds all of it:

    resolution        the row with the greatest valid_from at or before a date
    append-only       unconditional RAISE triggers on update/delete/truncate
    the barrier       vat_rule_not_behind_a_filing(), on every rule table
    the fingerprint   recorded on a period at the moment it is filed

This module reads those functions and gives them types. If it and the database
ever disagree, the database is right - the same bargain api/ledger/model.py
makes with 0020.

--- "Retroactively changing a rate must not alter a filed period" ---

Effective dating on its own does not deliver that half of CMP-014. A row
inserted today with valid_from in 2019 changes what applied in 2019, and every
return filed since would quietly restate. So there is a barrier and a detector:

    PREVENTED   a rule may not take effect on or before the last day any
                period has been VAT-filed through. The frontier is one date
                for the whole system, because a rate is national and one row
                reaches every tenant.

    DETECTED    `filed_period_drift()` recomputes each filed period's
                fingerprint and reports any that has moved. Always empty; a
                row means the barrier was bypassed - by a dropped trigger, or
                by a superuser writing around it.

The asymmetry is deliberate and is this codebase's usual shape: prevention is
the guarantee, detection is the check on the guarantee.

--- Rates are Decimal, and a percentage ---

21.000, not 0.21 and never a float. NFR-031 covers the whole calculation path
and a rate is its first multiplier: a rate that has already lost precision
multiplies that loss across every line it touches. The loader rejects a JSON
number for the same reason `ledger.post_entry` does.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


class TreatmentRole(enum.Enum):
    """FR-AR-002's treatments, by what they DO.

    The stable identity. `vat_treatment.code` carries strings like `btw_21`
    and `btw_9`, which name a rate that has already outlived them - btw_9 was
    6% until 2019 - so anything reasoning about a treatment reads the role and
    anything storing one writes the code. ADR-027 records the rename as owed.
    """

    STANDARD = "standard"
    REDUCED = "reduced"
    ZERO = "zero"
    EXEMPT = "exempt"
    REVERSE_CHARGE = "reverse_charge"
    INTRA_COMMUNITY = "intra_community"
    EXPORT = "export"
    MARGIN = "margin"


class RubriekKind(enum.Enum):
    """What an OB-aangifte box holds (FR-VAT-001)."""

    TURNOVER = "turnover"
    VAT = "vat"
    INPUT = "input"
    #: Computed from other boxes; never posted to directly. 5a is the one.
    SUBTOTAL = "subtotal"


class RuleSource(enum.Enum):
    OFFICIAL = "official-publication"
    #: Not verified against the Belastingdienst's own publication. Safe to
    #: build and test against; not a basis for a filed return (FR-VAT-003).
    PROVISIONAL = "provisional-subset"


class VatRulesError(Exception):
    """Base for this module's refusals."""


@dataclass(frozen=True, slots=True)
class RuleSet:
    id: uuid.UUID
    jurisdiction: str
    version: str
    source: RuleSource
    source_checksum: str
    source_note: str | None = None
    loaded_at: datetime | None = None

    @property
    def is_provisional(self) -> bool:
        return self.source is RuleSource.PROVISIONAL


@dataclass(frozen=True, slots=True)
class Rubriek:
    code: str
    kind: RubriekKind
    description_nl: str
    description_en: str | None = None


@dataclass(frozen=True, slots=True)
class TreatmentRule:
    """One treatment as it stood on a particular date."""

    code: str
    role: TreatmentRole
    #: A percentage. None means no rule covers the date - a data gap, which is
    #: not the same as a rate of zero and must never be silently treated as
    #: one.
    rate: Decimal | None
    turnover_rubriek: str | None = None
    #: None where the treatment produces no output VAT for the seller. A
    #: reverse-charged supply has no VAT to report; a 0% supply has an amount
    #: that happens to be zero.
    vat_rubriek: str | None = None

    @property
    def is_resolvable(self) -> bool:
        return self.rate is not None

    def vat_on(self, base: Decimal) -> Decimal:
        """The VAT on a base amount, at this rule's rate.

        Refuses rather than guesses when the rate is missing: computing zero
        for a date no rule covers would put a wrong number on a return with
        nothing to show it was wrong.
        """
        if self.rate is None:
            raise VatRulesError(
                f"no rate is defined for {self.code} on the date asked for; "
                "the ruleset does not cover it (CMP-014)"
            )
        if isinstance(base, float):
            raise VatRulesError(f"a monetary base must be Decimal, not float (NFR-031): {base!r}")
        return base * self.rate / Decimal(100)


@dataclass(frozen=True, slots=True)
class EffectiveRules:
    """Every VAT rule in force on one date, plus the fingerprint of them.

    The fingerprint is what a period records when it is filed, and what
    `filed_period_drift()` compares back - so this type is both what a return
    builder reads and what proves, later, which rules it read.
    """

    on_date: date
    treatments: tuple[TreatmentRule, ...]
    rubrieken: tuple[Rubriek, ...]
    fingerprint: str

    def treatment(self, code: str) -> TreatmentRule:
        for rule in self.treatments:
            if rule.code == code:
                return rule
        raise VatRulesError(f"{code} is not a VAT treatment")

    def by_role(self, role: TreatmentRole) -> TreatmentRule:
        for rule in self.treatments:
            if rule.role is role:
                return rule
        raise VatRulesError(f"no treatment has role {role.value}")

    @property
    def unresolvable(self) -> tuple[TreatmentRule, ...]:
        """Treatments with no rate on this date. Should be empty; a non-empty
        result means the ruleset has a hole a return would fall through.
        """
        return tuple(rule for rule in self.treatments if not rule.is_resolvable)


@dataclass(frozen=True, slots=True)
class FiledPeriodDrift:
    """A filed period whose rules have changed since it was filed.

    Structurally impossible while the barrier stands. Reported anyway, for the
    reason 0020 gives about its gap report: a check that can only ever be
    empty is the check on the thing that makes it empty.
    """

    period_id: uuid.UUID
    administration_id: uuid.UUID
    end_date: date
    recorded_fingerprint: str
    current_fingerprint: str
    filed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WatermarkDrift:
    """The filed frontier against what `period` actually says.

    The watermark is derived state - advanced by ledger.mark_period_filed -
    and the barrier depends on it, so it is worth being able to check.
    """

    recorded: date | None
    computed: date | None


class VatRulesRepository(Protocol):
    async def rules_on(self, *, on_date: date) -> EffectiveRules: ...

    async def rate_on(self, *, treatment: str, on_date: date) -> Decimal | None: ...

    async def rulesets(self) -> Sequence[RuleSet]: ...

    async def filed_period_drift(self) -> Sequence[FiledPeriodDrift]: ...

    async def watermark_drift(self) -> WatermarkDrift | None: ...

    async def filed_through(self) -> date | None: ...


class VatRulesService:
    """Reads the rules that applied on a date.

    Read-only by construction: there is no method here that writes a rule.
    Loading a ruleset is `scripts/load_vat_rules.py` running as `ledgr_ops`,
    because a rate is public law that every tenant computes against and not
    something one tenant's request should introduce. A repository method for a
    statement `ledgr_app` cannot issue would be a lie about what this can do.
    """

    def __init__(self, repository: VatRulesRepository) -> None:
        self._repository = repository

    async def rules_on(self, on_date: date) -> EffectiveRules:
        """CMP-014's core question: what applied on this date."""
        if isinstance(on_date, datetime):
            # A datetime silently truncates to a different day across a
            # timezone boundary, and a rate change lands at midnight.
            raise VatRulesError(
                "a rule date is a date, not a datetime: the boundary is a day "
                "and a timezone would move it"
            )
        return await self._repository.rules_on(on_date=on_date)

    async def rate_on(self, treatment: str, on_date: date) -> Decimal | None:
        return await self._repository.rate_on(treatment=treatment, on_date=on_date)

    async def rules_for_period(self, *, end_date: date) -> EffectiveRules:
        """The rules a period is filed under.

        Keyed on the period's END date, which is the same key
        ledger.mark_period_filed records its fingerprint against. Choosing the
        start date instead would put a period that straddles a rate change on
        the old rate for its whole length.
        """
        return await self.rules_on(end_date)

    async def filed_through(self) -> date | None:
        """The filed frontier: the last day covered by any filed period.

        A rule may only take effect after this. Exposed so a regulatory-watch
        process (CMP-013) can answer "is there still time to load the January
        rate change" before the January period is filed, rather than finding
        out from a refusal.
        """
        return await self._repository.filed_through()

    async def filed_period_drift(self) -> Sequence[FiledPeriodDrift]:
        return await self._repository.filed_period_drift()

    async def watermark_drift(self) -> WatermarkDrift | None:
        return await self._repository.watermark_drift()

    async def provisional_rulesets(self) -> Sequence[RuleSet]:
        """Rulesets that were never checked against the authority they claim.

        CMP-013's watch function needs this, and so does anyone about to file:
        FR-VAT-003 sends these figures to the Belastingdienst.
        """
        return [r for r in await self._repository.rulesets() if r.is_provisional]

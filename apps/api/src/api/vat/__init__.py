"""VAT rules and their effective dating (PRD §6.6, §11).

Import from `api.vat` - never from `api.vat.rules_repository`, which names the
rule tables and belongs to `VatRulesService` alone. The same convention
`api.ledger` uses, for the same reason.
"""

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
    VatRulesRepository,
    VatRulesService,
    WatermarkDrift,
)

__all__ = [
    "EffectiveRules",
    "FiledPeriodDrift",
    "Rubriek",
    "RubriekKind",
    "RuleSet",
    "RuleSource",
    "TreatmentRole",
    "TreatmentRule",
    "VatRulesError",
    "VatRulesRepository",
    "VatRulesService",
    "WatermarkDrift",
]

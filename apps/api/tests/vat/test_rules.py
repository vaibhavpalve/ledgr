"""CMP-014 against the in-memory rules.

Runs tests/vat/rule_cases.py - the same table
tests/integration/test_effective_dated_rules.py runs against migration 0028 -
over the ruleset that actually ships, so "an invoice dated 2018-12-31 is 6%" is
a statement about the dataset rather than about a fixture.

The requirement has two halves and they fail differently:

    effective dating   the right row answers a question about a date
    the barrier        no row can be introduced that changes an answer already
                       filed against

Both are tested here and again against Postgres, because the barrier is the
half that a green suite over in-memory objects could easily be lying about.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.vat import (
    RubriekKind,
    RuleSource,
    TreatmentRole,
    VatRulesError,
    VatRulesService,
)
from tests.support.fake_vat_rules_repository import (
    InMemoryVatRulesRepository,
    load_document,
)
from tests.vat.rule_cases import (
    ALL_TREATMENTS,
    RATE_CASES,
    RUBRIEK_EXPECTATIONS,
    RateCase,
)


def loaded() -> InMemoryVatRulesRepository:
    repository = InMemoryVatRulesRepository()
    repository.load(load_document())
    return repository


def service(repository: InMemoryVatRulesRepository | None = None) -> VatRulesService:
    return VatRulesService(repository or loaded())


# ===========================================================================
# Effective dating
# ===========================================================================


@pytest.mark.parametrize("case", RATE_CASES, ids=lambda c: c.name)
async def test_the_rate_in_force(case: RateCase) -> None:
    rate = await service().rate_on(case.treatment, case.on_date)

    assert rate == case.expected, (
        f"{case.name}: {case.treatment} on {case.on_date} resolved to {rate}, "
        f"expected {case.expected}"
    )


async def test_a_missing_rate_is_not_a_rate_of_zero() -> None:
    """The distinction CMP-014 turns on at the edges.

    A date no rule covers has no rate. Returning 0 would let a return be
    computed at zero percent and look perfectly ordinary - the one failure
    mode nothing downstream can catch, because every later check would agree
    with it.
    """
    rules = await service().rules_on(date(2000, 12, 31))
    standard = rules.by_role(TreatmentRole.STANDARD)

    assert standard.rate is None
    assert not standard.is_resolvable
    assert standard in rules.unresolvable

    with pytest.raises(VatRulesError) as raised:
        standard.vat_on(Decimal("100.00"))
    assert "no rate is defined" in str(raised.value)


async def test_a_rate_change_does_not_move_what_came_before_it() -> None:
    """CMP-014 in one assertion. The 2019 change is loaded; 2018 keeps 6%."""
    rules_2018 = await service().rules_on(date(2018, 12, 31))
    rules_2019 = await service().rules_on(date(2019, 1, 1))

    assert rules_2018.by_role(TreatmentRole.REDUCED).rate == Decimal("6.000")
    assert rules_2019.by_role(TreatmentRole.REDUCED).rate == Decimal("9.000")
    assert rules_2018.fingerprint != rules_2019.fingerprint


async def test_vat_is_computed_from_the_rate_that_applied() -> None:
    """The rate is a multiplier, so it has to be Decimal all the way down."""
    before = (await service().rules_on(date(2018, 12, 31))).by_role(TreatmentRole.REDUCED)
    after = (await service().rules_on(date(2019, 1, 1))).by_role(TreatmentRole.REDUCED)

    assert before.vat_on(Decimal("100.00")) == Decimal("6.00")
    assert after.vat_on(Decimal("100.00")) == Decimal("9.00")


async def test_a_float_base_is_refused() -> None:
    """NFR-031 at the one boundary a rate is used."""
    standard = (await service().rules_on(date(2026, 6, 30))).by_role(TreatmentRole.STANDARD)

    with pytest.raises(VatRulesError) as raised:
        standard.vat_on(100.0)  # type: ignore[arg-type]
    assert "NFR-031" in str(raised.value)


async def test_a_datetime_is_refused_as_a_rule_date() -> None:
    """A rate change lands at midnight, and a datetime carries a timezone that
    can move it across the boundary. The type is the guard.
    """
    import datetime as dt

    with pytest.raises(VatRulesError) as raised:
        await service().rules_on(dt.datetime(2019, 1, 1))  # type: ignore[arg-type]
    assert "not a datetime" in str(raised.value)


async def test_every_treatment_the_chart_can_name_has_a_rule() -> None:
    """0024 lets ledger_account.default_vat_code be any of FR-AR-002's eight,
    so a ruleset covering seven leaves an account nobody can price.
    """
    rules = await service().rules_on(date(2026, 6, 30))

    assert {rule.code for rule in rules.treatments} == set(ALL_TREATMENTS)
    assert rules.unresolvable == ()
    assert len({rule.role for rule in rules.treatments}) == len(ALL_TREATMENTS)


@pytest.mark.parametrize("treatment", ALL_TREATMENTS)
async def test_each_treatment_maps_to_a_rubriek(treatment: str) -> None:
    """FR-VAT-001's structure, not its correctness - which is that a mapping
    resolves at all, and that the seller-side VAT box is absent exactly where
    the seller charges no VAT.
    """
    rules = await service().rules_on(date(2026, 6, 30))
    rule = rules.treatment(treatment)
    turnover, vat_box = RUBRIEK_EXPECTATIONS[treatment]

    assert rule.turnover_rubriek == turnover
    assert rule.vat_rubriek == vat_box


async def test_the_rubriek_set_is_the_official_boxes() -> None:
    rules = await service().rules_on(date(2026, 6, 30))
    codes = {r.code for r in rules.rubrieken}

    assert codes == {
        "1a",
        "1b",
        "1c",
        "1d",
        "1e",
        "2a",
        "3a",
        "3b",
        "3c",
        "4a",
        "4b",
        "5a",
        "5b",
    }
    assert next(r for r in rules.rubrieken if r.code == "5a").kind is RubriekKind.SUBTOTAL
    assert next(r for r in rules.rubrieken if r.code == "5b").kind is RubriekKind.INPUT


# ===========================================================================
# "Retroactively changing a rate must not alter a filed period"
# ===========================================================================


async def test_a_rate_may_be_introduced_ahead_of_the_frontier() -> None:
    """The half that has to keep working. A rate change announced for next
    year is the normal case, and refusing it would make the barrier useless.
    """
    repository = loaded()
    repository.file_period(end_date=date(2026, 3, 31))

    repository.add_rate(treatment="btw_21", valid_from=date(2027, 1, 1), rate=Decimal("23.000"))

    assert await service(repository).rate_on("btw_21", date(2027, 6, 1)) == Decimal("23.000")


async def test_a_rate_behind_the_frontier_is_refused() -> None:
    """CMP-014's second sentence, and the reason the frontier exists."""
    repository = loaded()
    repository.file_period(end_date=date(2026, 3, 31))

    with pytest.raises(VatRulesError) as raised:
        repository.add_rate(
            treatment="btw_21", valid_from=date(2026, 1, 15), rate=Decimal("25.000")
        )

    assert "already filed through 2026-03-31" in str(raised.value)
    assert "suppletie" in str(raised.value), (
        "the refusal has to name the route that IS open, or it is a dead end "
        "(PRD design principle D5)"
    )


async def test_the_frontier_is_the_latest_filed_period_not_the_last_filed() -> None:
    """Periods are not filed in date order. A late Q1 filing must not pull the
    barrier back over Q2, which a plain assignment would.
    """
    repository = loaded()
    repository.file_period(end_date=date(2026, 6, 30))
    repository.file_period(end_date=date(2026, 3, 31))

    assert await service(repository).filed_through() == date(2026, 6, 30)

    with pytest.raises(VatRulesError):
        repository.add_rate(treatment="btw_21", valid_from=date(2026, 5, 1), rate=Decimal("25.000"))


async def test_nothing_filed_means_nothing_to_protect() -> None:
    """A fresh system has no frontier, and a ruleset with a 2001 floor row has
    to be loadable into it.
    """
    repository = InMemoryVatRulesRepository()

    repository.load(load_document())

    assert await service(repository).filed_through() is None
    assert await service(repository).rate_on("btw_21", date(2005, 1, 1)) == Decimal("19.000")


async def test_a_filed_period_keeps_the_rules_it_was_filed_under() -> None:
    """The whole requirement, end to end: file, then change the rate ahead of
    the frontier, and the filed period's own answer does not move.
    """
    repository = loaded()
    filed = repository.file_period(end_date=date(2026, 3, 31))
    before = await service(repository).rules_for_period(end_date=date(2026, 3, 31))

    repository.add_rate(treatment="btw_21", valid_from=date(2026, 7, 1), rate=Decimal("23.000"))

    after = await service(repository).rules_for_period(end_date=date(2026, 3, 31))

    assert after.fingerprint == before.fingerprint == filed.recorded_fingerprint
    assert after.by_role(TreatmentRole.STANDARD).rate == Decimal("21.000")
    assert not await service(repository).filed_period_drift()


async def test_drift_is_reported_when_the_barrier_is_bypassed() -> None:
    """A detector that cannot detect is worse than none, so the barrier is
    stepped around deliberately here - the way a dropped trigger or a superuser
    would - and the drift must be reported.
    """
    repository = loaded()
    repository.file_period(end_date=date(2026, 3, 31))

    # Straight into the list, past add_rate() and its guard.
    from tests.support.fake_vat_rules_repository import FakeRate

    repository.rates.append(
        FakeRate(treatment="btw_21", valid_from=date(2026, 2, 1), rate=Decimal("25.000"))
    )

    drift = await service(repository).filed_period_drift()

    assert len(drift) == 1
    assert drift[0].end_date == date(2026, 3, 31)
    assert drift[0].recorded_fingerprint != drift[0].current_fingerprint


async def test_a_rule_cannot_be_restated_at_a_date_it_already_covers() -> None:
    """Append-only, as the primary key sees it: one rate per treatment per
    date, so superseding is always a new date rather than an edit.
    """
    repository = loaded()

    with pytest.raises(VatRulesError) as raised:
        repository.add_rate(treatment="btw_9", valid_from=date(2019, 1, 1), rate=Decimal("12.000"))

    assert "append-only" in str(raised.value)


# ===========================================================================
# Provenance (CMP-013, FR-VAT-003)
# ===========================================================================


async def test_the_shipped_ruleset_is_reported_as_provisional() -> None:
    """FR-VAT-003 sends these figures to the Belastingdienst. A ruleset nobody
    checked against the authority it claims has to be visible before then.
    """
    provisional = await service().provisional_rulesets()

    assert len(provisional) == 1
    assert provisional[0].source is RuleSource.PROVISIONAL
    assert provisional[0].version == "nl-2019-01"


async def test_a_rate_as_a_json_number_is_refused() -> None:
    """NFR-031 at the loader boundary: json.dumps of a Python float produces a
    number, and a rate that has already lost precision multiplies that loss
    across every line it touches.
    """
    document = load_document()
    document["rates"] = [{"treatment": "btw_21", "valid_from": "2001-01-01", "rate": 21.0}]

    with pytest.raises(VatRulesError) as raised:
        InMemoryVatRulesRepository().load(document)

    assert "NFR-031" in str(raised.value)


async def test_the_watermark_is_checkable_against_the_periods() -> None:
    """The barrier depends on derived state, so the derivation is checkable."""
    repository = loaded()
    repository.file_period(end_date=date(2026, 3, 31))

    assert await service(repository).watermark_drift() is None

    repository.watermark = date(2020, 1, 1)  # as a bad migration might
    drift = await service(repository).watermark_drift()

    assert drift is not None
    assert drift.recorded == date(2020, 1, 1)
    assert drift.computed == date(2026, 3, 31)

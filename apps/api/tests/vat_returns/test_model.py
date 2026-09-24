"""FR-VAT-001/002/011: the return builder, against the REAL shipped ruleset.

The rules come from `InMemoryVatRulesRepository` loaded with apps/api/data/vat/nl-vat-rules.json,
so "a 21% sale lands in 1a" is a statement about the data that ships, not about a fixture.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from api.vat.rules import EffectiveRules
from api.vat_returns.model import (
    AccountRole,
    BoxLine,
    CheckCode,
    PeriodFacts,
    Severity,
    VatLineTotal,
    VatReturn,
    box_lines_for,
    build_return,
    evaluate_checks,
    round_down,
    round_up,
)
from tests.support.fake_vat_rules_repository import InMemoryVatRulesRepository, load_document

START = date(2026, 7, 1)
END = date(2026, 9, 30)
AFTER = PeriodFacts(today=date(2026, 10, 5))

R = AccountRole


@pytest.fixture
async def rules() -> EffectiveRules:
    repository = InMemoryVatRulesRepository()
    repository.load(load_document())
    return await repository.rules_on(on_date=END)


def line(treatment: str, role: AccountRole, *, debit: str = "0", credit: str = "0") -> VatLineTotal:
    return VatLineTotal(
        vat_treatment=treatment, role=role, debit=Decimal(debit), credit=Decimal(credit)
    )


def build(rules: EffectiveRules, *lines: VatLineTotal, facts: PeriodFacts = AFTER) -> VatReturn:
    return build_return(period_start=START, period_end=END, totals=lines, rules=rules, facts=facts)


def figures(result: VatReturn, code: str) -> tuple[Decimal | None, Decimal | None]:
    box = result.box(code)
    assert box is not None, f"box {code} missing"
    return box.turnover_rounded, box.vat_rounded


async def test_a_standard_rate_sale_and_a_purchase(rules: EffectiveRules) -> None:
    result = build(
        rules,
        line("btw_21", R.REVENUE, credit="1000.00"),
        line("btw_21", R.VAT_OUTPUT, credit="210.00"),
        line("btw_21", R.OTHER, debit="100.00"),
        line("btw_21", R.VAT_INPUT, debit="21.00"),
    )
    assert figures(result, "1a") == (Decimal("1000"), Decimal("210"))
    assert figures(result, "5a") == (None, Decimal("210"))
    assert figures(result, "5b") == (None, Decimal("21"))
    assert (result.output_vat, result.input_vat, result.total_due) == (
        Decimal("210"),
        Decimal("21"),
        Decimal("189"),
    )
    # A purchase's base under an ordinary treatment is not on the return anywhere.
    assert all(b.turnover in (None, Decimal("0.00")) for b in result.boxes if b.code != "1a")
    assert result.can_be_filed


async def test_rounding_is_in_the_filers_favour(rules: EffectiveRules) -> None:
    result = build(
        rules,
        line("btw_21", R.REVENUE, credit="1000.99"),
        line("btw_21", R.VAT_OUTPUT, credit="210.21"),
        line("btw_21", R.VAT_INPUT, debit="20.01"),
    )
    box = result.box("1a")
    assert box is not None
    assert (box.turnover, box.turnover_rounded) == (Decimal("1000.99"), Decimal("1000"))
    assert (box.vat, box.vat_rounded) == (Decimal("210.21"), Decimal("210"))
    assert figures(result, "5b") == (None, Decimal("21"))
    assert result.total_due == Decimal("189")


def test_rounding_helpers_on_negative_amounts() -> None:
    # A period dominated by credit notes: "down" still means in the filer's favour.
    assert round_down(Decimal("-10.50")) == Decimal("-11")
    assert round_up(Decimal("-10.50")) == Decimal("-10")


async def test_a_credit_note_reduces_its_box(rules: EffectiveRules) -> None:
    result = build(
        rules,
        line("btw_21", R.REVENUE, credit="1000.00"),
        line("btw_21", R.VAT_OUTPUT, credit="210.00"),
        line("btw_21", R.REVENUE, debit="200.00"),
        line("btw_21", R.VAT_OUTPUT, debit="42.00"),
    )
    assert figures(result, "1a") == (Decimal("800"), Decimal("168"))


async def test_reduced_rate_zero_rate_and_domestic_reverse_charge_sales(
    rules: EffectiveRules,
) -> None:
    result = build(
        rules,
        line("btw_9", R.REVENUE, credit="500.00"),
        line("btw_9", R.VAT_OUTPUT, credit="45.00"),
        line("btw_0", R.REVENUE, credit="300.00"),
        line("btw_verlegd", R.REVENUE, credit="700.00"),
    )
    assert figures(result, "1b") == (Decimal("500"), Decimal("45"))
    # 1e has no VAT column: None, not zero.
    assert figures(result, "1e") == (Decimal("1000"), None)


async def test_exempt_turnover_is_not_on_the_return_but_is_not_lost(rules: EffectiveRules) -> None:
    result = build(rules, line("btw_vrijgesteld", R.REVENUE, credit="400.00"))
    assert figures(result, "1e") == (Decimal("0"), None)
    assert result.exempt_turnover == Decimal("400.00")


async def test_eu_and_export_sales(rules: EffectiveRules) -> None:
    result = build(
        rules,
        line("btw_icp", R.REVENUE, credit="2000.00"),
        line("btw_export", R.REVENUE, credit="3000.00"),
    )
    assert figures(result, "3b") == (Decimal("2000"), None)
    assert figures(result, "3a") == (Decimal("3000"), None)
    assert result.total_due == Decimal("0")


async def test_a_reverse_charged_eu_purchase_is_self_assessed_and_deducted(
    rules: EffectiveRules,
) -> None:
    """Google Ads from Ireland: no VAT on the invoice, 21% due in 4b and deductible in 5b."""
    result = build(rules, line("btw_icp", R.OTHER, debit="100.00"))
    assert figures(result, "4b") == (Decimal("100"), Decimal("21"))
    assert figures(result, "5a") == (None, Decimal("21"))
    assert figures(result, "5b") == (None, Decimal("21"))
    assert result.total_due == Decimal("0")


async def test_domestic_reverse_charge_and_non_eu_purchases(rules: EffectiveRules) -> None:
    result = build(
        rules,
        line("btw_verlegd", R.OTHER, debit="1000.00"),
        line("btw_export", R.OTHER, debit="50.00"),
    )
    assert figures(result, "2a") == (Decimal("1000"), Decimal("210"))
    assert figures(result, "4a") == (Decimal("50"), Decimal("10"))  # 10.50 rounded down
    assert figures(result, "5b") == (None, Decimal("221"))  # 220.50 rounded up
    assert result.total_due == Decimal("-1")


async def test_the_margin_scheme_blocks_filing(rules: EffectiveRules) -> None:
    result = build(rules, line("btw_marge", R.REVENUE, credit="500.00"))
    [check] = result.blocking
    assert check.code is CheckCode.UNPLACED_TREATMENT
    assert check.detail == ("btw_marge",)
    assert not result.can_be_filed


async def test_boxes_follow_the_forms_order_with_5a_before_5b(rules: EffectiveRules) -> None:
    result = build(rules)
    codes = [box.code for box in result.boxes]
    assert codes == ["1a", "1b", "1c", "1d", "1e", "2a", "3a", "3b", "3c", "4a", "4b", "5a", "5b"]
    assert result.box("3a") is not None and result.box("3a").vat is None  # type: ignore[union-attr]
    assert result.total_due == Decimal("0")


def test_a_period_still_running_cannot_be_filed() -> None:
    checks = evaluate_checks(period_end=END, facts=PeriodFacts(today=END))
    assert [(c.code, c.severity) for c in checks] == [
        (CheckCode.PERIOD_NOT_ENDED, Severity.BLOCKING)
    ]


def test_every_warning_fires_with_its_count() -> None:
    facts = PeriodFacts(
        today=date(2026, 10, 5),
        unposted_purchases=2,
        draft_sales_invoices=1,
        unreconciled_bank=7,
        untagged_vat_lines=1,
        untagged_vat_amount=Decimal("12.00"),
        earlier_unfiled=("2026-Q2",),
        ruleset_provisional=True,
    )
    checks = evaluate_checks(period_end=END, facts=facts)
    assert {c.code: c.count for c in checks} == {
        CheckCode.PROVISIONAL_RULESET: None,
        CheckCode.UNPOSTED_PURCHASES: 2,
        CheckCode.DRAFT_SALES_INVOICES: 1,
        CheckCode.UNRECONCILED_BANK: 7,
        CheckCode.UNTAGGED_VAT_POSTINGS: 1,
        CheckCode.EARLIER_PERIOD_UNFILED: 1,
    }
    assert all(c.severity is Severity.WARNING for c in checks)


def _raw(treatment: str, role: AccountRole, amount: str, number: int) -> BoxLine:
    return BoxLine(
        entry_id=uuid.uuid4(),
        entry_number=number,
        entry_date=date(2026, 8, 1),
        description=f"entry {number}",
        document_reference=None,
        source_system="test",
        account_code="8000",
        account_name="Omzet",
        vat_treatment=treatment,
        role=role,
        amount=Decimal(amount),
        column="",
    )


async def test_drill_down_reconciles_to_the_box(rules: EffectiveRules) -> None:
    """FR-VAT-011: the lines behind a box sum to its exact figure."""
    raw = [
        _raw("btw_21", R.REVENUE, "1000.00", 1),
        _raw("btw_21", R.VAT_OUTPUT, "210.00", 1),
        _raw("btw_9", R.REVENUE, "100.00", 2),
        _raw("btw_icp", R.OTHER, "100.00", 3),
        _raw("btw_21", R.VAT_INPUT, "21.00", 4),
    ]
    in_1a = box_lines_for("1a", raw, rules)
    assert sum((ln.amount for ln in in_1a if ln.column == "turnover"), Decimal(0)) == Decimal(
        "1000.00"
    )
    assert sum((ln.amount for ln in in_1a if ln.column == "vat"), Decimal(0)) == Decimal("210.00")
    assert {ln.entry_number for ln in box_lines_for("4b", raw, rules)} == {3}
    assert {ln.entry_number for ln in box_lines_for("5b", raw, rules)} == {3, 4}
    assert {ln.entry_number for ln in box_lines_for("5a", raw, rules)} == {1, 3}

"""SI-13 - which OB-aangifte box an invoice lands in, shown while it is built.

Run against the REAL shipped ruleset (apps/api/data/vat) through the in-memory
rules repository, for the reason that repository gives: "a standard-rate supply
reports in 1a" is a statement about the dataset that actually ships, and a
hand-built fixture would only prove the function agrees with itself.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from api.i18n.language import Language
from api.invoicing.model import InvoiceView
from api.invoicing.rubriek_preview import RubriekPreview, _box_order, preview
from api.invoicing.vat import VatGroup
from api.vat.rules import EffectiveRules, TreatmentRole
from tests.invoicing.test_duplicates import _invoice, _setup
from tests.support.fake_vat_rules_repository import InMemoryVatRulesRepository, load_document

ON = date(2026, 9, 9)


@pytest.fixture
async def rules() -> EffectiveRules:
    repository = InMemoryVatRulesRepository()
    repository.load(load_document())
    return await repository.rules_on(on_date=ON)


def group(
    treatment: str, role: TreatmentRole, taxable: str, vat: str = "0.00", rate: str | None = None
) -> VatGroup:
    return VatGroup(
        treatment=treatment,
        role=role,
        rate=Decimal(rate) if rate is not None else None,
        taxable=Decimal(taxable),
        vat=Decimal(vat),
    )


STANDARD = group("btw_21", TreatmentRole.STANDARD, "950.00", "199.50", "21")
REDUCED = group("btw_9", TreatmentRole.REDUCED, "100.00", "9.00", "9")
ZERO = group("btw_0", TreatmentRole.ZERO, "40.00", rate="0")
EXEMPT = group("btw_vrijgesteld", TreatmentRole.EXEMPT, "10.00", rate="0")
EXPORT = group("btw_export", TreatmentRole.EXPORT, "500.00", rate="0")
MARGIN = group("btw_marge", TreatmentRole.MARGIN, "300.00")


def test_a_standard_rate_supply_lands_in_1a_with_its_vat(rules: EffectiveRules) -> None:
    result = preview([STANDARD], rules)

    (box,) = result.boxes
    assert box.code == "1a"
    assert box.turnover == Decimal("950.00")
    assert box.vat == Decimal("199.50")
    assert box.treatments == ("btw_21",)
    assert result.unplaced_treatments == ()


def test_boxes_come_out_in_form_order_and_only_the_ones_used(rules: EffectiveRules) -> None:
    result = preview([EXPORT, ZERO, STANDARD, REDUCED], rules)

    assert [box.code for box in result.boxes] == ["1a", "1b", "1e", "3a"]


def test_a_box_with_no_vat_column_has_no_vat_rather_than_zero(rules: EffectiveRules) -> None:
    """1e and 3a report turnover only. `None` is 'this box has no VAT column';
    `Decimal(0)` would say 'it has one and it is empty'."""
    by_code = {box.code: box for box in preview([ZERO, EXPORT], rules).boxes}

    assert by_code["1e"].vat is None
    assert by_code["3a"].vat is None


def test_a_vat_box_with_a_zero_amount_still_shows_it(rules: EffectiveRules) -> None:
    """A treatment that HAS a VAT box reports its amount even when it is zero -
    an empty cell would read as 'nothing was decided'."""
    zero_vat = group("btw_21", TreatmentRole.STANDARD, "0.00", "0.00", "21")
    (box,) = preview([zero_vat], rules).boxes

    assert box.code == "1a"
    assert box.vat == Decimal("0.00")


def test_two_treatments_in_one_box_are_summed(rules: EffectiveRules) -> None:
    (box,) = preview([ZERO, EXEMPT], rules).boxes

    assert box.code == "1e"
    assert box.turnover == Decimal("50.00")
    assert box.treatments == ("btw_0", "btw_vrijgesteld")


def test_the_margin_scheme_is_not_placed_in_a_box(rules: EffectiveRules) -> None:
    """What the return reports for a margin supply is the MARGIN. The invoice
    carries the sale price, so showing it in 1a would be a wrong number, and a
    zero would be a wrong statement. It is called out instead."""
    result = preview([MARGIN, STANDARD], rules)

    assert [box.code for box in result.boxes] == ["1a"]
    assert result.boxes[0].turnover == Decimal("950.00")
    assert result.unplaced_treatments == ("btw_marge",)


def test_a_treatment_the_ruleset_does_not_know_is_unplaced_not_dropped(
    rules: EffectiveRules,
) -> None:
    result = preview([group("btw_nieuw", TreatmentRole.STANDARD, "10.00", "2.10", "21")], rules)

    assert result.boxes == ()
    assert result.unplaced_treatments == ("btw_nieuw",)


def test_a_treatment_with_no_box_on_the_date_is_unplaced(rules: EffectiveRules) -> None:
    """The mapping is effective-dated. Before it exists there is no box, and
    the honest answer is to say so."""
    unmapped = tuple(
        replace(rule, turnover_rubriek=None, vat_rubriek=None) if rule.code == "btw_21" else rule
        for rule in rules.treatments
    )
    result = preview([STANDARD], replace(rules, treatments=unmapped))

    assert result.boxes == ()
    assert result.unplaced_treatments == ("btw_21",)


def test_the_box_title_follows_the_readers_language(rules: EffectiveRules) -> None:
    (box,) = preview([STANDARD], rules).boxes

    assert box.description(Language.EN) == "Supplies at the standard rate"
    assert box.description(Language.NL) == box.description_nl
    assert box.description_nl != box.description(Language.EN)


def test_the_title_falls_back_to_dutch_when_there_is_no_english_one() -> None:
    from api.invoicing.rubriek_preview import BoxPreview

    box = BoxPreview("1a", "Leveringen", None, Decimal(0), None, ())

    assert box.description(Language.EN) == "Leveringen"


def test_form_order_is_numeric_not_alphabetical() -> None:
    assert sorted(["10a", "2a", "1e", "1a", "1b"], key=_box_order) == [
        "1a",
        "1b",
        "1e",
        "2a",
        "10a",
    ]


def test_an_invoice_with_no_groups_previews_nothing(rules: EffectiveRules) -> None:
    assert preview([], rules) == RubriekPreview()


def test_amounts_are_decimal_never_float(rules: EffectiveRules) -> None:
    for box in preview([STANDARD, REDUCED, ZERO], rules).boxes:
        assert isinstance(box.turnover, Decimal)
        assert box.vat is None or isinstance(box.vat, Decimal)


# ===========================================================================
# In the service: shown on every status, and informational only
# ===========================================================================


async def test_a_draft_view_carries_the_preview() -> None:
    view = await _setup([]).view()

    (box,) = view.rubriek_preview.boxes
    assert box.code == "1a"
    assert box.turnover == view.net


async def test_an_issued_invoice_shows_where_it_landed() -> None:
    from api.invoicing.model import InvoiceStatus

    view = await _setup([], status=InvoiceStatus.ISSUED, invoice_number=3).view()

    assert [box.code for box in view.rubriek_preview.boxes] == ["1a"]


async def test_an_empty_draft_previews_nothing_and_asks_for_no_rules() -> None:
    setup = _setup([], lines=())
    asked: list[date] = []
    original = setup.repository.effective_rules_on

    async def spy(*, on_date: date) -> EffectiveRules:
        asked.append(on_date)
        return await original(on_date=on_date)

    setup.repository.effective_rules_on = spy  # type: ignore[method-assign]
    view = await setup.view()

    assert view.rubriek_preview == RubriekPreview()
    assert asked == []


async def test_the_rules_are_read_as_of_the_invoice_date() -> None:
    """Effective-dated (CMP-014): a back-dated invoice reports into the box the
    mapping gave THEN."""
    setup = _setup([], invoice_date=date(2019, 6, 1))
    seen: list[date] = []
    original = setup.repository.effective_rules_on

    async def spy(*, on_date: date) -> EffectiveRules:
        seen.append(on_date)
        return await original(on_date=on_date)

    setup.repository.effective_rules_on = spy  # type: ignore[method-assign]
    await setup.view()

    assert seen == [date(2019, 6, 1)]


def test_the_view_serialises_the_preview_and_the_duplicate_warnings(
    rules: EffectiveRules,
) -> None:
    """The wire shape packages/shared-types promises (SI-12 and SI-13), driven
    through the real `_view_json` - nothing else exercises it."""
    from api.invoicing.duplicates import InvoiceDuplicateStrength, InvoiceDuplicateWarning
    from api.invoicing.routes import _view_json

    view = InvoiceView(
        invoice=_invoice(uuid.uuid4(), uuid.uuid4()),
        groups=(STANDARD, ZERO),
        net=Decimal("990.00"),
        vat=Decimal("199.50"),
        gross=Decimal("1189.50"),
        rubriek_preview=preview([STANDARD, ZERO, MARGIN], rules),
        duplicate_warnings=(
            InvoiceDuplicateWarning(
                invoice_id=uuid.UUID(int=7),
                strength=InvoiceDuplicateStrength.SAME_TOTAL,
                status="issued",
                invoice_reference="2026-4",
                invoice_date=date(2026, 9, 1),
                net_total=Decimal("990.00"),
            ),
        ),
    )

    body = _view_json(view, Language.EN)

    assert body["rubriek_preview"] == {
        "boxes": [
            {
                "code": "1a",
                "description": "Supplies at the standard rate",
                "turnover_amount": "950.00",
                "vat_amount": "199.50",
                "treatments": ["btw_21"],
            },
            {
                "code": "1e",
                "description": "Supplies at 0% or not taxed with you",
                "turnover_amount": "40.00",
                "vat_amount": None,
                "treatments": ["btw_0"],
            },
        ],
        "unplaced_treatments": ["btw_marge"],
    }
    (warning,) = body["duplicate_warnings"]  # type: ignore[misc]
    assert warning["strength"] == "same_total"
    assert warning["status"] == "issued"
    assert warning["invoice_reference"] == "2026-4"
    assert warning["net_amount"] == "990.00"
    assert "2026-4" in warning["message"]


def test_the_preview_never_gates_the_issue(rules: EffectiveRules) -> None:
    """A view with a full preview and no statutory failures can be issued, and
    an unplaced treatment does not change that: SI-13 informs, FR-AR-003 gates."""
    invoice = _invoice(uuid.uuid4(), uuid.uuid4())
    view = InvoiceView(
        invoice=invoice,
        groups=(STANDARD, MARGIN),
        net=Decimal("1250.00"),
        vat=Decimal("199.50"),
        gross=Decimal("1449.50"),
        rubriek_preview=preview([STANDARD, MARGIN], rules),
    )

    assert view.rubriek_preview.boxes and view.rubriek_preview.unplaced_treatments
    assert view.can_be_issued is True

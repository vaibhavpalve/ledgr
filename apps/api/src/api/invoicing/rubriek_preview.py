"""SI-13: which OB-aangifte box an invoice will land in, shown while it is built.

    SI-13  Show which VAT return box an invoice will land in while it's being
           built, not only after filing.

A bookkeeper choosing a VAT treatment on a line is, without saying so, choosing
a box on the return: a supply at the standard rate is reported in 1a, a
reverse-charged one in 2a, an export in 3a. Today the consequence is discovered
when the return is prepared. This module states it up front, per box, from the
same source the return builder reads - `vat.rules_on`, the effective-dated
treatment-to-rubriek mapping (ADR-027) - so the preview cannot disagree with
what filing will do, and follows a rule change on its own.

--- What it is and is not ---

An invoice's own contribution, computed from its VAT groups. It is NOT the
return, and it says nothing about anything else in the period. In particular:

* Amounts are the invoice's exact figures. The return's own rounding rules
  (FR-VAT-*) are not applied here.
* A treatment whose contribution cannot be shown here is listed in
  `unplaced_treatments` rather than dropped or guessed at, for one of two
  reasons. Either the mapping has no turnover box for it on the invoice date - a
  data gap in the ruleset, the same species as a missing rate - or it is the
  margin scheme, where what the return reports is the MARGIN, not the sale
  price on the invoice, so the invoice's own figures would put a wrong number
  in the box. A client should say "cannot be placed", not show a zero.
* It is only as authoritative as the ruleset loaded. A provisional ruleset
  (FR-VAT-003) is fine to preview against and is not a basis for a filed return.

Both the turnover and the VAT of a box use ONE code (`1a` carries both columns
on the form), so a row holds both amounts. `vat` is None for a box with no VAT
column - 1e, 3a, 3b - which is different from a VAT column that happens to be
zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from api.i18n.language import Language
from api.invoicing.vat import VatGroup
from api.vat.rules import EffectiveRules, TreatmentRole

__all__ = ["BoxPreview", "RubriekPreview", "preview"]


@dataclass(frozen=True, slots=True)
class BoxPreview:
    """One box, with what this invoice puts in it."""

    code: str
    description_nl: str
    description_en: str | None
    #: The taxable amount reported in this box's turnover column.
    turnover: Decimal
    #: The VAT reported in this box's VAT column; None where the box has none.
    vat: Decimal | None
    #: The treatment codes that feed it, so a client can say which lines.
    treatments: tuple[str, ...]

    def description(self, language: Language) -> str:
        """The box's title in the READER's language, Dutch when there is no
        English one - the box's official name is Dutch, so it is the fallback
        that is never wrong."""
        if language is Language.EN and self.description_en:
            return self.description_en
        return self.description_nl


@dataclass(frozen=True, slots=True)
class RubriekPreview:
    boxes: tuple[BoxPreview, ...] = ()
    #: Treatments on the invoice whose amounts cannot be shown in a box: none
    #: is mapped on its date, or it is the margin scheme.
    unplaced_treatments: tuple[str, ...] = ()


def _box_order(code: str) -> tuple[int, str]:
    """`1a` < `1b` < `1e` < `2a` < `10a`: by the number, then the letter. A
    plain string sort would put `10a` before `2a`."""
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 0, code)


def preview(groups: Sequence[VatGroup], rules: EffectiveRules) -> RubriekPreview:
    """The boxes `groups` will report into on `rules`' date.

    Pure. `rules` is `EffectiveRules` as of the invoice date - passed in, not
    looked up, so a return-builder test and a preview test can run the same
    fixture.
    """
    described = {box.code: box for box in rules.rubrieken}
    turnover: dict[str, Decimal] = {}
    vat: dict[str, Decimal] = {}
    feeding: dict[str, list[str]] = {}
    unplaced: list[str] = []

    known = {rule.code: rule for rule in rules.treatments}
    for group in groups:
        rule = known.get(group.treatment)
        if rule is None or rule.turnover_rubriek is None or group.role is TreatmentRole.MARGIN:
            unplaced.append(group.treatment)
            continue

        box = rule.turnover_rubriek
        turnover[box] = turnover.get(box, Decimal(0)) + group.taxable
        feeding.setdefault(box, []).append(group.treatment)

        # A treatment with a VAT box always has an amount for it, zero
        # included: the column exists for it and an empty cell would read as
        # "nothing was decided".
        if rule.vat_rubriek is not None:
            vat[rule.vat_rubriek] = vat.get(rule.vat_rubriek, Decimal(0)) + group.vat
            if rule.vat_rubriek != box:
                feeding.setdefault(rule.vat_rubriek, []).append(group.treatment)

    codes = sorted(set(turnover) | set(vat), key=_box_order)
    boxes = []
    for code in codes:
        definition = described.get(code)
        boxes.append(
            BoxPreview(
                code=code,
                description_nl=definition.description_nl if definition else code,
                description_en=definition.description_en if definition else None,
                turnover=turnover.get(code, Decimal(0)),
                vat=vat.get(code),
                treatments=tuple(sorted(set(feeding.get(code, ())))),
            )
        )
    return RubriekPreview(boxes=tuple(boxes), unplaced_treatments=tuple(sorted(set(unplaced))))

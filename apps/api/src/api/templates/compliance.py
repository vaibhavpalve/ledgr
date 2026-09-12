"""What an invoice template must carry before it may be saved - FR-TPL-009.

    FR-TPL-009  Statutory fields cannot be removed or hidden. The designer
                enforces the Dutch invoice content requirements (FR-AR-003):
                removing or obscuring a mandatory field is not offered as an
                option, and THE TEMPLATE CANNOT BE SAVED IN A NON-COMPLIANT
                STATE. The reason is shown inline, not as a generic error.

This is `api.invoicing.statutory`'s cousin, deliberately. FR-AR-003 gates
whether an INVOICE may be ISSUED; this gates whether a TEMPLATE may be SAVED.
Both are refusals rather than warnings, both report every failure at once
rather than one save at a time, and both resolve an identifier to a sentence
carrying what/why/action (D5) rather than leaving a client to invent one. The
parallel is intentional enough that this module borrows the shape almost
line for line - `TemplateField` next to `StatutoryField`, `TemplateViolation`
next to `StatutoryFailure`, `NotCompliant` next to `NotStatutoryCompliant` -
so that a reader who already understands one understands the other, and so
that a change to one shape prompts asking whether the other needs it too.

--- Why this is a gate rather than a warning ---

Every other property of a draft template is advisory: a colour combination
that fails contrast (`api.templates.model.ColorScheme.contrast_warnings`)
saves anyway, because print legibility is the author's judgment call and
somebody's brand book is allowed to be bold. Hiding a legally required column
or deleting the sentence that carries the supplier's VAT number is not a
judgment call - FR-TPL-009 says so explicitly - so this one refuses.

--- Every failure is reported at once ---

`check()` returns the whole list, for the reason `api.invoicing.statutory`
gives about `Expense.missing_fields`: somebody editing a template sees every
problem on one screen rather than discovering them one save attempt at a
time.

--- Every failure says what to DO about it (D5) ---

    D5  No dead ends. Every error message states what happened, why, and the
        specific next action. "Validation failed" is not an acceptable
        string.

`message_for` renders one; the route sends it beside the field so a client
has both the sentence and the identifier to attach it to - exactly the split
`api.invoicing.statutory.message_for` makes and for the same reason.

--- The list, and where it comes from ---

Two kinds of failure, both traceable to Wet OB 1968 art. 35a (implementing
EU VAT Directive art. 226) and both already enforced elsewhere in the system,
which is what makes them checkable here rather than a matter of judgment:

    a locked LINE-ITEM COLUMN is hidden      art. 35a(1)(h)/(i)/(j): quantity,
                                              unit price and the rate applied
                                              must appear on every line.
                                              `api.templates.model.
                                              STATUTORY_COLUMNS` names the
                                              three; mirrored by 0042's
                                              `invoice_template_column_
                                              statutory_visible` CHECK.
    a locked MERGE TAG is missing            art. 35a(1)(c) (VAT number) and
    from the `legal_identity` block,         the Handelsregisterwet (KvK
    in EITHER language                       number), both via the one free
                                              block FR-TPL-007 offers for
                                              them. `api.templates.model.
                                              REQUIRED_MERGE_TAGS` names the
                                              two tags; mirrored by 0042's
                                              `invoice_template_block_
                                              legal_identity_tags` CHECK.

NOT checked here, and deliberately, for the same reasons `api.invoicing.
statutory` gives about its own list:

  * whether the REMAINING prose in `legal_identity` (or any other block) is
    coherent, complete or well-written. FR-TPL-007's free text is free; this
    gate only refuses to let it lose the two tags the statute needs.
  * whether the customer's own VAT number will be present on a given invoice
    (art. 35a(1)(f), reverse charge / intra-Community). That is a fact about
    ONE INVOICE, resolved at issue by `api.invoicing.statutory`, not about a
    template that has not yet been used to render one.
  * the invoice number, invoice date and supplier's own name and address
    (art. 35a(1)(a), (b), (e)). Those are DOCUMENT facts a renderer supplies
    unconditionally on every layout FR-TPL-005 offers - there is no template
    control that could hide them, so there is nothing here to gate.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from api.i18n.catalogue import translate
from api.i18n.language import Language
from api.templates.model import (
    REQUIRED_MERGE_TAGS,
    STATUTORY_COLUMNS,
    ContentBlock,
    InvoiceTemplate,
    LineColumn,
)

__all__ = [
    "TemplateField",
    "TemplateViolation",
    "MESSAGE_KEYS",
    "NotCompliant",
    "check",
    "message_for",
    "describe",
]


class TemplateField(enum.Enum):
    """What is locked and why a save was refused - `api.invoicing.statutory.
    StatutoryField`'s counterpart, one level up (a template rather than a
    document).
    """

    QUANTITY_COLUMN = "quantity_column"
    UNIT_PRICE_COLUMN = "unit_price_column"
    VAT_RATE_COLUMN = "vat_rate_column"
    LEGAL_IDENTITY_VAT_TAG = "legal_identity_vat_tag"
    LEGAL_IDENTITY_KVK_TAG = "legal_identity_kvk_tag"


#: `LineColumn` members that lock, mapped to the field a violation reports.
#: Built from `STATUTORY_COLUMNS` rather than listed again by hand, so the two
#: modules cannot silently disagree about which three columns are statutory.
_COLUMN_FIELDS: dict[LineColumn, TemplateField] = {
    LineColumn.QUANTITY: TemplateField.QUANTITY_COLUMN,
    LineColumn.UNIT_PRICE: TemplateField.UNIT_PRICE_COLUMN,
    LineColumn.VAT_RATE: TemplateField.VAT_RATE_COLUMN,
}
assert set(_COLUMN_FIELDS) == STATUTORY_COLUMNS

#: The two `legal_identity` merge tags, mapped the same way.
_TAG_FIELDS: dict[str, TemplateField] = {
    "supplier_vat_number": TemplateField.LEGAL_IDENTITY_VAT_TAG,
    "supplier_kvk_number": TemplateField.LEGAL_IDENTITY_KVK_TAG,
}
assert set(_TAG_FIELDS) == REQUIRED_MERGE_TAGS[ContentBlock.LEGAL_IDENTITY]


@dataclass(frozen=True, slots=True)
class TemplateViolation:
    """One thing FR-TPL-009 will not let a template be saved without.

    `language` is set for a merge-tag violation (which of the block's two
    languages lost the tag) and None for a column violation, which has no
    per-language shape - the same optional-discriminator pattern
    `StatutoryFailure.line_position` uses for the same reason: a screen
    showing "the VAT number is missing" against a bilingual block has not
    said which language to go fix.
    """

    field: TemplateField
    language: Language | None = None


#: Fields whose message names which language's text lost the tag. Kept in
#: step with `TemplateViolation.language` for the reason `api.invoicing.
#: statutory._LINE_SCOPED` gives about line position: a language-scoped
#: message with no language is the dead end D5 rules out.
_LANGUAGE_SCOPED: frozenset[TemplateField] = frozenset(
    {TemplateField.LEGAL_IDENTITY_VAT_TAG, TemplateField.LEGAL_IDENTITY_KVK_TAG}
)

#: The merge tag literal each language-scoped field is about, for
#: interpolation into its message. Written out in full (`{{...}}`) because the
#: message names the exact string an author needs to retype, not a
#: paraphrase of it.
_TAG_LITERAL: dict[TemplateField, str] = {
    TemplateField.LEGAL_IDENTITY_VAT_TAG: "{{supplier_vat_number}}",
    TemplateField.LEGAL_IDENTITY_KVK_TAG: "{{supplier_kvk_number}}",
}

#: One catalogue key per field, exhaustive by construction - asserted complete
#: in tests/templates/test_compliance.py, the same discipline `api.invoicing.
#: statutory.MESSAGE_KEYS` holds itself to.
MESSAGE_KEYS: dict[TemplateField, str] = {
    TemplateField.QUANTITY_COLUMN: "invoice.template.locked.quantity_column",
    TemplateField.UNIT_PRICE_COLUMN: "invoice.template.locked.unit_price_column",
    TemplateField.VAT_RATE_COLUMN: "invoice.template.locked.vat_rate_column",
    TemplateField.LEGAL_IDENTITY_VAT_TAG: "invoice.template.locked.legal_identity_vat_tag",
    TemplateField.LEGAL_IDENTITY_KVK_TAG: "invoice.template.locked.legal_identity_kvk_tag",
}


def message_for(violation: TemplateViolation, language: Language) -> str:
    """What happened, why, and what to do about it - D5, exactly as
    `api.invoicing.statutory.message_for` does it for an issue-time failure.

    `language` is the READER's - whoever is editing the template - not either
    of the two languages `legal_identity`'s text is written in. Those two are
    named inside the message itself via `{language}`, using the endonym
    catalogue already shared with the language picker (`common.language.
    name.nl` / `.en`) rather than a translated demonym, so "Dutch" reads as
    "Nederlands" or "English" reads as "English" regardless of which language
    the surrounding sentence is in - the same choice a language picker makes
    for the same reason.
    """
    if violation.field in _LANGUAGE_SCOPED and violation.language is None:
        raise ValueError(
            f"{violation.field.value} is about a specific language and carries none. "
            f"A message that cannot say WHICH language is the dead end D5 rules out."
        )

    params: dict[str, object] = {}
    if violation.language is not None:
        params["language"] = translate(f"common.language.name.{violation.language.value}", language)
        params["tag"] = _TAG_LITERAL[violation.field]

    return translate(MESSAGE_KEYS[violation.field], language, **params)


def describe(
    violations: Sequence[TemplateViolation], language: Language
) -> tuple[tuple[TemplateViolation, str], ...]:
    """Every violation with its sentence, in the order `check()` found them -
    columns before merge tags, the order somebody would naturally work
    through a form from top (line items) to bottom (content blocks).
    """
    return tuple((violation, message_for(violation, language)) for violation in violations)


def check(template: InvoiceTemplate) -> tuple[TemplateViolation, ...]:
    """Everything that would make this template non-compliant, all at once.

    Empty means it may be saved. Takes the assembled `InvoiceTemplate` rather
    than raw request fields, the same choice `api.invoicing.statutory.check`
    makes by taking `InvoiceForIssue`: a pure function over a value is
    trivially testable and cannot itself reach a database.
    """
    violations: list[TemplateViolation] = []

    for column, field_ in _COLUMN_FIELDS.items():
        setting = template.columns.setting_for(column)
        if not setting.visible:
            violations.append(TemplateViolation(field_))

    legal_identity = template.block_text(ContentBlock.LEGAL_IDENTITY)
    text_nl = legal_identity.text_nl if legal_identity is not None else ""
    text_en = legal_identity.text_en if legal_identity is not None else ""

    for field_ in _TAG_FIELDS.values():
        literal = _TAG_LITERAL[field_]
        if literal not in text_nl:
            violations.append(TemplateViolation(field_, language=Language.NL))
        if literal not in text_en:
            violations.append(TemplateViolation(field_, language=Language.EN))

    return tuple(violations)


class NotCompliant(Exception):
    """FR-TPL-009. Carries every violation, so a screen shows them at once -
    `api.invoicing.model.NotStatutoryCompliant`'s counterpart.
    """

    def __init__(self, violations: Sequence[TemplateViolation]) -> None:
        self.violations = tuple(violations)
        super().__init__(
            "this template hides or omits content Dutch law requires and cannot be "
            f"saved (FR-TPL-009): {', '.join(v.field.value for v in self.violations)}"
        )

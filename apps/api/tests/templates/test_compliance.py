"""FR-TPL-009's gate: what blocks an invoice template from being saved.

Mirrors `tests/invoicing/test_statutory.py`'s builder-helper style
deliberately - see `api.templates.compliance`'s module docstring for why the
parallel is intentional.
"""

from __future__ import annotations

import uuid

import pytest

from api.i18n.language import Language
from api.templates.compliance import (
    MESSAGE_KEYS,
    NotCompliant,
    TemplateField,
    TemplateViolation,
    check,
    describe,
    message_for,
)
from api.templates.model import (
    BlockText,
    ColorScheme,
    ColumnLayout,
    ColumnSetting,
    ContentBlock,
    FontWeight,
    InvoiceTemplate,
    Layout,
    LetterSpacing,
    LineColumn,
    LineHeight,
    Logo,
    LogoPosition,
    LogoSize,
    TypeScale,
    Typography,
)

_LEGAL_IDENTITY_NL = "KvK {{supplier_kvk_number}} — btw-nr. {{supplier_vat_number}}"
_LEGAL_IDENTITY_EN = "KvK {{supplier_kvk_number}} — VAT no. {{supplier_vat_number}}"

_LANGUAGE_SCOPED_FIELDS = (
    TemplateField.LEGAL_IDENTITY_VAT_TAG,
    TemplateField.LEGAL_IDENTITY_KVK_TAG,
)


def a_column_layout(**hidden: bool) -> ColumnLayout:
    """All six columns visible by default; pass e.g. `quantity=False` to hide
    one - the same "builder with overrides" convention `test_statutory.py`'s
    `an_invoice` / `a_line` use, adapted to a fixed six-member set rather than
    free-form kwargs.
    """
    settings = []
    for position, column in enumerate(LineColumn, start=1):
        visible = hidden.get(column.value, True)
        settings.append(ColumnSetting(column=column, visible=visible, position=position))
    return ColumnLayout(tuple(settings))


def a_block(block: ContentBlock, *, text_nl: str = "", text_en: str = "") -> BlockText:
    return BlockText(block=block, text_nl=text_nl, text_en=text_en)


def default_blocks(overrides: dict[ContentBlock, BlockText] | None = None) -> tuple[BlockText, ...]:
    """A fully compliant set of five blocks, with `legal_identity` already
    carrying both locked merge tags - the same "compliant unless a test says
    otherwise" default `test_statutory.py`'s `an_invoice()` builds.
    """
    blocks: dict[ContentBlock, BlockText] = {
        ContentBlock.HEADER: a_block(ContentBlock.HEADER),
        ContentBlock.INTRO: a_block(ContentBlock.INTRO),
        ContentBlock.PAYMENT_TERMS: a_block(ContentBlock.PAYMENT_TERMS),
        ContentBlock.FOOTER: a_block(ContentBlock.FOOTER),
        ContentBlock.LEGAL_IDENTITY: a_block(
            ContentBlock.LEGAL_IDENTITY, text_nl=_LEGAL_IDENTITY_NL, text_en=_LEGAL_IDENTITY_EN
        ),
    }
    if overrides:
        blocks.update(overrides)
    return tuple(blocks.values())


def a_template(**overrides: object) -> InvoiceTemplate:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "administration_id": uuid.uuid4(),
        "name": "Classic",
        "is_default": True,
        "version": 1,
        "layout": Layout.CLASSIC,
        "logo": Logo(asset_id=None, position=LogoPosition.LEFT, size=LogoSize.MEDIUM),
        "typography": Typography(
            heading_font="ibm_plex_sans",
            body_font="ibm_plex_sans",
            figures_font="ibm_plex_mono",
            type_scale=TypeScale.MEDIUM,
            font_weight=FontWeight.REGULAR,
            line_height=LineHeight.NORMAL,
            letter_spacing=LetterSpacing.NORMAL,
        ),
        "colors": ColorScheme(accent="#0f172a", text="#111827", background="#ffffff"),
        "columns": a_column_layout(),
        "blocks": default_blocks(),
    }
    defaults.update(overrides)
    return InvoiceTemplate(**defaults)  # type: ignore[arg-type]


def fields(template: InvoiceTemplate) -> set[TemplateField]:
    return {violation.field for violation in check(template)}


def test_a_fully_compliant_template_may_be_saved() -> None:
    assert check(a_template()) == ()


# ---------------------------------------------------------------------------
# The six line-item columns - only three lock (FR-TPL-006, FR-TPL-009)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column_key", "expected"),
    [
        ("quantity", TemplateField.QUANTITY_COLUMN),
        ("unit_price", TemplateField.UNIT_PRICE_COLUMN),
        ("vat_rate", TemplateField.VAT_RATE_COLUMN),
    ],
)
def test_hiding_a_statutory_column_fails_with_the_right_field(
    column_key: str, expected: TemplateField
) -> None:
    template = a_template(columns=a_column_layout(**{column_key: False}))
    assert expected in fields(template)


@pytest.mark.parametrize("column_key", ["unit", "discount", "line_total"])
def test_hiding_a_non_statutory_column_is_allowed(column_key: str) -> None:
    template = a_template(columns=a_column_layout(**{column_key: False}))
    assert check(template) == ()


def test_there_is_no_vat_amount_column_to_hide_at_all() -> None:
    # ADR-037 decision #3, inherited: a per-line VAT amount does not exist in
    # the data model, so there is no LineColumn member for it to hide.
    assert not any(column.value == "vat_amount" for column in LineColumn)


# ---------------------------------------------------------------------------
# The legal_identity block's two locked merge tags, per language (FR-TPL-009
# applied to free text)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("supplier_vat_number", TemplateField.LEGAL_IDENTITY_VAT_TAG),
        ("supplier_kvk_number", TemplateField.LEGAL_IDENTITY_KVK_TAG),
    ],
)
def test_stripping_a_required_tag_from_dutch_fails_only_for_dutch(
    tag: str, expected: TemplateField
) -> None:
    stripped_nl = _LEGAL_IDENTITY_NL.replace("{{" + tag + "}}", "")
    template = a_template(
        blocks=default_blocks(
            {
                ContentBlock.LEGAL_IDENTITY: a_block(
                    ContentBlock.LEGAL_IDENTITY, text_nl=stripped_nl, text_en=_LEGAL_IDENTITY_EN
                )
            }
        )
    )
    violations = [violation for violation in check(template) if violation.field is expected]
    assert violations == [TemplateViolation(expected, language=Language.NL)]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("supplier_vat_number", TemplateField.LEGAL_IDENTITY_VAT_TAG),
        ("supplier_kvk_number", TemplateField.LEGAL_IDENTITY_KVK_TAG),
    ],
)
def test_stripping_a_required_tag_from_english_fails_only_for_english(
    tag: str, expected: TemplateField
) -> None:
    stripped_en = _LEGAL_IDENTITY_EN.replace("{{" + tag + "}}", "")
    template = a_template(
        blocks=default_blocks(
            {
                ContentBlock.LEGAL_IDENTITY: a_block(
                    ContentBlock.LEGAL_IDENTITY, text_nl=_LEGAL_IDENTITY_NL, text_en=stripped_en
                )
            }
        )
    )
    violations = [violation for violation in check(template) if violation.field is expected]
    assert violations == [TemplateViolation(expected, language=Language.EN)]


def test_stripping_a_tag_from_both_languages_fails_for_both() -> None:
    template = a_template(
        blocks=default_blocks(
            {
                ContentBlock.LEGAL_IDENTITY: a_block(
                    ContentBlock.LEGAL_IDENTITY,
                    text_nl="No BTW number here.",
                    text_en="No VAT number here.",
                )
            }
        )
    )
    violations = {
        (violation.field, violation.language)
        for violation in check(template)
        if violation.field is TemplateField.LEGAL_IDENTITY_VAT_TAG
    }
    assert violations == {
        (TemplateField.LEGAL_IDENTITY_VAT_TAG, Language.NL),
        (TemplateField.LEGAL_IDENTITY_VAT_TAG, Language.EN),
    }


def test_a_missing_legal_identity_block_fails_every_tag_in_every_language() -> None:
    # Defensive: the domain type does not force all five blocks to be
    # present (only ColumnLayout enforces completeness on construction), so
    # `check()` has to treat an absent legal_identity block as empty text
    # rather than crash.
    template = a_template(blocks=())
    violations = fields(template)
    assert violations == set(_LANGUAGE_SCOPED_FIELDS)


def test_a_template_missing_several_things_reports_all_at_once() -> None:
    template = a_template(
        columns=a_column_layout(quantity=False, vat_rate=False),
        blocks=default_blocks(
            {
                ContentBlock.LEGAL_IDENTITY: a_block(
                    ContentBlock.LEGAL_IDENTITY, text_nl="", text_en=""
                )
            }
        ),
    )
    violations = check(template)
    # 2 hidden columns + 2 tags missing from BOTH languages (2 x 2) = 6.
    assert len(violations) == 6
    found = fields(template)
    assert found == {
        TemplateField.QUANTITY_COLUMN,
        TemplateField.VAT_RATE_COLUMN,
        TemplateField.LEGAL_IDENTITY_VAT_TAG,
        TemplateField.LEGAL_IDENTITY_KVK_TAG,
    }


# ---------------------------------------------------------------------------
# D5: no dead ends
# ---------------------------------------------------------------------------


class TestD5NoDeadEnds:
    def test_every_field_has_a_message(self) -> None:
        assert set(MESSAGE_KEYS) == set(TemplateField)

    @pytest.mark.parametrize("field", list(TemplateField))
    @pytest.mark.parametrize("language", list(Language))
    def test_every_message_resolves_in_both_languages(
        self, field: TemplateField, language: Language
    ) -> None:
        violation_language = Language.NL if field in _LANGUAGE_SCOPED_FIELDS else None
        violation = TemplateViolation(field, language=violation_language)
        assert message_for(violation, language).strip()

    def test_a_language_scoped_violation_without_a_language_is_refused(self) -> None:
        with pytest.raises(ValueError, match="WHICH language"):
            message_for(
                TemplateViolation(TemplateField.LEGAL_IDENTITY_VAT_TAG, language=None), Language.EN
            )

    def test_describe_keeps_the_order_check_found_them_in(self) -> None:
        template = a_template(columns=a_column_layout(quantity=False))
        violations = check(template)

        described = describe(violations, Language.EN)

        assert [violation for violation, _ in described] == list(violations)
        assert all(message.strip() for _, message in described)

    def test_the_reader_gets_the_readers_language(self) -> None:
        violation = TemplateViolation(TemplateField.QUANTITY_COLUMN)
        assert message_for(violation, Language.NL) != message_for(violation, Language.EN)

    def test_the_legal_identity_message_names_the_exact_tag_to_retype(self) -> None:
        message = message_for(
            TemplateViolation(TemplateField.LEGAL_IDENTITY_VAT_TAG, language=Language.NL),
            Language.EN,
        )
        assert "{{supplier_vat_number}}" in message

    def test_the_legal_identity_message_names_which_language_lost_the_tag(self) -> None:
        dutch_missing = message_for(
            TemplateViolation(TemplateField.LEGAL_IDENTITY_KVK_TAG, language=Language.NL),
            Language.EN,
        )
        english_missing = message_for(
            TemplateViolation(TemplateField.LEGAL_IDENTITY_KVK_TAG, language=Language.EN),
            Language.EN,
        )
        assert dutch_missing != english_missing


def test_not_compliant_carries_every_violation() -> None:
    template = a_template(columns=a_column_layout(quantity=False, vat_rate=False))
    violations = check(template)

    exc = NotCompliant(violations)

    assert exc.violations == violations
    assert "FR-TPL-009" in str(exc)

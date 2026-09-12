"""FR-AR-003's gate: what blocks an invoice from being issued."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.i18n.language import Language
from api.invoicing.statutory import (
    MESSAGE_KEYS,
    InvoiceForIssue,
    LineForIssue,
    StatutoryFailure,
    StatutoryField,
    SupplierDetails,
    check,
    describe,
    message_for,
)
from api.vat.rules import TreatmentRole

COMPLETE_SUPPLIER = SupplierDetails(
    legal_name="Bakker Consultancy B.V.",
    address_line1="Keizersgracht 1",
    postal_code="1015 CJ",
    city="Amsterdam",
    vat_number="NL001234567B01",
    kvk_number="12345678",
)


def supplier_without(**overrides: object) -> SupplierDetails:
    return SupplierDetails(
        **{
            "legal_name": COMPLETE_SUPPLIER.legal_name,
            "address_line1": COMPLETE_SUPPLIER.address_line1,
            "postal_code": COMPLETE_SUPPLIER.postal_code,
            "city": COMPLETE_SUPPLIER.city,
            "vat_number": COMPLETE_SUPPLIER.vat_number,
            "kvk_number": COMPLETE_SUPPLIER.kvk_number,
            **overrides,
        }
    )


def a_line(**overrides: object) -> LineForIssue:
    defaults = {
        "position": 1,
        "description": "Advies",
        "quantity": Decimal("1"),
        "unit_price": Decimal("100.00"),
        "treatment": "btw_21",
        "role": TreatmentRole.STANDARD,
        "net": Decimal("100.00"),
    }
    return LineForIssue(**{**defaults, **overrides})  # type: ignore[arg-type]


def an_invoice(**overrides: object) -> InvoiceForIssue:
    defaults = {
        "supplier": COMPLETE_SUPPLIER,
        "customer_name": "De Vries Holding B.V.",
        "customer_address": "Damrak 70, 1012 LM Amsterdam",
        "customer_vat_number": None,
        "invoice_date": date(2026, 9, 9),
        "lines": [a_line()],
    }
    return InvoiceForIssue(**{**defaults, **overrides})  # type: ignore[arg-type]


def fields(invoice: InvoiceForIssue) -> set[StatutoryField]:
    return {failure.field for failure in check(invoice)}


def test_a_complete_domestic_invoice_may_be_issued() -> None:
    assert check(an_invoice()) == ()


# ---------------------------------------------------------------------------
# The supplier's own details - art. 35a(1)(c), (e)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("legal_name", StatutoryField.SUPPLIER_NAME),
        ("address_line1", StatutoryField.SUPPLIER_ADDRESS),
        ("postal_code", StatutoryField.SUPPLIER_ADDRESS),
        ("city", StatutoryField.SUPPLIER_ADDRESS),
        ("vat_number", StatutoryField.SUPPLIER_VAT_NUMBER),
        ("kvk_number", StatutoryField.SUPPLIER_KVK_NUMBER),
    ],
)
def test_the_supplier_must_identify_itself(missing: str, expected: StatutoryField) -> None:
    # Art. 35a(1)(c) and (e). Each part of the address separately, because "is
    # the address blank" is a question one space answers, and this is the one
    # the statute actually asks.
    assert expected in fields(an_invoice(supplier=supplier_without(**{missing: None})))


def test_a_second_address_line_is_optional() -> None:
    assert StatutoryField.SUPPLIER_ADDRESS not in fields(
        an_invoice(supplier=supplier_without(address_line2=None))
    )


def test_whitespace_is_not_a_value() -> None:
    supplier = SupplierDetails(
        legal_name="   ",
        address_line1="  ",
        postal_code=" ",
        city="\t",
        vat_number="\t",
        kvk_number=" ",
    )

    assert fields(an_invoice(supplier=supplier)) >= {
        StatutoryField.SUPPLIER_NAME,
        StatutoryField.SUPPLIER_ADDRESS,
        StatutoryField.SUPPLIER_VAT_NUMBER,
        StatutoryField.SUPPLIER_KVK_NUMBER,
    }


def test_the_address_is_formatted_once_so_a_template_cannot_invent_a_second() -> None:
    assert COMPLETE_SUPPLIER.formatted_address == "Keizersgracht 1\n1015 CJ Amsterdam"


def test_a_second_address_line_sits_between_the_street_and_the_city() -> None:
    assert (
        supplier_without(address_line2="Unit 4").formatted_address
        == "Keizersgracht 1\nUnit 4\n1015 CJ Amsterdam"
    )


def test_a_supplier_with_no_address_formats_to_nothing_rather_than_blank_lines() -> None:
    supplier = supplier_without(address_line1=None, postal_code=None, city=None)

    assert supplier.formatted_address == ""
    assert supplier.has_address is False


def test_a_complete_address_passes_the_gate() -> None:
    # The regression this whole migration exists to prevent: before 0038 there
    # was no column to read an address from, so this could never be true and
    # FR-AR-003 refused every invoice.
    assert COMPLETE_SUPPLIER.has_address is True
    assert StatutoryField.SUPPLIER_ADDRESS not in fields(an_invoice())


# ---------------------------------------------------------------------------
# The customer - art. 35a(1)(e), (f)
# ---------------------------------------------------------------------------


def test_the_customer_must_be_named_and_addressed() -> None:
    assert fields(an_invoice(customer_name=None, customer_address=None)) >= {
        StatutoryField.CUSTOMER_NAME,
        StatutoryField.CUSTOMER_ADDRESS,
    }


@pytest.mark.parametrize("role", [TreatmentRole.REVERSE_CHARGE, TreatmentRole.INTRA_COMMUNITY])
def test_the_customer_vat_number_is_mandatory_where_the_customer_owes_the_vat(
    role: TreatmentRole,
) -> None:
    # Art. 226(11a). Not a formality: without it the buyer cannot make the
    # reverse charge, and an intra-Community supply cannot be zero-rated at
    # all - so the treatment is not lawful without the number on the document.
    invoice = an_invoice(
        customer_vat_number=None,
        lines=[a_line(treatment="btw_verlegd", role=role)],
    )

    assert StatutoryField.CUSTOMER_VAT_NUMBER in fields(invoice)


@pytest.mark.parametrize(
    "role",
    [
        TreatmentRole.STANDARD,
        TreatmentRole.REDUCED,
        TreatmentRole.ZERO,
        TreatmentRole.EXEMPT,
        TreatmentRole.EXPORT,
        TreatmentRole.MARGIN,
    ],
)
def test_an_ordinary_invoice_needs_no_customer_vat_number(role: TreatmentRole) -> None:
    invoice = an_invoice(customer_vat_number=None, lines=[a_line(role=role)])

    assert StatutoryField.CUSTOMER_VAT_NUMBER not in fields(invoice)


def test_one_reverse_charged_line_is_enough_to_require_it() -> None:
    invoice = an_invoice(
        customer_vat_number=None,
        lines=[
            a_line(position=1),
            a_line(position=2, treatment="btw_verlegd", role=TreatmentRole.REVERSE_CHARGE),
        ],
    )

    assert StatutoryField.CUSTOMER_VAT_NUMBER in fields(invoice)


# ---------------------------------------------------------------------------
# The supply itself - art. 35a(1)(h), (i)
# ---------------------------------------------------------------------------


def test_an_invoice_with_no_lines_states_no_supply() -> None:
    # Refused rather than numbered: the number cannot be given back, and a
    # gapless series would then carry a document that says nothing.
    assert StatutoryField.LINES in fields(an_invoice(lines=[]))


def test_each_line_needs_a_description_a_quantity_and_a_price() -> None:
    invoice = an_invoice(lines=[a_line(description=None, quantity=None, unit_price=None)])

    assert fields(invoice) >= {
        StatutoryField.LINE_DESCRIPTION,
        StatutoryField.LINE_QUANTITY,
        StatutoryField.LINE_UNIT_PRICE,
    }


def test_a_quantity_of_zero_is_a_quantity() -> None:
    # Absent is missing; nought is a number somebody chose.
    invoice = an_invoice(lines=[a_line(quantity=Decimal(0))])

    assert StatutoryField.LINE_QUANTITY not in fields(invoice)


def test_a_failure_names_the_line_it_is_about() -> None:
    # "Quantity is missing" against a twelve-line invoice has not helped
    # anybody.
    invoice = an_invoice(lines=[a_line(position=1), a_line(position=2, description=None)])

    failures = [f for f in check(invoice) if f.field is StatutoryField.LINE_DESCRIPTION]
    assert [f.line_position for f in failures] == [2]


def test_a_treatment_with_no_rate_on_the_invoice_date_blocks_issuing() -> None:
    # CMP-014. Issuing would print a VAT amount computed from a rate the
    # ruleset does not have.
    invoice = an_invoice(unresolvable_treatments=frozenset({"btw_21"}))

    assert StatutoryField.VAT_RATE in fields(invoice)


class TestD5NoDeadEnds:
    """D5: "Every error message states what happened, why, and the specific
    next action. 'Validation failed' is not an acceptable string."

    A `StatutoryField` is an identifier, and an identifier on a screen is
    exactly the dead end D5 names. These assert that every one of them
    resolves to a sentence a person can act on, in both languages.
    """

    def test_every_field_has_a_message(self) -> None:
        # Exhaustive by assertion rather than by hope: a field added to the
        # enum without a message would reach a user as `supplier_vat_number`.
        assert set(MESSAGE_KEYS) == set(StatutoryField)

    @pytest.mark.parametrize("field", list(StatutoryField))
    @pytest.mark.parametrize("language", list(Language))
    def test_every_message_resolves_in_both_languages(
        self, field: StatutoryField, language: Language
    ) -> None:
        failure = StatutoryFailure(field, line_position=3)

        assert message_for(failure, language).strip()

    @pytest.mark.parametrize("field", list(StatutoryField))
    def test_every_message_names_a_next_action(self, field: StatutoryField) -> None:
        # The third of D5's three parts, and the one a validator usually
        # omits. Checked by looking for an imperative that tells somebody
        # where to go - crude, and it catches the failure that matters: a
        # message that only describes the problem.
        dutch = message_for(StatutoryFailure(field, line_position=3), Language.NL).lower()

        assert any(verb in dutch for verb in ("vul ", "kies ", "voeg ", "beschrijf ", "laat ")), (
            f"{field.value} says what is wrong but not what to do about it (D5)"
        )

    @pytest.mark.parametrize("field", list(StatutoryField))
    def test_every_message_says_more_than_what_is_missing(self, field: StatutoryField) -> None:
        # "what happened, why, AND the next action" is three things. One
        # clause is a label; this is the cheapest structural proxy for the
        # message having done the other two.
        english = message_for(StatutoryFailure(field, line_position=3), Language.EN)

        assert english.count(".") >= 2, f"{field.value} reads as a label, not a message"

    def test_a_line_level_message_names_the_line(self) -> None:
        # "A description is missing" against a twelve-line invoice is the dead
        # end D5 is about.
        message = message_for(
            StatutoryFailure(StatutoryField.LINE_DESCRIPTION, line_position=7), Language.EN
        )

        assert "7" in message

    def test_a_line_level_failure_without_a_line_is_refused(self) -> None:
        # Louder than the catalogue's own missing-placeholder error, and it
        # says which of the two mistakes was made.
        with pytest.raises(ValueError, match="WHICH line"):
            message_for(
                StatutoryFailure(StatutoryField.LINE_QUANTITY, line_position=None),
                Language.EN,
            )

    def test_the_customer_vat_number_offers_both_ways_out(self) -> None:
        # There genuinely are two: supply the number, or choose a treatment
        # that does not need one. Offering only the first dead-ends somebody
        # invoicing a private individual who has no VAT number at all.
        message = message_for(
            StatutoryFailure(StatutoryField.CUSTOMER_VAT_NUMBER), Language.EN
        ).lower()

        assert "add the number" in message
        assert "different" in message and "treatment" in message

    def test_the_missing_rate_hands_off_to_somebody_who_can_fix_it(self) -> None:
        # The one failure that may not be the user's to fix (CMP-014): the
        # ruleset has no rate covering the invoice date.
        message = message_for(
            StatutoryFailure(StatutoryField.VAT_RATE, line_position=2), Language.EN
        ).lower()

        assert "administrator" in message

    def test_describe_keeps_the_order_check_found_them_in(self) -> None:
        # Invoice-wide problems before line-level ones: there is no point
        # fixing line 7's quantity while the invoice has no customer.
        invoice = an_invoice(customer_name=None, lines=[a_line(position=1, description=None)])
        failures = check(invoice)

        described = describe(failures, Language.EN)

        assert [failure for failure, _ in described] == list(failures)
        assert all(message.strip() for _, message in described)

    def test_the_reader_gets_the_reader_s_language(self) -> None:
        # These are interface text for whoever is completing the invoice, not
        # the legal wording that follows the customer (FR-TPL-013). Swapping
        # them would tell a Dutch bookkeeper in German what to fix.
        failure = StatutoryFailure(StatutoryField.SUPPLIER_NAME)

        assert message_for(failure, Language.NL) != message_for(failure, Language.EN)


def test_every_failure_is_reported_at_once() -> None:
    # Somebody completing an invoice sees all of it on one screen rather than
    # discovering them one save at a time.
    invoice = InvoiceForIssue(
        supplier=SupplierDetails(
            legal_name=None,
            address_line1=None,
            postal_code=None,
            city=None,
            vat_number=None,
            kvk_number=None,
        ),
        customer_name=None,
        customer_address=None,
        customer_vat_number=None,
        invoice_date=None,
        lines=[],
    )

    assert len(check(invoice)) >= 8

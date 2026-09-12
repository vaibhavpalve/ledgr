"""FR-AR-002's legal wording: every treatment states its reason."""

from __future__ import annotations

import pytest

from api.i18n.language import Language
from api.invoicing.wording import (
    WORDING_KEYS,
    WordingReview,
    requires_customer_vat_number,
    reviews,
    unreviewed_wording,
    wording_for,
)
from api.vat.rules import TreatmentRole


@pytest.mark.parametrize("role", list(TreatmentRole))
@pytest.mark.parametrize("language", list(Language))
def test_every_treatment_has_wording_in_both_languages(
    role: TreatmentRole, language: Language
) -> None:
    # An invoice showing 0.00 with no explanation is not a compliant invoice
    # (art. 226(11)), so there is no treatment for which "no wording" is an
    # acceptable answer - and FR-LOC-001 forbids falling back to English.
    text = wording_for(role, language).text

    assert text.strip()


def test_all_eight_of_fr_ar_002_are_covered() -> None:
    assert set(WORDING_KEYS) == set(TreatmentRole)
    assert len(TreatmentRole) == 8


def test_the_reverse_charge_carries_the_statutory_dutch_phrase_in_both_languages() -> None:
    # "BTW verlegd" is what obliges the BUYER to account for the tax. It is
    # carried verbatim into the English version rather than translated away,
    # because a foreign customer's own tax authority reads the Dutch phrase -
    # the argument FR-LOC-001c makes about BTW generally.
    dutch = wording_for(TreatmentRole.REVERSE_CHARGE, Language.NL).text
    english = wording_for(TreatmentRole.REVERSE_CHARGE, Language.EN).text

    assert "verlegd" in dutch
    assert "verlegd" in english


def test_the_intra_community_statement_references_the_directive() -> None:
    # Art. 226(11) accepts a reference to the provision as the required
    # statement, and a buyer's own authority looks for it.
    for language in Language:
        assert "138" in wording_for(TreatmentRole.INTRA_COMMUNITY, language).text


def test_the_margin_scheme_says_the_vat_is_not_deductible() -> None:
    # The buyer cannot deduct it, and the invoice has to say so.
    for language in Language:
        text = wording_for(TreatmentRole.MARGIN, language).text.lower()
        assert "marge" in text or "margin" in text


def test_exempt_and_zero_do_not_say_the_same_thing() -> None:
    # Same figure, different legal meaning, different rubriek on the return.
    # Collapsing them is what makes a return wrong in a way the ledger cannot
    # see.
    for language in Language:
        assert (
            wording_for(TreatmentRole.EXEMPT, language).text
            != wording_for(TreatmentRole.ZERO, language).text
        )


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (TreatmentRole.REVERSE_CHARGE, True),
        (TreatmentRole.INTRA_COMMUNITY, True),
        (TreatmentRole.STANDARD, False),
        (TreatmentRole.EXPORT, False),
        (TreatmentRole.MARGIN, False),
    ],
)
def test_which_treatments_make_the_customer_vat_number_statutory(
    role: TreatmentRole, expected: bool
) -> None:
    assert requires_customer_vat_number(role) is expected


def test_the_wording_is_flagged_as_unreviewed_until_a_tax_adviser_signs_it_off() -> None:
    # The MECHANISM is complete; the TEXT has not been through a Dutch tax
    # adviser. Getting it wrong produces an invoice that looks entirely normal
    # and is not compliant, so the fact is made checkable rather than left in a
    # comment - the posture VatRulesService.provisional_rulesets takes.
    assert set(unreviewed_wording()) == set(TreatmentRole)
    assert wording_for(TreatmentRole.STANDARD, Language.NL).reviewed is False


class TestTheReviewRecord:
    """FR-AR-002's review state, which is data rather than a constant.

    Signing a treatment off is an edit to
    apps/api/data/invoicing/wording-review.json - the same shape the glossary
    uses for FR-LOC-001c's terminology review, and for the same reason: only a
    qualified person can make the judgement, and the record of whether they
    have belongs somewhere they can write.
    """

    def test_every_treatment_has_a_record(self) -> None:
        # An omission is the uncertainty going unrecorded, which is the one
        # state this file exists to prevent.
        assert set(reviews()) == set(TreatmentRole)

    def test_every_record_cites_the_provision_its_words_answer_to(self) -> None:
        # The basis is what an adviser checks the wording against. Without it
        # the sheet is eight sentences and no way to judge them.
        for review in reviews().values():
            assert "art." in review.basis.lower()

    def test_status_and_confidence_use_the_glossary_vocabulary(self) -> None:
        # Deliberately the same words as check_translations.py's
        # REVIEW_STATUSES / REVIEW_CONFIDENCES, so somebody who has reviewed
        # the glossary already knows what these mean.
        for review in reviews().values():
            assert review.status in {"proposed", "flagged", "reviewed"}
            assert review.confidence in {"high", "medium", "low"}

    def test_a_flagged_treatment_says_what_is_still_open(self) -> None:
        # Flagged with no question is a worry recorded nowhere.
        for review in reviews().values():
            if review.status == "flagged":
                assert review.questions, f"{review.role.value} is flagged with no question"

    def test_reviewed_needs_a_named_person_and_a_date(self) -> None:
        # A status alone would let somebody mark eight treatments compliant
        # with nobody's name against it, which is worse than leaving them
        # unreviewed - it looks like an assurance.
        assert (
            WordingReview(
                role=TreatmentRole.STANDARD,
                basis="art. 226(9)",
                status="reviewed",
                confidence="high",
                reviewed_by=None,
                reviewed_on=None,
                questions=(),
            ).is_reviewed
            is False
        )

        assert (
            WordingReview(
                role=TreatmentRole.STANDARD,
                basis="art. 226(9)",
                status="reviewed",
                confidence="high",
                reviewed_by="A. Jansen RB",
                reviewed_on="2026-09-10",
                questions=(),
            ).is_reviewed
            is True
        )

    def test_the_margin_scheme_records_that_one_treatment_may_not_be_enough(self) -> None:
        # Art. 226(14) names three margin schemes and requires the invoice to
        # say which. The system has one `btw_marge`, so it cannot - and that is
        # a ruleset question, not a wording one. Recorded because it is the
        # most likely reason this design changes.
        questions = " ".join(reviews()[TreatmentRole.MARGIN].questions)
        assert "226(14)" in questions
        assert reviews()[TreatmentRole.MARGIN].confidence == "low"

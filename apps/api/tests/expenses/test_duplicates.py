"""FR-EXP-001g - warn when a receipt matches an existing expense.

The single most important assertion in this file is that a warning does not
stop anything. "Warn, don't block" is the kind of property that quietly becomes
"block" the first time somebody treats a warning as an error, so it is asserted
at every layer that could take it away: the view, submission, and posting.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.expenses.duplicates import (
    SIMILARITY_THRESHOLD,
    DuplicateStrength,
    DuplicateWarning,
    ExpenseTriple,
    describe,
    find_exact_matches,
    is_exact_match,
    normalise_supplier,
)
from api.expenses.model import ExpenseStatus, VatTreatment
from tests.expenses.duplicate_cases import MATCH_CASES, NORMALISATION_CASES, MatchCase
from tests.expenses.test_form import complete, harness, update

DAY = date(2025, 6, 1)
OTHER_DAY = date(2025, 6, 2)
AMOUNT = Decimal("12.50")
OTHER_AMOUNT = Decimal("12.51")


def triple(
    supplier: str | None = "Albert Heijn",
    on: date | None = DAY,
    gross: Decimal | None = AMOUNT,
) -> ExpenseTriple:
    return ExpenseTriple(supplier=supplier, on=on, gross_amount=gross)


# ===========================================================================
# The normalisation, against the shared table
# ===========================================================================


def test_the_case_tables_are_not_empty() -> None:
    """A table-driven suite that finds no rows passes completely."""
    assert len(NORMALISATION_CASES) >= 10
    assert len(MATCH_CASES) >= 5


@pytest.mark.parametrize(("raw", "expected", "why"), NORMALISATION_CASES, ids=lambda v: str(v)[:40])
def test_normalisation_matches_the_shared_table(raw: str, expected: str, why: str) -> None:
    assert normalise_supplier(raw) == expected, why


def test_normalisation_is_idempotent() -> None:
    """Normalising an already-normalised name changes nothing.

    The index in migration 0036 is built on the normalised form and the query
    normalises the input, so a rule that shifted on a second application would
    make the index disagree with the lookup.
    """
    for raw, _, _ in NORMALISATION_CASES:
        once = normalise_supplier(raw)
        assert normalise_supplier(once) == once


# ===========================================================================
# The match rule
# ===========================================================================


@pytest.mark.parametrize("case", MATCH_CASES, ids=lambda c: f"{c.left_supplier}~{c.right_supplier}")
def test_the_match_rule_matches_the_shared_table(case: MatchCase) -> None:
    left = triple(case.left_supplier)
    right = triple(
        case.right_supplier,
        on=DAY if case.same_date else OTHER_DAY,
        gross=AMOUNT if case.same_amount else OTHER_AMOUNT,
    )

    assert is_exact_match(left, right) is case.matches, case.why


def test_an_incomplete_triple_matches_nothing() -> None:
    """A form somebody is halfway through is the normal state (FR-EXP-001c),
    not an error. Two half-filled forms sharing a supplier are two forms.
    """
    assert not triple(supplier=None).is_complete
    assert not is_exact_match(triple(supplier=None), triple())
    assert not is_exact_match(triple(), triple(on=None))
    assert not is_exact_match(triple(gross=None), triple(gross=None))


def test_matching_is_symmetric() -> None:
    """Which of the two claims is "the new one" must not change the answer."""
    left, right = triple("Albert Heijn"), triple("  ALBERT HEIJN  ")
    assert is_exact_match(left, right) == is_exact_match(right, left)


def test_finding_matches_in_memory() -> None:
    """The path a capture-time check will take once extraction fills these
    fields, and the one a batch review takes before anything is stored.
    """
    subject = triple()
    others = {
        (same := uuid.uuid4()): triple("ALBERT HEIJN"),
        uuid.uuid4(): triple("Jumbo"),
        uuid.uuid4(): triple(on=OTHER_DAY),
    }

    assert find_exact_matches(subject, others) == [same]


@given(
    supplier=st.text(min_size=1, max_size=40),
    spaces=st.integers(min_value=1, max_value=4),
)
@settings(max_examples=200, deadline=None, derandomize=True)
def test_whitespace_and_case_never_change_the_answer(supplier: str, spaces: int) -> None:
    """The property behind every normalisation case: how somebody typed the
    name does not decide whether it is the same shop.
    """
    noisy = (" " * spaces) + supplier.upper() + (" " * spaces)

    # Casefolding is not always an involution - 'ß'.upper() is 'SS' - so the
    # comparison is against the normalised forms rather than against the raw
    # ones, which is what the rule itself does.
    expected = bool(normalise_supplier(supplier)) and normalise_supplier(
        supplier
    ) == normalise_supplier(noisy)

    assert is_exact_match(triple(supplier), triple(noisy)) is expected


@pytest.mark.parametrize("junk", [":", "...", "---", "()", "   .   "])
def test_a_supplier_that_normalises_to_nothing_matches_nothing(junk: str) -> None:
    """Found by the property test above, and worth its own case.

    Without this rule every claim with a punctuation-only supplier would warn
    about every other one on the same date and amount. 0033's CHECK does not
    stop one being entered - `length(btrim(':')) > 0` is true - so the rule
    lives here and in migration 0036's search.
    """
    assert normalise_supplier(junk) == ""
    assert not is_exact_match(triple(junk), triple(junk))
    assert find_exact_matches(triple(junk), {uuid.uuid4(): triple(junk)}) == []


# ===========================================================================
# Warn, never block - at every layer that could take it away
# ===========================================================================


def _warning(same_submitter: bool = False) -> DuplicateWarning:
    return DuplicateWarning(
        expense_id=uuid.uuid4(),
        strength=DuplicateStrength.EXACT,
        supplier="Albert Heijn",
        on=DAY,
        gross_amount=AMOUNT,
        status="posted",
        same_submitter=same_submitter,
    )


async def test_a_claim_with_warnings_can_still_be_submitted() -> None:
    """The requirement in one assertion. Legitimate duplicates exist - two
    identical coffees on one morning - so this must succeed.
    """
    h = harness()
    h.repository.duplicates = [_warning()]
    await complete(h)

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )
    assert view.has_duplicate_warning
    assert view.can_be_marked_ready, "a warning does not make a claim unsubmittable"

    ready = await h.service.mark_ready(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )
    assert ready.expense.status is ExpenseStatus.READY


async def test_can_be_marked_ready_does_not_consult_the_warnings() -> None:
    """The specific line that would turn warn into block.

    Asserted against a claim that is complete AND has warnings, because that is
    the only state where the two could disagree.
    """
    h = harness()
    h.repository.duplicates = [_warning(), _warning()]
    await complete(h)

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert len(view.duplicate_warnings) == 2
    assert view.missing_fields == ()
    assert view.can_be_marked_ready is True


async def test_the_submission_records_that_warnings_were_outstanding() -> None:
    """Not a gate - a record. An approver reading this entry later should be
    able to see what the submitter saw.
    """
    h = harness()
    h.repository.duplicates = [_warning()]
    await complete(h)

    await h.service.mark_ready(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    submitted = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "submit_expense"
    ]
    assert (
        submitted[0].detail["duplicate_warnings"] == "1 exact and 0 probable duplicate warning(s)"
    )


async def test_a_clean_submission_says_so() -> None:
    h = harness()
    await complete(h)

    await h.service.mark_ready(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    submitted = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "submit_expense"
    ]
    assert submitted[0].detail["duplicate_warnings"] == "no duplicate warnings"


async def test_the_audit_entry_does_not_name_the_matching_claims() -> None:
    """The audit log has its own retention (IAM-093) and is not the place to
    accumulate a second index of who spent what.
    """
    h = harness()
    warning = _warning()
    h.repository.duplicates = [warning]
    await complete(h)

    await h.service.mark_ready(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    submitted = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "submit_expense"
    ]
    assert str(warning.expense_id) not in str(submitted[0].detail)
    assert "Albert Heijn" not in str(submitted[0].detail)


# ===========================================================================
# When the check runs
# ===========================================================================


async def test_no_lookup_happens_until_the_triple_is_complete() -> None:
    """A form halfway through has nothing to match on, and asking the database
    on every keystroke would be a query per character.
    """
    h = harness()
    h.repository.duplicates = [_warning()]

    await update(h, supplier="Albert Heijn")
    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.duplicate_warnings == ()
    assert h.repository.duplicate_lookups == 0


async def test_the_lookup_happens_as_soon_as_it_is_complete() -> None:
    h = harness()
    h.repository.duplicates = [_warning()]

    await update(
        h,
        supplier="Albert Heijn",
        expense_date=DAY,
        gross_amount=AMOUNT,
        vat_treatment=VatTreatment.BTW_21,
    )
    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert len(view.duplicate_warnings) == 1
    assert h.repository.duplicate_lookups > 0


async def test_the_warning_distinguishes_your_own_double_entry() -> None:
    """Their own is a mistake to fix; somebody else's is a conversation to
    have. The flag is the difference, and the other person's identity is
    deliberately absent.
    """
    h = harness()
    h.repository.duplicates = [_warning(same_submitter=True)]
    await complete(h)

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.duplicate_warnings[0].same_submitter is True
    assert not hasattr(view.duplicate_warnings[0], "submitted_by_user_id")


def test_the_similarity_threshold_is_generous_on_purpose() -> None:
    """Asymmetric costs: a false positive is a line somebody dismisses, a false
    negative is a claim paid twice. Pinned so that raising it is deliberate.
    """
    assert 0.3 <= SIMILARITY_THRESHOLD <= 0.6


def test_describe_summarises_without_naming_anything() -> None:
    assert describe([]) == "no duplicate warnings"
    assert describe([_warning()]) == "1 exact and 0 probable duplicate warning(s)"
    assert (
        describe([_warning(), replace(_warning(), strength=DuplicateStrength.PROBABLE)])
        == "1 exact and 1 probable duplicate warning(s)"
    )

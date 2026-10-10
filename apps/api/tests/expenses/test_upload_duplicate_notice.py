"""ADR-116: telling the uploader, at the upload, that an invoice looks like one
already on the list.

Three properties carry it and each is asserted on its own:

  * an identical FILE is caught with no reading at all
  * the invoice number decides how sure the notice is (ADR-101's grading)
  * nothing found is None, and nothing here ever refuses anything
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

from api.expenses.duplicates import DuplicateStrength, DuplicateWarning
from api.expenses.model import Expense, ExpenseStatus
from tests.expenses.test_form import Harness, complete, harness

DAY = date(2026, 9, 28)
AMOUNT = Decimal("10.00")


def _warning(
    *,
    invoice_number_match: str = "different",
    strength: DuplicateStrength = DuplicateStrength.EXACT,
    similarity: float | None = None,
    same_submitter: bool = True,
) -> DuplicateWarning:
    return DuplicateWarning(
        expense_id=uuid.uuid4(),
        strength=strength,
        supplier="Mistral AI SAS",
        on=DAY,
        gross_amount=AMOUNT,
        status="ready",
        same_submitter=same_submitter,
        similarity=similarity,
        invoice_number_match=invoice_number_match,  # type: ignore[arg-type]
    )


async def _notice(h: Harness):
    expense = h.repository.expenses[h.expense_id]
    return await h.service.duplicate_notice(expense=expense)


def _earlier(h: Harness, **fields: object) -> Expense:
    return Expense(
        id=uuid.uuid4(),
        administration_id=h.administration,
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.READY,
        submitted_by_user_id=h.user,
        **fields,  # type: ignore[arg-type]
    )


async def test_nothing_on_the_list_means_no_notice() -> None:
    h = harness()
    await complete(h)

    assert await _notice(h) is None


async def test_an_identical_file_is_caught_before_anything_was_read() -> None:
    """The expense is still an empty draft - the reading failed or never ran -
    and the notice still names the claim that holds the same file."""
    h = harness()
    item = h.repository.expenses[h.expense_id].capture_item_id
    existing = _earlier(h, supplier="Mistral AI SAS", expense_date=DAY, gross_amount=AMOUNT)
    h.repository.same_file[item] = (existing, 3)

    notice = await _notice(h)

    assert notice is not None
    assert notice.match == "same_file"
    assert notice.expense_id == existing.id
    assert notice.count == 3
    assert notice.supplier == "Mistral AI SAS"
    assert notice.invoice_number_match is None
    assert h.repository.duplicate_lookups == 0, "a file match needs no look-alike search"


async def test_the_same_invoice_number_is_graded_as_the_same_invoice() -> None:
    h = harness()
    await complete(h)
    await h.service.update(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
        invoice_number="MSTRL-001",
    )
    other = _warning(invoice_number_match="same")
    h.repository.duplicates = [_warning(invoice_number_match="different"), other]

    notice = await _notice(h)

    assert notice is not None
    assert notice.match == "same_invoice"
    assert notice.expense_id == other.expense_id
    assert notice.invoice_number == "MSTRL-001"
    # Only the warnings that share the number are counted: the look-alike with
    # a different number is a different document.
    assert notice.count == 1


async def test_matching_details_with_a_different_number_is_only_a_soft_notice() -> None:
    h = harness()
    await complete(h)
    h.repository.duplicates = [_warning(invoice_number_match="different")]

    notice = await _notice(h)

    assert notice is not None
    assert notice.match == "same_details"
    assert notice.invoice_number_match == "different"


async def test_a_missing_number_cannot_rule_a_duplicate_out() -> None:
    h = harness()
    await complete(h)
    h.repository.duplicates = [_warning(invoice_number_match="missing")]

    notice = await _notice(h)

    assert notice is not None
    assert notice.match == "same_details"
    assert notice.invoice_number_match == "missing"


async def test_the_most_alike_claim_is_the_one_named() -> None:
    h = harness()
    await complete(h)
    similar = _warning(strength=DuplicateStrength.PROBABLE, similarity=0.7)
    exact = _warning(strength=DuplicateStrength.EXACT)
    h.repository.duplicates = [similar, exact]

    notice = await _notice(h)

    assert notice is not None
    assert notice.expense_id == exact.expense_id
    assert notice.count == 2


async def test_an_unfinished_draft_is_not_searched_for_look_alikes() -> None:
    """Supplier, date and amount are all needed to match on. A reading that got
    two of three has nothing to compare, and is not a duplicate of anything."""
    h = harness()
    await h.service.update(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
        supplier="Mistral AI SAS",
    )
    h.repository.duplicates = [_warning(invoice_number_match="same")]

    assert await _notice(h) is None
    assert h.repository.duplicate_lookups == 0


async def test_who_filed_the_other_claim_is_a_flag_not_a_name() -> None:
    h = harness()
    await complete(h)
    h.repository.duplicates = [_warning(same_submitter=False)]

    notice = await _notice(h)

    assert notice is not None
    assert notice.same_submitter is False
    assert not hasattr(notice, "submitted_by_user_id")


async def test_a_notice_does_not_change_what_can_be_submitted() -> None:
    """ADR-116 moves WHERE a duplicate is announced. What it blocks is still
    ADR-101's confirmed duplicate and nothing else: a soft notice leaves the
    claim exactly as submittable as before."""
    h = harness()
    await complete(h)
    h.repository.duplicates = [_warning(invoice_number_match="different")]

    notice = await _notice(h)
    view = await h.service.view(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    assert notice is not None
    assert view.can_be_marked_ready
    assert replace(view.expense).status is ExpenseStatus.DRAFT

"""ADR-117: throwing a draft invoice away.

What is asserted is what discarding is FOR - the way out of a confirmed
duplicate - and what it must never be: a way to make a claim that is in front of
an approver, or already in the books, disappear.
"""

from __future__ import annotations

import pytest

from api.expenses.model import (
    ExpenseNotDiscardable,
    ExpenseNotFound,
    ExpenseStatus,
    NotAuthorizedToCapture,
)
from tests.expenses.test_form import Harness, complete, harness


async def _discard(h: Harness, reason: str = "duplicate") -> None:
    await h.service.discard(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
        reason=reason,
    )


async def test_a_draft_can_be_thrown_away() -> None:
    h = harness()
    item = h.repository.expenses[h.expense_id].capture_item_id

    await _discard(h, "duplicate")

    assert h.repository.discarded_items == {item: "duplicate"}


async def test_a_discarded_invoice_is_not_there_to_be_found() -> None:
    """Opened, edited, submitted or read again afterwards: not found, because
    every one of those goes through the same lookup."""
    h = harness()
    await _discard(h)

    with pytest.raises(ExpenseNotFound):
        await h.service.view(
            administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
        )
    with pytest.raises(ExpenseNotFound):
        await h.service.mark_ready(
            administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
        )


async def test_discarding_twice_is_not_found_rather_than_a_second_decision() -> None:
    h = harness()
    await _discard(h)

    with pytest.raises(ExpenseNotFound):
        await _discard(h)


async def test_a_claim_in_front_of_an_approver_cannot_be_thrown_away() -> None:
    h = harness()
    await complete(h)
    await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    with pytest.raises(ExpenseNotDiscardable):
        await _discard(h)
    assert h.repository.discarded_items == {}


async def test_a_posted_claim_is_corrected_by_reversal_not_by_hiding_it() -> None:
    """FR-GL-003: the books are append-only, and a booked purchase that could be
    discarded off its own list would be a way to hide a posting."""
    h = harness()
    from dataclasses import replace

    h.repository.expenses[h.expense_id] = replace(
        h.repository.expenses[h.expense_id], status=ExpenseStatus.POSTED
    )

    with pytest.raises(ExpenseNotDiscardable):
        await _discard(h)
    assert h.repository.discarded_items == {}


async def test_it_is_recorded_with_the_reason_and_nothing_of_the_claim() -> None:
    h = harness()
    await complete(h)

    await _discard(h, "not_an_invoice")

    entries = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "discard_expense"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.resource_id == h.expense_id
    assert entry.actor_user_id == h.user
    assert entry.detail["reason"] == "not_an_invoice"
    # The audit log has its own retention (IAM-093); the claim is not copied in.
    assert "supplier" not in entry.detail
    assert "gross_amount" not in entry.detail


async def test_someone_who_may_not_submit_expenses_cannot_discard() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await _discard(h)
    assert h.repository.discarded_items == {}

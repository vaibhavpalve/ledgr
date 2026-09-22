"""The expense entry form - FR-EXP-001b, FR-EXP-001e.

Three things carry the requirement and each is asserted on its own:

  * the five typed fields plus payment method, and nothing else required
  * VAT and net derived rather than entered, from a rate looked up by DATE
  * the category defaulted from this user's history, and never overriding a
    choice they already made
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pytest

from api.audit.log import AuditLog, AuditOutcome
from api.authz.service import AuthorizationService
from api.expenses.duplicates import DuplicateWarning, ExpenseTriple
from api.expenses.form import UNSET, ExpenseFormService
from api.expenses.model import (
    Expense,
    ExpenseAlreadyReady,
    ExpenseNotFound,
    ExpenseStatus,
    IncompleteExpense,
    NotAuthorizedToCapture,
    PaymentMethod,
    VatRateUnavailable,
    VatTreatment,
)
from api.expenses.vat import VatError
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

#: The shipped Dutch ruleset (migration 0028's data file), as the repository
#: would answer it. The 2012 boundary is the whole reason the rate is looked
#: up by date rather than typed.
RULESET: dict[str, tuple[tuple[date, Decimal], ...]] = {
    "btw_21": ((date(2001, 1, 1), Decimal("19.00")), (date(2012, 10, 1), Decimal("21.00"))),
    "btw_9": ((date(2001, 1, 1), Decimal("6.00")), (date(2019, 1, 1), Decimal("9.00"))),
    "btw_0": ((date(2001, 1, 1), Decimal("0.00")),),
    "btw_vrijgesteld": ((date(2001, 1, 1), Decimal("0.00")),),
    "btw_verlegd": ((date(2001, 1, 1), Decimal("0.00")),),
    "btw_icp": ((date(2001, 1, 1), Decimal("0.00")),),
    "btw_export": ((date(2001, 1, 1), Decimal("0.00")),),
}


@dataclass
class FakeFormRepository:
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    expenses: dict[uuid.UUID, Expense] = field(default_factory=dict)
    #: (user_id, supplier_lower) -> category, most recent last
    history: list[tuple[uuid.UUID, str | None, str]] = field(default_factory=list)
    #: FR-EXP-001g. What the archive-wide search would return.
    duplicates: list[DuplicateWarning] = field(default_factory=list)
    #: How many times it was asked. The form must not query on every keystroke
    #: - only once the triple is complete.
    duplicate_lookups: int = 0
    #: item id -> its first stored original, for the review screen's preview.
    documents: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    async def get(self, *, administration_id: uuid.UUID, expense_id: uuid.UUID) -> Expense | None:
        expense = self.expenses.get(expense_id)
        if expense is None or expense.administration_id != administration_id:
            return None
        return expense

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        changes: dict[str, object],
    ) -> Expense:
        expense = self.expenses[expense_id]
        mapped: dict[str, object] = {}
        for name, value in changes.items():
            if name == "vat_treatment":
                mapped[name] = VatTreatment(value) if value else None
            elif name == "payment_method":
                mapped[name] = PaymentMethod(value) if value else None
            else:
                mapped[name] = value
        updated = replace(expense, **mapped)  # type: ignore[arg-type]
        # The database generates net; the fake has to, or these tests would
        # pass against a shape production never produces.
        if updated.gross_amount is not None and updated.vat_amount is not None:
            updated = replace(updated, net_amount=updated.gross_amount - updated.vat_amount)
        else:
            updated = replace(updated, net_amount=None)
        self.expenses[expense_id] = updated
        return updated

    async def set_status(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, status: str
    ) -> Expense:
        updated = replace(self.expenses[expense_id], status=ExpenseStatus(status))
        self.expenses[expense_id] = updated
        return updated

    async def rate_on(self, *, treatment: str, on: date) -> Decimal | None:
        rows = RULESET.get(treatment)
        if rows is None:
            return None
        applicable = [rate for valid_from, rate in rows if valid_from <= on]
        return applicable[-1] if applicable else None

    async def suggest_category(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, supplier: str | None
    ) -> str | None:
        mine = [(s, c) for u, s, c in self.history if u == user_id]
        if supplier:
            for stored_supplier, category in reversed(mine):
                if stored_supplier and stored_supplier.strip().lower() == supplier.strip().lower():
                    return category
        if not mine:
            return None
        counts: dict[str, int] = {}
        for _, category in mine:
            counts[category] = counts.get(category, 0) + 1
        return max(counts, key=lambda c: counts[c])

    async def duplicate_candidates(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        triple: ExpenseTriple,
    ) -> list[DuplicateWarning]:
        assert triple.is_complete, "the form must not look up an incomplete triple"
        self.duplicate_lookups += 1
        return list(self.duplicates)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id if administration_id == self.administration_id else None

    async def first_document_id(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID
    ) -> uuid.UUID | None:
        return self.documents.get(item_id)

    async def list_by_status(
        self, *, administration_id: uuid.UUID, status: str | None, limit: int
    ) -> list[Expense]:
        items = [e for e in self.expenses.values() if e.administration_id == administration_id]
        if status is not None:
            items = [e for e in items if e.status.value == status]
        return items[:limit]


@dataclass
class Harness:
    service: ExpenseFormService
    repository: FakeFormRepository
    audit: InMemoryAuditRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID
    expense_id: uuid.UUID


def harness(*, role: str = "Bookkeeper") -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    repository = FakeFormRepository(organization_id=world.acme, administration_id=world.acme_books)
    expense = Expense(
        id=uuid.uuid4(),
        administration_id=world.acme_books,
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.DRAFT,
        submitted_by_user_id=world.user,
    )
    repository.expenses[expense.id] = expense
    audit = InMemoryAuditRepository()
    return Harness(
        service=ExpenseFormService(
            repository=repository,
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(audit),
        ),
        repository=repository,
        audit=audit,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
        expense_id=expense.id,
    )


async def update(h: Harness, **fields: object):
    return await h.service.update(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
        **fields,  # type: ignore[arg-type]
    )


async def complete(h: Harness) -> None:
    """Every field FR-EXP-001b and FR-EXP-001e require."""
    await update(
        h,
        expense_date=date(2025, 6, 1),
        supplier="Albert Heijn",
        gross_amount=Decimal("121.00"),
        vat_treatment=VatTreatment.BTW_21,
        category="Kantoorbenodigdheden",
        payment_method=PaymentMethod.BUSINESS_CARD,
    )


# ===========================================================================
# FR-EXP-001b: VAT and net calculated automatically
# ===========================================================================


async def test_vat_and_net_are_derived_from_gross_and_the_treatment() -> None:
    view = await update(
        harness(),
        expense_date=date(2025, 6, 1),
        gross_amount=Decimal("121.00"),
        vat_treatment=VatTreatment.BTW_21,
    )

    assert view.expense.vat_rate == Decimal("21.00")
    assert view.expense.vat_amount == Decimal("21.00")
    assert view.expense.net_amount == Decimal("100.00")


async def test_the_rate_comes_from_the_expense_date_not_today() -> None:
    """CMP-014, and the reason "VAT rate" is stored as a treatment.

    `btw_21` was 19% until 2012-10-01 and 21% after. A receipt is filed under
    the rate that applied when it was issued, and no care at the keyboard makes
    a typed rate track that.
    """
    h = harness()

    before = await update(
        h,
        expense_date=date(2012, 9, 30),
        gross_amount=Decimal("119.00"),
        vat_treatment=VatTreatment.BTW_21,
    )
    assert before.expense.vat_rate == Decimal("19.00")
    assert before.expense.vat_amount == Decimal("19.00")
    assert before.expense.net_amount == Decimal("100.00")

    after = await update(h, expense_date=date(2012, 10, 1))
    assert after.expense.vat_rate == Decimal("21.00")
    # Same gross, different era, different split - recomputed because the DATE
    # changed, not only because the amount did.
    assert after.expense.vat_amount == Decimal("20.65")
    assert after.expense.net_amount == Decimal("98.35")


async def test_changing_the_gross_recomputes_the_amounts() -> None:
    h = harness()
    await update(
        h,
        expense_date=date(2025, 6, 1),
        gross_amount=Decimal("121.00"),
        vat_treatment=VatTreatment.BTW_21,
    )

    view = await update(h, gross_amount=Decimal("242.00"))

    assert view.expense.vat_amount == Decimal("42.00")
    assert view.expense.net_amount == Decimal("200.00")


async def test_net_plus_vat_is_always_the_gross() -> None:
    """FR-GL-001's balance, at the place the figures are made."""
    view = await update(
        harness(),
        expense_date=date(2025, 6, 1),
        gross_amount=Decimal("100.00"),
        vat_treatment=VatTreatment.BTW_21,
    )
    expense = view.expense

    assert expense.vat_amount == Decimal("17.36")
    assert expense.net_amount == Decimal("82.64")
    assert expense.net_amount + expense.vat_amount == expense.gross_amount
    assert expense.amounts_balance


async def test_a_zero_rated_treatment_leaves_the_whole_amount_as_net() -> None:
    view = await update(
        harness(),
        expense_date=date(2025, 6, 1),
        gross_amount=Decimal("100.00"),
        vat_treatment=VatTreatment.BTW_VERLEGD,
    )

    assert view.expense.vat_rate == Decimal("0.00")
    assert view.expense.vat_amount == Decimal("0.00")
    assert view.expense.net_amount == Decimal("100.00")


async def test_a_float_gross_is_refused() -> None:
    """NFR-031 / CLAUDE.md rule four, at the form's door."""
    with pytest.raises(VatError, match="float"):
        await update(
            harness(),
            expense_date=date(2025, 6, 1),
            gross_amount=121.0,  # type: ignore[arg-type]
            vat_treatment=VatTreatment.BTW_21,
        )


async def test_a_date_before_the_ruleset_is_refused() -> None:
    """Refused rather than defaulted to zero: a claim with no VAT because
    nobody knew the rate looks exactly like a genuinely zero-rated one.
    """
    with pytest.raises(VatRateUnavailable):
        await update(
            harness(),
            expense_date=date(1999, 1, 1),
            gross_amount=Decimal("121.00"),
            vat_treatment=VatTreatment.BTW_21,
        )


def test_the_margin_scheme_is_not_offered() -> None:
    """`btw_marge` charges VAT on the MARGIN, not the sale price. Extracting
    it from a gross receipt would compute the tax on the whole amount and
    overstate it by whatever the goods cost.

    Absent from the enum, and refused by migration 0033's CHECK - because the
    resulting row would be internally consistent and wrong only in its scheme,
    which nothing downstream would catch.
    """
    assert "btw_marge" not in {t.value for t in VatTreatment}


# ===========================================================================
# FR-EXP-001b: the category defaults from the user's history
# ===========================================================================


async def test_the_category_defaults_to_what_this_user_filed_this_supplier_under() -> None:
    h = harness()
    h.repository.history.append((h.user, "Albert Heijn", "Boodschappen"))
    h.repository.history.append((h.user, "NS", "Reiskosten"))
    await update(h, supplier="Albert Heijn")

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.suggested_category == "Boodschappen"


async def test_it_falls_back_to_what_they_file_most_things_under() -> None:
    h = harness()
    h.repository.history.extend(
        [
            (h.user, "NS", "Reiskosten"),
            (h.user, "Uber", "Reiskosten"),
            (h.user, "Coolblue", "Kantoorbenodigdheden"),
        ]
    )
    await update(h, supplier="A Supplier Never Seen Before")

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.suggested_category == "Reiskosten"


async def test_there_is_no_suggestion_on_a_first_expense() -> None:
    """Every person's first claim. Null rather than a guess."""
    h = harness()

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.suggested_category is None


async def test_a_suggestion_never_overrides_a_choice() -> None:
    """FR-EXP-001b defaults a field; it does not correct one.

    Once the person has chosen a category, the suggestion goes away rather
    than sitting beside their answer inviting a client to apply it.
    """
    h = harness()
    h.repository.history.append((h.user, "Albert Heijn", "Boodschappen"))

    await update(h, supplier="Albert Heijn", category="Representatie")
    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.expense.category == "Representatie"
    assert view.suggested_category is None


async def test_the_history_is_this_users_own() -> None:
    """FR-EXP-001b says "the user's history", and §8.4 scopes an Expense
    Submitter to their own submissions. A default drawn from a colleague's
    habits would also leak what they have been claiming.
    """
    h = harness()
    colleague = uuid.uuid4()
    h.repository.history.append((colleague, "Albert Heijn", "Boodschappen"))
    await update(h, supplier="Albert Heijn")

    view = await h.service.view(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.suggested_category is None


# ===========================================================================
# FR-EXP-001c: a partly-filled form is normal
# ===========================================================================


async def test_saving_a_single_field_is_allowed() -> None:
    """ "The product never blocks on extraction being available." A receipt
    that has just been photographed has an amount nobody has read yet.
    """
    view = await update(harness(), supplier="Albert Heijn")

    assert view.expense.supplier == "Albert Heijn"
    assert view.expense.gross_amount is None
    assert not view.can_be_marked_ready


async def test_an_unmentioned_field_is_left_alone() -> None:
    """The distinction PATCH exists for: not mentioning a field is not the
    same as clearing it, or a client saving one change would wipe the rest.
    """
    h = harness()
    await update(h, supplier="Albert Heijn", category="Boodschappen")

    view = await update(h, supplier="Jumbo")

    assert view.expense.supplier == "Jumbo"
    assert view.expense.category == "Boodschappen"


async def test_an_explicit_null_clears_a_field() -> None:
    """The other half of that distinction - somebody has to be able to undo a
    mistyped supplier.
    """
    h = harness()
    await update(h, supplier="Albert Hijn")

    view = await update(h, supplier=None)

    assert view.expense.supplier is None


async def test_whitespace_only_is_the_same_as_empty() -> None:
    """A supplier of "  " would satisfy NOT NULL and fail migration 0033's
    length check - the right answer arriving from the wrong layer.
    """
    view = await update(harness(), supplier="   ")
    assert view.expense.supplier is None


# ===========================================================================
# FR-EXP-001b + FR-EXP-001e: what `ready` requires
# ===========================================================================


async def test_the_required_set_is_the_minimum_plus_payment_method() -> None:
    """FR-EXP-001b's five, FR-EXP-001e's one, and the two derived figures that
    cannot be absent once a gross and a treatment are given.
    """
    assert set(Expense.REQUIRED_FIELDS) == {
        "expense_date",
        "supplier",
        "gross_amount",
        "vat_treatment",
        "vat_rate",
        "vat_amount",
        "category",
        "payment_method",
    }


async def test_marking_ready_lists_exactly_what_is_missing() -> None:
    h = harness()
    await update(h, supplier="Albert Heijn")

    with pytest.raises(IncompleteExpense) as raised:
        await h.service.mark_ready(
            administration_id=h.administration,
            expense_id=h.expense_id,
            actor_user_id=h.user,
        )

    assert "payment_method" in raised.value.missing
    assert "gross_amount" in raised.value.missing
    assert "supplier" not in raised.value.missing


async def test_payment_method_alone_blocks_a_claim() -> None:
    """FR-EXP-001e: it "determines the posting and cannot be reliably inferred
    later", so a claim that reached approval without it would have to be sent
    back to somebody's memory.
    """
    h = harness()
    await complete(h)
    await update(h, payment_method=None)

    with pytest.raises(IncompleteExpense) as raised:
        await h.service.mark_ready(
            administration_id=h.administration,
            expense_id=h.expense_id,
            actor_user_id=h.user,
        )
    assert raised.value.missing == ("payment_method",)


@pytest.mark.parametrize("method", list(PaymentMethod))
async def test_every_payment_method_fr_exp_001e_names_is_accepted(
    method: PaymentMethod,
) -> None:
    h = harness()
    await complete(h)
    view = await update(h, payment_method=method)

    assert view.expense.payment_method is method
    assert view.can_be_marked_ready


def test_only_personal_payment_is_reimbursable() -> None:
    """The distinction that changes who gets paid rather than only which
    account is credited - FR-EXP-003's reimbursement run reads it.
    """
    assert PaymentMethod.PERSONAL_REIMBURSABLE.is_reimbursable
    assert not PaymentMethod.BUSINESS_ACCOUNT.is_reimbursable
    assert not PaymentMethod.BUSINESS_CARD.is_reimbursable


async def test_a_complete_form_can_be_marked_ready() -> None:
    h = harness()
    await complete(h)

    view = await h.service.mark_ready(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )

    assert view.expense.status is ExpenseStatus.READY
    assert view.missing_fields == ()


async def test_marking_ready_twice_is_not_an_error() -> None:
    """A retry, or two taps. NFR-032's idempotency at the level above."""
    h = harness()
    await complete(h)
    first = await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )
    second = await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    assert first.expense.status is second.expense.status is ExpenseStatus.READY


async def test_a_submitted_claim_cannot_be_edited() -> None:
    """FR-EXP-001c's "always editable" is about extraction never locking a
    field against the person filling the form in - not about a claim already
    in front of an approver.
    """
    h = harness()
    await complete(h)
    await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    with pytest.raises(ExpenseAlreadyReady):
        await update(h, gross_amount=Decimal("999.00"))


# ===========================================================================
# Audit, tenancy and authorization
# ===========================================================================


async def test_the_audit_entry_names_fields_never_values() -> None:
    """An audit entry holding a supplier and an amount is a second copy of the
    claim in a table with different retention rules (IAM-093 vs FR-DOC-002).
    """
    h = harness()
    await update(h, supplier="Albert Heijn", gross_amount=Decimal("121.00"))

    entries = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "update_expense"
    ]
    assert entries
    detail = entries[0].detail
    assert "supplier" in detail["fields"]
    assert "Albert Heijn" not in str(detail)
    assert "121.00" not in str(detail)


async def test_submitting_records_the_payment_method() -> None:
    """FR-EXP-001e: which posting this produces is the one thing about the
    claim that cannot be re-derived later, so it is on the record.
    """
    h = harness()
    await complete(h)
    await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    submitted = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "submit_expense"
    ]
    assert submitted[0].detail["payment_method"] == "business_card"
    assert submitted[0].detail["vat_treatment"] == "btw_21"


async def test_another_administrations_expense_is_not_found() -> None:
    h = harness()

    with pytest.raises(ExpenseNotFound):
        await h.service.view(
            administration_id=uuid.uuid4(),
            expense_id=h.expense_id,
            actor_user_id=h.user,
        )


async def test_a_role_without_submit_expense_cannot_use_the_form() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await h.service.view(
            administration_id=h.administration,
            expense_id=h.expense_id,
            actor_user_id=h.user,
        )


async def test_a_denied_update_is_audited() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await update(h, supplier="Albert Heijn")

    denied = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.outcome is AuditOutcome.DENIED
    ]
    assert denied and denied[0].action == "submit_expense"


# ===========================================================================
# MOB-004: the Approve/View tabs' list
# ===========================================================================


async def test_list_by_status_filters_to_status_and_administration() -> None:
    h = harness()
    await complete(h)
    await h.service.mark_ready(
        administration_id=h.administration, expense_id=h.expense_id, actor_user_id=h.user
    )

    other_draft = Expense(
        id=uuid.uuid4(),
        administration_id=h.administration,
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.DRAFT,
        submitted_by_user_id=h.user,
    )
    h.repository.expenses[other_draft.id] = other_draft
    elsewhere = Expense(
        id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.DRAFT,
        submitted_by_user_id=h.user,
    )
    h.repository.expenses[elsewhere.id] = elsewhere

    drafts = await h.service.list_by_status(
        administration_id=h.administration,
        actor_user_id=h.user,
        status=ExpenseStatus.DRAFT,
        limit=50,
    )
    assert [e.id for e in drafts] == [other_draft.id]

    everything = await h.service.list_by_status(
        administration_id=h.administration, actor_user_id=h.user, status=None, limit=50
    )
    assert {e.id for e in everything} == {h.expense_id, other_draft.id}


async def test_a_role_without_submit_expense_cannot_list() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await h.service.list_by_status(
            administration_id=h.administration, actor_user_id=h.user, status=None, limit=50
        )


def test_unset_is_distinguishable_from_none() -> None:
    """The sentinel the PATCH semantics rest on. If `UNSET` were `None`, an
    unmentioned field and an explicitly cleared one would be the same request.
    """
    assert UNSET is not None
    assert UNSET is not False

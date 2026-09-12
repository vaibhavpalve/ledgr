"""Confirming an expense into the ledger - FR-EXP-001d, FR-EXP-001e.

Two things are being asserted, and they are different:

  * the ENTRY is right - it balances, it carries the VAT treatment, and the
    credit follows the payment method (FR-EXP-001e's "determines the posting")
  * the entry is made THROUGH the ledger service (CLAUDE.md non-negotiable #1),
    not beside it
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.authz.service import AuthorizationService
from api.expenses.model import (
    Expense,
    ExpenseStatus,
    IncompleteExpense,
    NotAuthorizedToCapture,
    PaymentMethod,
    VatTreatment,
)
from api.expenses.posting import (
    ExpenseAlreadyPosted,
    ExpensePostingService,
    NoOpenPeriod,
    NoPurchaseJournal,
    PostingConfigurationMissing,
)
from api.ledger.model import EntryInput, PostedEntry
from api.ledger.service import LedgerService
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

EXPENSE_ACCOUNT = uuid.uuid4()
VAT_ACCOUNT = uuid.uuid4()
BANK_ACCOUNT = uuid.uuid4()
CARD_ACCOUNT = uuid.uuid4()
REIMBURSEMENT_ACCOUNT = uuid.uuid4()
JOURNAL = uuid.uuid4()
PERIOD = uuid.uuid4()

ACCOUNTS: dict[tuple[str, str | None], uuid.UUID] = {
    ("expense_category", None): EXPENSE_ACCOUNT,
    ("vat_input", None): VAT_ACCOUNT,
    ("business_account", None): BANK_ACCOUNT,
    ("business_card", None): CARD_ACCOUNT,
    ("reimbursement_liability", None): REIMBURSEMENT_ACCOUNT,
}


@dataclass
class RecordingLedgerRepository:
    """Stands in for the ledger's own repository.

    Deliberately NOT a fake LedgerService: these tests run the real
    `LedgerService`, so `assert_balanced` and the audit event it writes are
    exercised rather than mocked away. What is faked is only the SQL beneath
    it.
    """

    #: The real repository derives this from the administration; the fake is
    #: told, because `LedgerService.post` attributes its audit event to it and
    #: a random value would hide the entry from every search.
    organization_id: uuid.UUID
    posted: list[EntryInput] = field(default_factory=list)

    async def post(self, entry: EntryInput) -> PostedEntry:
        self.posted.append(entry)
        return PostedEntry(
            id=uuid.uuid4(),
            organization_id=self.organization_id,
            administration_id=entry.administration_id,
            fiscal_year_id=uuid.uuid4(),
            period_id=entry.period_id,
            journal_id=entry.journal_id,
            entry_number=1,
            entry_date=entry.entry_date,
            description=entry.description,
            source_system=entry.source_system,
            posted_at=datetime.now(),
        )


@dataclass
class FakePostingRepository:
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    expenses: dict[uuid.UUID, Expense] = field(default_factory=dict)
    accounts: dict[tuple[str, str | None], uuid.UUID] = field(
        default_factory=lambda: dict(ACCOUNTS)
    )
    journal: uuid.UUID | None = JOURNAL
    open_periods: dict[date, uuid.UUID] = field(default_factory=dict)
    linked: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    documents_per_expense: int = 2

    async def get(self, *, administration_id: uuid.UUID, expense_id: uuid.UUID) -> Expense | None:
        expense = self.expenses.get(expense_id)
        if expense is None or expense.administration_id != administration_id:
            return None
        return expense

    async def account_for(
        self, *, administration_id: uuid.UUID, purpose: str, category: str | None = None
    ) -> uuid.UUID | None:
        if purpose == "expense_category":
            return self.accounts.get(("expense_category", category)) or self.accounts.get(
                ("expense_category", None)
            )
        return self.accounts.get((purpose, None))

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        return self.open_periods.get(on)

    async def purchase_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.journal

    async def mark_posted(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, entry_id: uuid.UUID
    ) -> Expense:
        updated = replace(
            self.expenses[expense_id],
            status=ExpenseStatus.POSTED,
            journal_entry_id=entry_id,
            posted_at=datetime.now(),
        )
        self.expenses[expense_id] = updated
        return updated

    async def link_documents_to_entry(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        entry_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> int:
        self.linked.append((expense_id, entry_id))
        return self.documents_per_expense

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id if administration_id == self.administration_id else None


@dataclass
class Harness:
    service: ExpensePostingService
    repository: FakePostingRepository
    ledger_repository: RecordingLedgerRepository
    audit: InMemoryAuditRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID
    expense_id: uuid.UUID


def complete_expense(
    administration_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    gross: str = "121.00",
    vat: str = "21.00",
    rate: str = "21.00",
    treatment: VatTreatment = VatTreatment.BTW_21,
    method: PaymentMethod = PaymentMethod.BUSINESS_ACCOUNT,
    on: date = date(2025, 6, 1),
    category: str = "Kantoorbenodigdheden",
) -> Expense:
    gross_amount = Decimal(gross)
    vat_amount = Decimal(vat)
    return Expense(
        id=uuid.uuid4(),
        administration_id=administration_id,
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.READY,
        submitted_by_user_id=user_id,
        expense_date=on,
        supplier="Albert Heijn",
        gross_amount=gross_amount,
        vat_treatment=treatment,
        vat_rate=Decimal(rate),
        vat_amount=vat_amount,
        # The database generates this; the fixture mirrors it.
        net_amount=gross_amount - vat_amount,
        category=category,
        payment_method=method,
    )


def harness(*, role: str = "Bookkeeper", **expense_kwargs: object) -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    expense = complete_expense(world.acme_books, world.user, **expense_kwargs)  # type: ignore[arg-type]
    repository = FakePostingRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    repository.expenses[expense.id] = expense
    assert expense.expense_date is not None
    repository.open_periods[expense.expense_date] = PERIOD

    ledger_repository = RecordingLedgerRepository(organization_id=world.acme)
    audit = InMemoryAuditRepository()
    return Harness(
        service=ExpensePostingService(
            repository=repository,
            ledger=LedgerService(ledger_repository, AuditLog(audit)),  # type: ignore[arg-type]
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(audit),
        ),
        repository=repository,
        ledger_repository=ledger_repository,
        audit=audit,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
        expense_id=expense.id,
    )


async def post(h: Harness) -> PostedEntry:
    return await h.service.post(
        administration_id=h.administration,
        expense_id=h.expense_id,
        actor_user_id=h.user,
    )


def lines_by_account(entry: EntryInput) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    return {line.account_id: (line.debit, line.credit) for line in entry.lines}


# ===========================================================================
# The entry
# ===========================================================================


async def test_an_expense_posts_net_vat_and_gross() -> None:
    """Dr expense (net), Dr input VAT, Cr the funding account (gross)."""
    h = harness()

    await post(h)

    entry = h.ledger_repository.posted[0]
    lines = lines_by_account(entry)
    assert lines[EXPENSE_ACCOUNT] == (Decimal("100.00"), Decimal("0.00"))
    assert lines[VAT_ACCOUNT] == (Decimal("21.00"), Decimal("0.00"))
    assert lines[BANK_ACCOUNT] == (Decimal("0.00"), Decimal("121.00"))


async def test_the_entry_balances() -> None:
    """FR-GL-001. `LedgerService.post` calls `assert_balanced` before anything
    is written, so an unbalanced entry never reaches the database - and the
    deferred trigger checks it again at COMMIT, which is the guarantee.
    """
    h = harness(gross="100.00", vat="17.36")

    await post(h)

    entry = h.ledger_repository.posted[0]
    assert entry.total_debit == entry.total_credit == Decimal("100.00")
    entry.assert_balanced()


async def test_the_vat_treatment_is_on_the_lines() -> None:
    """FR-VAT-001's raw material (migration 0035).

    On the cost line as well as the VAT line: a rubriek needs the TURNOVER,
    which is the net amount and not the tax. The funding line carries none -
    money moving is not turnover, and a code there would put the payment in a
    rubriek beside the cost.
    """
    h = harness()

    await post(h)

    entry = h.ledger_repository.posted[0]
    by_account = {line.account_id: line.vat_treatment for line in entry.lines}
    assert by_account[EXPENSE_ACCOUNT] == "btw_21"
    assert by_account[VAT_ACCOUNT] == "btw_21"
    assert by_account[BANK_ACCOUNT] is None


async def test_a_zero_rated_treatment_posts_no_vat_line() -> None:
    """btw_verlegd carries rate 0, so the entry is net = gross and two lines.

    Correct for the money that changed hands. The notional reverse-charge pair
    is FR-VAT-001's and can be derived later precisely because the treatment is
    recorded on the line.
    """
    h = harness(gross="100.00", vat="0.00", rate="0.00", treatment=VatTreatment.BTW_VERLEGD)

    await post(h)

    entry = h.ledger_repository.posted[0]
    assert len(entry.lines) == 2
    assert VAT_ACCOUNT not in lines_by_account(entry)
    assert lines_by_account(entry)[EXPENSE_ACCOUNT] == (Decimal("100.00"), Decimal("0.00"))
    assert entry.lines[0].vat_treatment == "btw_verlegd"


async def test_the_entry_says_where_it_came_from() -> None:
    """A reader of the ledger can tell an expense claim from a manual journal
    without joining anything.
    """
    h = harness()
    await post(h)

    entry = h.ledger_repository.posted[0]
    assert entry.source_system == "expenses"
    assert entry.description == "Albert Heijn - Kantoorbenodigdheden"
    assert entry.idempotency_key == f"expense:{h.expense_id}"


# ===========================================================================
# FR-EXP-001e: the payment method determines the posting
# ===========================================================================


@pytest.mark.parametrize(
    ("method", "account"),
    [
        (PaymentMethod.BUSINESS_ACCOUNT, BANK_ACCOUNT),
        (PaymentMethod.BUSINESS_CARD, CARD_ACCOUNT),
        (PaymentMethod.PERSONAL_REIMBURSABLE, REIMBURSEMENT_ACCOUNT),
    ],
    ids=lambda v: v.value if isinstance(v, PaymentMethod) else "account",
)
async def test_each_payment_method_credits_its_own_account(
    method: PaymentMethod, account: uuid.UUID
) -> None:
    """The debits never move; only the credit does. That is what FR-EXP-001e
    means by "determines the posting", and why the field cannot be filled in
    afterwards - a card slip looks the same whoever's card it was.
    """
    h = harness(method=method)

    await post(h)

    lines = lines_by_account(h.ledger_repository.posted[0])
    assert lines[account] == (Decimal("0.00"), Decimal("121.00"))
    assert lines[EXPENSE_ACCOUNT] == (Decimal("100.00"), Decimal("0.00"))


async def test_a_personally_paid_receipt_creates_a_reimbursement_liability() -> None:
    """The business owes this person money. FR-EXP-003's run is what pays it,
    and until then the liability has to be on the balance sheet.
    """
    h = harness(method=PaymentMethod.PERSONAL_REIMBURSABLE)

    await post(h)

    entry = h.ledger_repository.posted[0]
    credit = next(line for line in entry.lines if line.credit)
    assert credit.account_id == REIMBURSEMENT_ACCOUNT
    assert credit.credit == Decimal("121.00")
    # Readable as a reimbursement in the journal itself, not only from the
    # account's name.
    assert credit.description == "Te betalen declaratie"


async def test_the_reimbursement_is_flagged_in_the_audit_entry() -> None:
    h = harness(method=PaymentMethod.PERSONAL_REIMBURSABLE)

    await post(h)

    entries = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "post_expense"
    ]
    assert entries[0].detail["reimbursable"] is True
    assert entries[0].detail["payment_method"] == "personal_reimbursable"
    assert entries[0].detail["vat_treatment"] == "btw_21"


async def test_a_card_payment_is_not_reimbursable() -> None:
    h = harness(method=PaymentMethod.BUSINESS_CARD)

    await post(h)

    entries = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "post_expense"
    ]
    assert entries[0].detail["reimbursable"] is False


# ===========================================================================
# FR-EXP-001d: the bidirectional link
# ===========================================================================


async def test_posting_links_the_receipt_to_the_entry() -> None:
    """FR-EXP-001d's "linked bidirectionally to the resulting posting", which
    could not be completed until a posting existed (ADR-031 recorded the gap).
    """
    h = harness()

    posted = await post(h)

    assert h.repository.linked == [(h.expense_id, posted.id)]
    entries = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "post_expense"
    ]
    assert entries[0].detail["documents_linked"] == 2


# ===========================================================================
# Refusals, before anything is written
# ===========================================================================


async def test_an_incomplete_claim_cannot_be_posted() -> None:
    h = harness()
    h.repository.expenses[h.expense_id] = replace(
        h.repository.expenses[h.expense_id], payment_method=None
    )

    with pytest.raises(IncompleteExpense):
        await post(h)

    assert h.ledger_repository.posted == [], "nothing reached the ledger"


async def test_posting_twice_is_refused() -> None:
    """FR-GL-003: the correction is a reversing entry, never a second posting.
    A double claim with no trace of the first is what this prevents.
    """
    h = harness()
    await post(h)

    with pytest.raises(ExpenseAlreadyPosted):
        await post(h)

    assert len(h.ledger_repository.posted) == 1


@pytest.mark.parametrize(
    "purpose",
    ["expense_category", "vat_input", "business_account"],
)
async def test_a_missing_account_mapping_is_named_not_guessed(purpose: str) -> None:
    """Configuration that has not been done is visible; a wrong guess is not,
    and a lunch in "Loonheffingen" is found by an accountant months later.
    """
    h = harness()
    del h.repository.accounts[(purpose, None)]

    with pytest.raises(PostingConfigurationMissing) as raised:
        await post(h)

    assert raised.value.purpose == purpose
    assert h.ledger_repository.posted == []


async def test_an_administration_with_no_vat_never_needs_a_vat_account() -> None:
    """A zero-rated claim posts no VAT line, so the mapping is not consulted -
    an administration that only books btw_verlegd is not blocked by
    configuration it cannot use.
    """
    h = harness(gross="100.00", vat="0.00", rate="0.00", treatment=VatTreatment.BTW_0)
    del h.repository.accounts[("vat_input", None)]

    await post(h)

    assert len(h.ledger_repository.posted) == 1


async def test_a_date_in_no_open_period_is_refused() -> None:
    """FR-GL-007. An expense belongs in the period it happened in; posting it
    into an open one instead would put the cost in the wrong month.
    """
    h = harness()
    h.repository.open_periods.clear()

    with pytest.raises(NoOpenPeriod):
        await post(h)

    assert h.ledger_repository.posted == []


async def test_an_ambiguous_purchase_journal_is_refused() -> None:
    """FR-GL-013 numbers per journal, so which one a claim lands in shows up in
    its entry number forever.
    """
    h = harness()
    h.repository.journal = None

    with pytest.raises(NoPurchaseJournal):
        await post(h)


# ===========================================================================
# Authorization
# ===========================================================================


async def test_posting_takes_the_ledgers_permission_not_the_submitters() -> None:
    """§8.4 lets an Expense Submitter submit a claim and not post one. Keeping
    that separation is the point - somebody who can only capture receipts must
    not be able to put them in the books.
    """
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToCapture) as raised:
        await post(h)

    assert raised.value.action == "post"
    assert raised.value.resource_type == "journal_entry"
    assert h.ledger_repository.posted == []


async def test_a_denied_posting_is_audited() -> None:
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToCapture):
        await post(h)

    denied = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.outcome is AuditOutcome.DENIED
    ]
    assert denied and denied[0].action == "post_journal_entry"


async def test_the_ledger_records_its_own_posting_event() -> None:
    """Two entries, answering different questions: the ledger's POSTING event
    says what was posted, and this module's says who confirmed the claim.
    """
    h = harness()

    await post(h)

    actions = [e.action for e in await h.audit.search(organization_id=h.organization)]
    assert "post_journal_entry" in actions, "the ledger service wrote its own"
    assert "post_expense" in actions, "and the claim's confirmation is recorded too"

    posting_events = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "post_expense"
    ]
    assert posting_events[0].category is AuditCategory.POSTING


async def test_the_claim_is_marked_posted_with_its_entry() -> None:
    h = harness()

    posted = await post(h)

    expense = h.repository.expenses[h.expense_id]
    assert expense.status is ExpenseStatus.POSTED
    assert expense.journal_entry_id == posted.id

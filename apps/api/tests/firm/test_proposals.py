"""Booking proposals (ADR-110, FR-BNK-003/004): generation, supersede and decide, with in-memory
fakes. The database-backed behaviour - RLS, 0079's trigger and unique index, the real posting
through the Bank screen's path, and tenant isolation of every route - is in
tests/integration/test_booking_proposals.py.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.audit.log import AuditCategory, AuditEvent, AuditOutcome
from api.authz.model import AuthorizationDecision
from api.bank.csv_parser import StatementRow
from api.bank.matching import Confidence, ScoredCandidate
from api.bank.model import (
    BankAccount,
    BankAccountStatus,
    BankTransaction,
    CandidateKind,
    ExpenseNotMatchable,
    MatchCandidate,
    TransactionStatus,
)
from api.bank.service import BankService
from api.firm.proposals import (
    NewProposal,
    PendingProposal,
    ProposalGenerator,
    ProposalHook,
    ProposalStatus,
    TargetAccount,
    group_key,
    new_proposal,
    normalise_counterparty,
    plan,
)
from api.firm.proposals_repository import ProposalRecord, ProposalRow
from api.firm.proposals_service import (
    Decision,
    DecisionRequest,
    ProposalReviewService,
    failure_reason,
    group,
)

ADMIN = uuid.uuid4()
OTHER_ADMIN = uuid.uuid4()
ORG = uuid.uuid4()
USER = uuid.uuid4()
#: Both administrations are in the caller's portfolio unless a test says otherwise, so the per-item
#: authorize() is what refuses OTHER_ADMIN in the tests that need it.
PORTFOLIO = frozenset({ADMIN, OTHER_ADMIN})


def line(
    amount: str, name: str | None = "KPN", description: str | None = "Telefoon"
) -> BankTransaction:
    return BankTransaction(
        id=uuid.uuid4(),
        administration_id=ADMIN,
        bank_account_id=uuid.uuid4(),
        booking_date=date(2026, 9, 30),
        value_date=None,
        amount=Decimal(amount),
        currency="EUR",
        counterparty_name=name,
        counterparty_iban=None,
        description=description,
        external_id=uuid.uuid4().hex,
        status=TransactionStatus.UNMATCHED,
        matched_sales_invoice_id=None,
        matched_payment_id=None,
        journal_entry_id=None,
        reconciled_at=None,
    )


def receipt(supplier: str, gross: str, number: str | None = None) -> MatchCandidate:
    return MatchCandidate(
        kind=CandidateKind.EXPENSE,
        document_id=uuid.uuid4(),
        reference=number,
        party_name=supplier,
        amount=Decimal(gross),
        document_date=date(2026, 9, 28),
    )


def invoice(customer: str, amount: str, reference: str) -> MatchCandidate:
    return MatchCandidate(
        kind=CandidateKind.SALES_INVOICE,
        document_id=uuid.uuid4(),
        reference=reference,
        party_name=customer,
        amount=Decimal(amount),
        document_date=date(2026, 9, 1),
    )


# ---------------------------------------------------------------------------
# group_key
# ---------------------------------------------------------------------------


def test_counterparty_is_normalised_for_grouping() -> None:
    assert normalise_counterparty("KPN B.V.") == "kpn"
    assert normalise_counterparty("kpn bv") == "kpn"
    assert normalise_counterparty("  Hotel  De Gouden Leeuw ") == "hotel gouden leeuw"
    assert normalise_counterparty(None) == ""


def test_group_key_is_counterparty_and_account() -> None:
    assert group_key("KPN B.V.", "4500") == group_key("KPN", "4500") == "kpn|4500"
    assert group_key("KPN", "4600") != group_key("KPN", "4500")
    # Never empty - 0079 requires a key.
    assert group_key(None, None) == "?|?"


def test_new_proposal_carries_a_positive_decimal_amount() -> None:
    outgoing = line("-70.27")
    scored = ScoredCandidate(receipt("KPN B.V.", "70.27"), Confidence.HIGH, ("name",))
    proposal = new_proposal(outgoing, scored, TargetAccount("4500", "Telefoon"))
    assert proposal.amount == Decimal("70.27")
    assert isinstance(proposal.amount, Decimal)
    assert proposal.group_key == "kpn|4500"
    assert proposal.document_kind is CandidateKind.EXPENSE
    assert proposal.counterparty == "KPN B.V."


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


def _certain(
    transaction: BankTransaction, candidate: MatchCandidate
) -> dict[uuid.UUID, tuple[BankTransaction, ScoredCandidate]]:
    return {
        transaction.id: (transaction, ScoredCandidate(candidate, Confidence.HIGH, ("reference",)))
    }


def test_plan_creates_for_a_new_certain_line() -> None:
    t, doc = line("-70.27"), receipt("KPN", "70.27")
    decided = plan(_certain(t, doc), [])
    assert [c[0].id for c in decided.create] == [t.id]
    assert decided.supersede == () and decided.keep == ()


def test_plan_keeps_a_pending_proposal_that_is_still_certain() -> None:
    t, doc = line("-70.27"), receipt("KPN", "70.27")
    pending = PendingProposal(uuid.uuid4(), t.id, doc.document_id)
    decided = plan(_certain(t, doc), [pending])
    assert decided.keep == (pending.id,)
    assert decided.create == () and decided.supersede == ()


def test_plan_supersedes_when_a_better_candidate_appears() -> None:
    t, old, better = line("-70.27"), receipt("KPN", "70.27"), receipt("KPN", "70.27")
    pending = PendingProposal(uuid.uuid4(), t.id, old.document_id)
    decided = plan(_certain(t, better), [pending])
    assert decided.supersede == (pending.id,)
    assert [c[1].candidate.document_id for c in decided.create] == [better.document_id]


def test_plan_supersedes_a_line_that_is_no_longer_certain() -> None:
    pending = PendingProposal(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    decided = plan({}, [pending])
    assert decided.supersede == (pending.id,)


def test_plan_supersedes_a_second_pending_row_for_one_line() -> None:
    t, doc = line("-70.27"), receipt("KPN", "70.27")
    first = PendingProposal(uuid.uuid4(), t.id, doc.document_id)
    second = PendingProposal(uuid.uuid4(), t.id, doc.document_id)
    decided = plan(_certain(t, doc), [first, second])
    assert decided.keep == (first.id,)
    assert decided.supersede == (second.id,)
    assert decided.create == ()


# ---------------------------------------------------------------------------
# ProposalGenerator
# ---------------------------------------------------------------------------


@dataclass
class StoredProposal:
    id: uuid.UUID
    administration_id: uuid.UUID
    bank_transaction_id: uuid.UUID
    document_id: uuid.UUID
    document_kind: CandidateKind
    status: ProposalStatus
    amount: Decimal
    group_key: str
    account_code: str | None
    decided_by: uuid.UUID | None = None


class FakeMatching:
    def __init__(
        self, transactions: list[BankTransaction], documents: list[MatchCandidate]
    ) -> None:
        self.transactions = transactions
        self.documents = documents

    async def unmatched_transactions(self, *, administration_id: uuid.UUID) -> list[Any]:
        return [t for t in self.transactions if t.status is TransactionStatus.UNMATCHED]

    async def open_invoice_balances(self, *, administration_id: uuid.UUID) -> list[Any]:
        return [d for d in self.documents if d.kind is CandidateKind.SALES_INVOICE]

    async def bank_paid_expenses(
        self, *, administration_id: uuid.UUID, amount: Decimal | None = None
    ) -> list[Any]:
        return [d for d in self.documents if d.kind is CandidateKind.EXPENSE]


class FakeProposals:
    """In-memory booking_proposal, with 0079's rules: one pending row per line, status moves from
    pending once. `isolated()` restores the rows on failure, as a savepoint does."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, StoredProposal] = {}
        self.fail_with: Exception | None = None

    @asynccontextmanager
    async def isolated(self) -> AsyncIterator[object]:
        snapshot = copy.deepcopy(self.rows)
        try:
            yield None
        except BaseException:
            self.rows = snapshot
            raise

    async def pending_for(self, *, administration_id: uuid.UUID) -> list[PendingProposal]:
        if self.fail_with is not None:
            raise self.fail_with
        return [
            PendingProposal(r.id, r.bank_transaction_id, r.document_id)
            for r in self.rows.values()
            if r.administration_id == administration_id and r.status is ProposalStatus.PENDING
        ]

    async def target_accounts(
        self, *, administration_id: uuid.UUID, documents: Sequence[tuple[CandidateKind, uuid.UUID]]
    ) -> dict[uuid.UUID, TargetAccount]:
        return {d: TargetAccount("4500", "Telefoon") for _, d in documents}

    async def insert(self, *, administration_id: uuid.UUID, proposal: NewProposal) -> bool:
        if any(
            r.bank_transaction_id == proposal.bank_transaction_id
            and r.status is ProposalStatus.PENDING
            for r in self.rows.values()
        ):
            return False
        row = StoredProposal(
            id=uuid.uuid4(),
            administration_id=administration_id,
            bank_transaction_id=proposal.bank_transaction_id,
            document_id=proposal.document_id,
            document_kind=proposal.document_kind,
            status=ProposalStatus.PENDING,
            amount=proposal.amount,
            group_key=proposal.group_key,
            account_code=proposal.account_code,
        )
        self.rows[row.id] = row
        return True

    def _move(self, row: StoredProposal, status: ProposalStatus) -> None:
        assert row.status is ProposalStatus.PENDING, "0079: status moves from pending only"
        row.status = status

    async def supersede(self, *, administration_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> int:
        moved = 0
        for i in ids:
            row = self.rows.get(i)
            if row is not None and row.status is ProposalStatus.PENDING:
                self._move(row, ProposalStatus.SUPERSEDED)
                moved += 1
        return moved

    async def supersede_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> int:
        settled = [r for r in self.rows.values() if r.bank_transaction_id == transaction_id]
        documents = {r.document_id for r in settled}
        moved = 0
        for row in self.rows.values():
            if row.status is ProposalStatus.PENDING and (
                row.bank_transaction_id == transaction_id or row.document_id in documents
            ):
                self._move(row, ProposalStatus.SUPERSEDED)
                moved += 1
        return moved

    # -- the decide side ----------------------------------------------------------

    async def get(self, *, proposal_id: uuid.UUID) -> ProposalRecord | None:
        row = self.rows.get(proposal_id)
        if row is None:
            return None
        return ProposalRecord(
            id=row.id,
            organization_id=ORG,
            administration_id=row.administration_id,
            bank_transaction_id=row.bank_transaction_id,
            document_id=row.document_id,
            document_kind=row.document_kind,
            status=row.status,
            amount=row.amount,
        )

    async def claim(
        self, *, proposal_id: uuid.UUID, status: ProposalStatus, user_id: uuid.UUID
    ) -> bool:
        row = self.rows.get(proposal_id)
        if row is None or row.status is not ProposalStatus.PENDING:
            return False
        self._move(row, status)
        row.decided_by = user_id
        return True

    async def pending_rows(self, *, administration_ids: Sequence[uuid.UUID]) -> list[ProposalRow]:
        return []

    def pending(self) -> list[StoredProposal]:
        return [r for r in self.rows.values() if r.status is ProposalStatus.PENDING]


async def test_refresh_proposes_only_a_single_certain_match() -> None:
    kpn = line("-70.27", "KPN B.V.")
    vague = line("-12.00", "Albert Heijn", "Boodschappen")
    matching = FakeMatching(
        [kpn, vague],
        [
            receipt("KPN", "70.27"),
            # Two receipts of 12.00 from nobody the line names - LOW, never proposed.
            receipt("Shell", "12.00"),
            receipt("Esso", "12.00"),
        ],
    )
    proposals = FakeProposals()
    result = await ProposalGenerator(proposals=proposals, matching=matching).refresh(
        administration_id=ADMIN
    )
    assert result.created == 1
    [row] = proposals.pending()
    assert row.bank_transaction_id == kpn.id
    assert row.group_key == "kpn|4500"
    assert row.amount == Decimal("70.27")


async def test_refresh_never_proposes_a_partial_payment() -> None:
    payment = line("400.00", "De Vries", "Factuur 2026-0001")
    owing = invoice("De Vries Holding B.V.", "605.00", "2026-0001")
    proposals = FakeProposals()
    result = await ProposalGenerator(
        proposals=proposals, matching=FakeMatching([payment], [owing])
    ).refresh(administration_id=ADMIN)
    assert result.created == 0
    assert proposals.pending() == []


async def test_refresh_never_proposes_an_ambiguous_match() -> None:
    # One document certain for two lines (a customer paid twice) is certain for neither.
    first, second = line("605.00", "De Vries"), line("605.00", "De Vries")
    owing = invoice("De Vries Holding B.V.", "605.00", "2026-0001")
    proposals = FakeProposals()
    await ProposalGenerator(
        proposals=proposals, matching=FakeMatching([first, second], [owing])
    ).refresh(administration_id=ADMIN)
    assert proposals.pending() == []


async def test_refresh_is_idempotent() -> None:
    matching = FakeMatching([line("-70.27", "KPN")], [receipt("KPN", "70.27")])
    proposals = FakeProposals()
    generator = ProposalGenerator(proposals=proposals, matching=matching)
    await generator.refresh(administration_id=ADMIN)
    again = await generator.refresh(administration_id=ADMIN)
    assert (again.created, again.superseded, again.kept) == (0, 0, 1)
    assert len(proposals.rows) == 1


async def test_refresh_supersedes_when_the_line_is_matched_by_other_means() -> None:
    kpn = line("-70.27", "KPN")
    matching = FakeMatching([kpn], [receipt("KPN", "70.27")])
    proposals = FakeProposals()
    generator = ProposalGenerator(proposals=proposals, matching=matching)
    await generator.refresh(administration_id=ADMIN)

    matching.transactions = []  # reconciled on the Bank screen
    result = await generator.refresh(administration_id=ADMIN)
    assert result.superseded == 1
    assert proposals.pending() == []
    [row] = proposals.rows.values()
    assert row.status is ProposalStatus.SUPERSEDED


async def test_refresh_supersedes_when_a_competing_candidate_appears() -> None:
    kpn = line("-70.27", "KPN")
    matching = FakeMatching([kpn], [receipt("KPN", "70.27")])
    proposals = FakeProposals()
    generator = ProposalGenerator(proposals=proposals, matching=matching)
    await generator.refresh(administration_id=ADMIN)

    # A second KPN receipt of the same amount: no longer one certain match.
    matching.documents.append(receipt("KPN", "70.27"))
    result = await generator.refresh(administration_id=ADMIN)
    assert result.superseded == 1 and result.created == 0
    assert proposals.pending() == []


async def test_transaction_settled_withdraws_the_line_and_its_document() -> None:
    kpn = line("-70.27", "KPN")
    proposals = FakeProposals()
    generator = ProposalGenerator(
        proposals=proposals, matching=FakeMatching([kpn], [receipt("KPN", "70.27")])
    )
    await generator.refresh(administration_id=ADMIN)
    assert await generator.transaction_settled(administration_id=ADMIN, transaction_id=kpn.id) == 1
    assert proposals.pending() == []


async def test_the_hook_never_raises_into_the_import() -> None:
    proposals = FakeProposals()
    proposals.fail_with = RuntimeError("relation booking_proposal does not exist")
    hook = ProposalHook(
        generator=ProposalGenerator(proposals=proposals, matching=FakeMatching([], [])),
        proposals=proposals,
    )
    await hook.refresh(administration_id=ADMIN)  # no exception
    assert hook.failures == ["RuntimeError"]


# ---------------------------------------------------------------------------
# The hook inside BankService.import_rows
# ---------------------------------------------------------------------------


class _RecordingHook:
    def __init__(self) -> None:
        self.refreshed: list[uuid.UUID] = []

    async def refresh(self, *, administration_id: uuid.UUID) -> None:
        self.refreshed.append(administration_id)

    async def transaction_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> None:
        pass


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ImportRepository:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def get_account(self, **_: Any) -> BankAccount:
        return BankAccount(
            id=uuid.uuid4(),
            administration_id=ADMIN,
            name="Bank",
            iban=None,
            currency="EUR",
            ledger_account_id=uuid.uuid4(),
            status=BankAccountStatus.ACTIVE,
            created_at=datetime.now(UTC),
        )

    async def create_import(self, **_: Any) -> uuid.UUID:
        return uuid.uuid4()

    async def insert_transaction(self, *, external_id: str, **_: Any) -> bool:
        new = external_id not in self.seen
        self.seen.add(external_id)
        return new

    async def finalize_import(self, **_: Any) -> None:
        pass


async def test_an_import_with_new_lines_refreshes_proposals_and_a_duplicate_does_not() -> None:
    hook = _RecordingHook()
    service = BankService(
        _ImportRepository(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        _FakeAudit(),  # type: ignore[arg-type]
        hook,
    )
    row = StatementRow(
        booking_date=date(2026, 9, 30),
        amount=Decimal("-70.27"),
        counterparty_name="KPN",
        counterparty_iban=None,
        description="Telefoon",
    )
    account = uuid.uuid4()
    for _ in range(2):
        await service.import_rows(
            organization_id=ORG,
            administration_id=ADMIN,
            bank_account_id=account,
            rows=[row],
            filename=None,
            source_format="psd2",
            actor_user_id=None,
        )
    assert hook.refreshed == [ADMIN]  # the second import added nothing


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


class FakeAuthorization:
    def __init__(self, allowed: set[uuid.UUID]) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str, str, uuid.UUID]] = []

    async def authorize(self, request: Any) -> AuthorizationDecision:
        administration_id = request.target.administration_id
        self.asked.append((request.action, request.resource_type, administration_id))
        if administration_id in self.allowed:
            return AuthorizationDecision(allowed=True, reason="allowed")
        return AuthorizationDecision(allowed=False, reason="no_matching_grant")


@dataclass
class FakeBank:
    """Stands in for BankService's two reconciliation entry points; counts postings per line."""

    proposals: FakeProposals
    posted: dict[uuid.UUID, int] = field(default_factory=dict)
    refuse: set[uuid.UUID] = field(default_factory=set)

    async def _post(self, administration_id: uuid.UUID, transaction_id: uuid.UUID) -> Any:
        if transaction_id in self.refuse:
            raise ExpenseNotMatchable("the receipt is already matched to another bank line")
        # Counted, not refused: a second booking must show up in the assertions, not be turned
        # into a reported failure by decide()'s per-item error handling.
        self.posted[transaction_id] = self.posted.get(transaction_id, 0) + 1
        # BankService._record_reconciled's hook: the line's other pending proposals go.
        await self.proposals.supersede_settled(
            administration_id=administration_id, transaction_id=transaction_id
        )

    async def reconcile_with_expense(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID, **_: Any
    ) -> Any:
        return await self._post(administration_id, transaction_id)

    async def reconcile_with_invoice(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID, **_: Any
    ) -> Any:
        return await self._post(administration_id, transaction_id)


def _seed(proposals: FakeProposals, administration_id: uuid.UUID = ADMIN) -> StoredProposal:
    row = StoredProposal(
        id=uuid.uuid4(),
        administration_id=administration_id,
        bank_transaction_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_kind=CandidateKind.EXPENSE,
        status=ProposalStatus.PENDING,
        amount=Decimal("70.27"),
        group_key="kpn|4500",
        account_code="4500",
    )
    proposals.rows[row.id] = row
    return row


def _service(
    proposals: FakeProposals, allowed: set[uuid.UUID] | None = None
) -> tuple[ProposalReviewService, FakeBank, _FakeAudit, FakeAuthorization]:
    bank = FakeBank(proposals)
    audit = _FakeAudit()
    authorization = FakeAuthorization(allowed if allowed is not None else {ADMIN})
    service = ProposalReviewService(
        store=proposals,  # type: ignore[arg-type]
        authorization=authorization,  # type: ignore[arg-type]
        bank=bank,
        audit_log=audit,  # type: ignore[arg-type]
    )
    return service, bank, audit, authorization


async def test_approving_posts_through_the_bank_once_and_audits_it() -> None:
    proposals = FakeProposals()
    row = _seed(proposals)
    service, bank, audit, authorization = _service(proposals)

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[DecisionRequest(row.id, Decision.APPROVE)],
    )

    assert (outcome.approved, outcome.rejected, outcome.failed) == (1, 0, [])
    assert bank.posted == {row.bank_transaction_id: 1}
    assert row.status is ProposalStatus.APPROVED and row.decided_by == USER
    assert authorization.asked == [("reconcile", "bank_transaction", ADMIN)]
    [event] = audit.events
    assert event.action == "approve_booking_proposal"
    assert event.category is AuditCategory.POSTING
    assert event.outcome is AuditOutcome.SUCCESS
    assert event.administration_id == ADMIN and event.resource_id == row.id


async def test_rejecting_changes_only_the_proposal() -> None:
    proposals = FakeProposals()
    row = _seed(proposals)
    service, bank, audit, _ = _service(proposals)

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[DecisionRequest(row.id, Decision.REJECT)],
    )

    assert (outcome.approved, outcome.rejected) == (0, 1)
    assert bank.posted == {}
    assert row.status is ProposalStatus.REJECTED
    assert [e.action for e in audit.events] == ["reject_booking_proposal"]


async def test_a_retried_approval_is_answered_again_without_posting_twice() -> None:
    proposals = FakeProposals()
    row = _seed(proposals)
    service, bank, audit, _ = _service(proposals)
    request = [DecisionRequest(row.id, Decision.APPROVE)]

    await service.decide(
        user_id=USER, acting_organization_id=ORG, portfolio=PORTFOLIO, decisions=request
    )
    again = await service.decide(
        user_id=USER, acting_organization_id=ORG, portfolio=PORTFOLIO, decisions=request
    )

    assert (again.approved, again.failed) == (1, [])
    assert bank.posted == {row.bank_transaction_id: 1}
    assert len(audit.events) == 1


async def test_the_opposite_decision_after_one_is_refused() -> None:
    proposals = FakeProposals()
    row = _seed(proposals)
    service, bank, _, _ = _service(proposals)
    await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[DecisionRequest(row.id, Decision.REJECT)],
    )

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[DecisionRequest(row.id, Decision.APPROVE)],
    )
    assert [f.reason for f in outcome.failed] == ["proposal_already_decided"]
    assert bank.posted == {}
    assert row.status is ProposalStatus.REJECTED


async def test_another_administrations_proposal_is_refused_and_the_rest_still_decided() -> None:
    proposals = FakeProposals()
    mine = _seed(proposals)
    theirs = _seed(proposals, OTHER_ADMIN)
    service, bank, audit, _ = _service(proposals, allowed={ADMIN})
    unknown = uuid.uuid4()

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[
            DecisionRequest(theirs.id, Decision.APPROVE),
            DecisionRequest(unknown, Decision.APPROVE),
            DecisionRequest(mine.id, Decision.APPROVE),
        ],
    )

    assert outcome.approved == 1
    assert [(f.proposal_id, f.reason) for f in outcome.failed] == [
        (theirs.id, "not_permitted"),
        (unknown, "proposal_not_found"),
    ]
    assert theirs.status is ProposalStatus.PENDING
    assert set(bank.posted) == {mine.bank_transaction_id}
    denied = [e for e in audit.events if e.outcome is AuditOutcome.DENIED]
    assert [e.administration_id for e in denied] == [OTHER_ADMIN]


async def test_a_proposal_outside_the_portfolio_is_refused_before_anything_else() -> None:
    proposals = FakeProposals()
    theirs = _seed(proposals, OTHER_ADMIN)
    # The per-item check would allow it; the portfolio the route resolved does not include it.
    service, bank, audit, authorization = _service(proposals, allowed={ADMIN, OTHER_ADMIN})

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=frozenset({ADMIN}),
        decisions=[DecisionRequest(theirs.id, Decision.APPROVE)],
    )

    assert [f.reason for f in outcome.failed] == ["not_permitted"]
    assert theirs.status is ProposalStatus.PENDING
    assert bank.posted == {}
    assert authorization.asked == []
    assert [(e.outcome, e.administration_id) for e in audit.events] == [
        (AuditOutcome.DENIED, OTHER_ADMIN)
    ]


async def test_a_refused_posting_leaves_the_proposal_pending_and_is_reported() -> None:
    proposals = FakeProposals()
    row = _seed(proposals)
    service, bank, audit, _ = _service(proposals)
    bank.refuse.add(row.bank_transaction_id)

    outcome = await service.decide(
        user_id=USER,
        acting_organization_id=ORG,
        portfolio=PORTFOLIO,
        decisions=[DecisionRequest(row.id, Decision.APPROVE)],
    )

    assert outcome.approved == 0
    assert [f.reason for f in outcome.failed] == ["bank_expense_not_matchable"]
    # The savepoint rolled the claim back with the refused posting.
    assert proposals.rows[row.id].status is ProposalStatus.PENDING
    assert [e.outcome for e in audit.events] == [AuditOutcome.FAILURE]


def test_failure_reasons_are_the_bank_screens_own() -> None:
    assert failure_reason(ExpenseNotMatchable("x")) == "bank_expense_not_matchable"
    assert failure_reason(RuntimeError("x")) == "posting_failed"


# NFR-032 / NFR-042: whatever sequence of decisions arrives - duplicates within a request,
# retries, contradictory decisions, several requests - a line is booked at most once, and booked
# exactly when its proposal ended up approved.
_decision = st.tuples(st.integers(min_value=0, max_value=3), st.sampled_from(list(Decision)))


@settings(max_examples=150, deadline=None)
@given(
    requests=st.lists(st.lists(_decision, max_size=6), min_size=1, max_size=5),
    refused=st.sets(st.integers(min_value=0, max_value=3)),
)
def test_deciding_never_double_posts(
    requests: list[list[tuple[int, Decision]]], refused: set[int]
) -> None:
    import asyncio

    async def run() -> None:
        proposals = FakeProposals()
        seeded = [_seed(proposals) for _ in range(4)]
        service, bank, _, _ = _service(proposals)
        bank.refuse.update(seeded[i].bank_transaction_id for i in refused)
        for request in requests:
            await service.decide(
                user_id=USER,
                acting_organization_id=ORG,
                portfolio=PORTFOLIO,
                decisions=[DecisionRequest(seeded[i].id, decision) for i, decision in request],
            )
        for index, seed in enumerate(seeded):
            row = proposals.rows[seed.id]
            booked = bank.posted.get(row.bank_transaction_id, 0)
            assert booked <= 1
            assert (booked == 1) == (row.status is ProposalStatus.APPROVED)
            if index in refused:
                assert row.status is not ProposalStatus.APPROVED
            if not any(i == index for request in requests for i, _ in request):
                assert row.status is ProposalStatus.PENDING

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The review sheet's grouping
# ---------------------------------------------------------------------------


def _row(key: str, administration_id: uuid.UUID, amount: str) -> ProposalRow:
    return ProposalRow(
        id=uuid.uuid4(),
        administration_id=administration_id,
        display_name="Client",
        group_key=key,
        counterparty="KPN B.V.",
        account_code="4500",
        account_name="Telefoon",
        amount=Decimal(amount),
        date=date(2026, 9, 30),
        description="Telefoon",
    )


def test_groups_count_clients_and_sum_decimals() -> None:
    rows = [
        _row("kpn|4500", ADMIN, "70.27"),
        _row("kpn|4500", ADMIN, "70.27"),
        _row("kpn|4500", OTHER_ADMIN, "0.10"),
        _row("shell|4300", ADMIN, "50.00"),
    ]
    first, second = group(rows)
    assert (first.group_key, first.count, first.client_count) == ("kpn|4500", 3, 2)
    assert first.total_amount == Decimal("140.64")
    assert str(first.total_amount) == "140.64"
    assert (second.group_key, second.count) == ("shell|4300", 1)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_values_are_the_contracts(decision: str) -> None:
    assert Decision(decision).value == decision

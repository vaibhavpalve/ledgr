"""Booking proposals: auto-bookings that wait for a person (FR-BNK-003/004, FR-FRM-002, ADR-110).

    FR-BNK-004  ... auto-post above the high threshold, propose between thresholds, queue for
                manual handling below.

Boeklite does not auto-post. Where matching (api.bank.matching) finds a single certain HIGH
candidate for an unmatched bank line, it writes a `booking_proposal` (migration 0079) - a row
OUTSIDE the ledger. The firm home's review sheet groups them ("KPN -> 4500 Telefoon - 12 bookings -
9 clients"); approving one books it through the existing bank reconciliation path
(api.firm.proposals_service), rejecting one changes only the proposal.

This module is the generation half:

    plan()                pure: given what matching finds certain now and what is pending,
                          which proposals to create and which to withdraw ("supersede")
    ProposalGenerator     reads the administration's unmatched lines and open documents, runs the
                          existing matching, and applies the plan
    ProposalHook          what api.bank.service calls after an import and after a reconciliation -
                          the generator, wrapped so a proposal failure can never fail the import
                          or the reconciliation that triggered it

"Certain" is exactly the bank screen's "match all certain": `matching.suggest` already demotes a
HIGH match that competes with another (two documents for one line, one document for two lines) to
MEDIUM, so only an unambiguous HIGH reaches a proposal. A partial payment is never HIGH, so it is
never proposed.
"""

from __future__ import annotations

import enum
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from api.bank.matching import Confidence, ScoredCandidate, suggest
from api.bank.model import BankTransaction, CandidateKind, MatchCandidate

logger = logging.getLogger(__name__)


class ProposalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


#: Words that say nothing about WHO - "KPN B.V." and "KPN" are one counterparty for grouping.
_LEGAL_FORMS = frozenset(
    {"bv", "nv", "vof", "cv", "holding", "ltd", "gmbh", "inc", "sa", "the", "de", "het"}
)


def normalise_counterparty(name: str | None) -> str:
    """Lower case, punctuation and legal-form suffixes dropped, whitespace collapsed:
    "KPN B.V." and "kpn bv" both become "kpn"."""
    # Dots dropped before splitting, so "b.v." is the word "bv" and recognised as a legal form.
    words = re.findall(r"[0-9a-zà-ÿ]+", (name or "").lower().replace(".", ""))
    return " ".join(w for w in words if w not in _LEGAL_FORMS)


def group_key(counterparty: str | None, account_code: str | None) -> str:
    """What the review sheet groups by: who was paid, and into which account it books. Never
    empty (0079's check) - an unknown side is "?"."""
    return f"{normalise_counterparty(counterparty) or '?'}|{account_code or '?'}"


@dataclass(frozen=True, slots=True)
class PendingProposal:
    id: uuid.UUID
    bank_transaction_id: uuid.UUID | None
    document_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class TargetAccount:
    """The account a matched document books into - the receipt's cost account, or the invoice's
    receivable - as read from the document's own posted entry."""

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class NewProposal:
    """One proposal to write. `amount` is what the line pays toward the document, always
    positive (NFR-031: Decimal)."""

    bank_transaction_id: uuid.UUID
    document_id: uuid.UUID
    document_kind: CandidateKind
    confidence: Confidence
    counterparty: str | None
    account_code: str | None
    amount: Decimal
    group_key: str


@dataclass(frozen=True, slots=True)
class Plan:
    create: tuple[tuple[BankTransaction, ScoredCandidate], ...] = ()
    supersede: tuple[uuid.UUID, ...] = ()
    keep: tuple[uuid.UUID, ...] = ()


def certain_matches(
    transactions: Sequence[BankTransaction], documents: Sequence[MatchCandidate]
) -> dict[uuid.UUID, tuple[BankTransaction, ScoredCandidate]]:
    """The lines matching finds one certain document for - `suggest`'s HIGH results, which are
    already the unambiguous ones."""
    by_id = {t.id: t for t in transactions}
    return {
        transaction_id: (by_id[transaction_id], scored)
        for transaction_id, scored in suggest(transactions, documents).items()
        if scored.confidence is Confidence.HIGH
    }


def plan(
    certain: Mapping[uuid.UUID, tuple[BankTransaction, ScoredCandidate]],
    pending: Sequence[PendingProposal],
) -> Plan:
    """Pure. A pending proposal that matching still finds certain, for the same document, is kept
    as it is - re-running generation never churns rows. One whose line is no longer certain
    (reconciled some other way, now ambiguous, its document taken) or whose line is now certain
    for a DIFFERENT document is superseded; every certain line left without a pending proposal
    gets one."""
    keep: list[uuid.UUID] = []
    supersede: list[uuid.UUID] = []
    covered: set[uuid.UUID] = set()
    for proposal in pending:
        transaction_id = proposal.bank_transaction_id
        match = certain.get(transaction_id) if transaction_id is not None else None
        if (
            transaction_id is not None
            and match is not None
            and transaction_id not in covered
            and match[1].candidate.document_id == proposal.document_id
        ):
            keep.append(proposal.id)
            covered.add(transaction_id)
        else:
            supersede.append(proposal.id)
    create = tuple(match for tid, match in certain.items() if tid not in covered)
    return Plan(create=create, supersede=tuple(supersede), keep=tuple(keep))


def new_proposal(
    transaction: BankTransaction, scored: ScoredCandidate, target: TargetAccount | None
) -> NewProposal:
    candidate = scored.candidate
    counterparty = candidate.party_name or transaction.counterparty_name
    account_code = target.code if target is not None else None
    return NewProposal(
        bank_transaction_id=transaction.id,
        document_id=candidate.document_id,
        document_kind=candidate.kind,
        confidence=scored.confidence,
        counterparty=counterparty,
        account_code=account_code,
        amount=abs(transaction.amount),
        group_key=group_key(counterparty, account_code),
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class MatchingReads(Protocol):
    """The reads generation needs from api.bank.repository.SqlBankRepository."""

    async def unmatched_transactions(
        self, *, administration_id: uuid.UUID
    ) -> Sequence[BankTransaction]: ...

    async def open_invoice_balances(
        self, *, administration_id: uuid.UUID
    ) -> list[MatchCandidate]: ...

    async def bank_paid_expenses(
        self, *, administration_id: uuid.UUID, amount: Decimal | None = None
    ) -> list[MatchCandidate]: ...


class ProposalWrites(Protocol):
    def isolated(self) -> AbstractAsyncContextManager[object]:
        """A savepoint: whatever fails inside rolls back alone."""
        ...

    async def pending_for(self, *, administration_id: uuid.UUID) -> Sequence[PendingProposal]: ...

    async def target_accounts(
        self, *, administration_id: uuid.UUID, documents: Sequence[tuple[CandidateKind, uuid.UUID]]
    ) -> dict[uuid.UUID, TargetAccount]: ...

    async def insert(self, *, administration_id: uuid.UUID, proposal: NewProposal) -> bool: ...

    async def supersede(self, *, administration_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> int: ...

    async def supersede_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RefreshResult:
    created: int = 0
    superseded: int = 0
    kept: int = 0


class ProposalGenerator:
    def __init__(self, *, proposals: ProposalWrites, matching: MatchingReads) -> None:
        self._proposals = proposals
        self._matching = matching

    async def refresh(self, *, administration_id: uuid.UUID) -> RefreshResult:
        """Bring one administration's pending proposals in line with what matching finds certain
        now. Idempotent: run twice, the second run keeps everything and writes nothing."""
        transactions = await self._matching.unmatched_transactions(
            administration_id=administration_id
        )
        documents: list[MatchCandidate] = []
        if any(t.is_inflow for t in transactions):
            documents += await self._matching.open_invoice_balances(
                administration_id=administration_id
            )
        if any(not t.is_inflow for t in transactions):
            documents += await self._matching.bank_paid_expenses(
                administration_id=administration_id
            )
        certain = certain_matches(transactions, documents)
        pending = await self._proposals.pending_for(administration_id=administration_id)
        decided = plan(certain, pending)

        # Withdraw first: a line whose certain document changed has its old pending row
        # superseded before the new one is written, or 0079's one-pending-per-line index refuses.
        superseded = await self._proposals.supersede(
            administration_id=administration_id, ids=decided.supersede
        )
        targets = await self._proposals.target_accounts(
            administration_id=administration_id,
            documents=[(s.candidate.kind, s.candidate.document_id) for _, s in decided.create],
        )
        created = 0
        for transaction, scored in decided.create:
            proposal = new_proposal(transaction, scored, targets.get(scored.candidate.document_id))
            if await self._proposals.insert(administration_id=administration_id, proposal=proposal):
                created += 1
        return RefreshResult(created=created, superseded=superseded, kept=len(decided.keep))

    async def transaction_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> int:
        """The line was reconciled: its own pending proposal, and any other pending proposal for
        the document it settled, are no longer something to decide."""
        return await self._proposals.supersede_settled(
            administration_id=administration_id, transaction_id=transaction_id
        )


@dataclass
class ProposalHook:
    """What api.bank.service.BankService calls (its `ProposalHook` protocol). Proposals are
    advisory: generation runs in a savepoint and a failure is logged, never raised, so neither a
    statement import nor a reconciliation can be lost to it - including on a deployment where
    migration 0079 has not been applied yet (NFR-044)."""

    generator: ProposalGenerator
    proposals: ProposalWrites
    failures: list[str] = field(default_factory=list)

    async def refresh(self, *, administration_id: uuid.UUID) -> None:
        try:
            async with self.proposals.isolated():
                await self.generator.refresh(administration_id=administration_id)
        except Exception as exc:  # advisory: never fail the import that triggered it
            self.failures.append(type(exc).__name__)
            logger.warning(
                "booking proposals not refreshed for administration %s: %s",
                administration_id,
                type(exc).__name__,
            )

    async def transaction_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> None:
        try:
            async with self.proposals.isolated():
                await self.generator.transaction_settled(
                    administration_id=administration_id, transaction_id=transaction_id
                )
        except Exception as exc:  # advisory: never fail the reconciliation that triggered it
            self.failures.append(type(exc).__name__)
            logger.warning(
                "booking proposals not superseded for bank transaction %s: %s",
                transaction_id,
                type(exc).__name__,
            )

"""Reviewing booking proposals: the grouped review sheet, and approving or rejecting in bulk
(FR-BNK-004, FR-FRM-002, ADR-110).

--- Approving posts through the bank's own path, never a new one ---

An approval calls `BankService.reconcile_with_expense` or `reconcile_with_invoice` - exactly what a
person's click on the Bank screen calls - so the posting goes through LedgerService.post() or
SalesPaymentService.record() (CLAUDE.md non-negotiable #1) with every check those already make
(open period, bank journal, receipt still unmatched, invoice still owing). Rejecting changes only
the proposal. Nothing posted is ever flagged, edited or deleted here.

--- One administration at a time, even in a batch ---

The route resolves the caller's portfolio for `reconcile bank_transaction`
(api.firm.worklist_access.require_portfolio_permission); a proposal outside it is refused. Each
decision is then authorized again against ITS administration with the shared library's
`authorize()` - against current state, never once for the batch - and
runs in its own savepoint, so one refusal or failure is reported in `failed[]` and the rest still
commit. Every decision, successful or not, is audited individually.

--- Never twice (NFR-032) ---

A proposal is claimed (pending -> approved) BEFORE anything is posted, in the same savepoint as
the posting. Of two concurrent or retried decisions only one can claim it; the other finds it
already decided. A retried request whose decision already took effect is answered as that
decision again - "approved" - without posting a second time. The idempotency middleware replays a
retry carrying the same Idempotency-Key on top of this.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.bank.model import (
    BankError,
    BankTransaction,
    CandidateKind,
    ExpenseNotMatchable,
    InvalidBankField,
    NoActiveBankJournal,
    NoOpenPeriod,
    TransactionAlreadyReconciled,
    TransactionNotFound,
)
from api.firm.proposals import ProposalStatus
from api.firm.proposals_repository import ProposalRecord, ProposalRow

#: What each decision is authorized against, per administration: the Bank screen's own
#: "Reconcile bank" (Appendix A) - approving is reconciling, rejecting is declining to.
APPROVE_PERMISSION = ("reconcile", "bank_transaction")

#: One request decides at most this many proposals: a firm's whole week fits, a runaway client
#: does not hold one transaction open for minutes.
MAX_DECISIONS = 500


class Decision(enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"

    @property
    def status(self) -> ProposalStatus:
        return ProposalStatus.APPROVED if self is Decision.APPROVE else ProposalStatus.REJECTED


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    proposal_id: uuid.UUID
    decision: Decision
    #: "Always do this" (ADR-113): after the approval, keep a per-administration rule. Approve only.
    remember: bool = False


@dataclass(frozen=True, slots=True)
class Failure:
    proposal_id: uuid.UUID
    #: A stable machine code - the same `reason` the Bank screen's own refusals carry where the
    #: refusal is the same one - never a sentence (FR-UX-007).
    reason: str


@dataclass
class DecideOutcome:
    approved: int = 0
    rejected: int = 0
    failed: list[Failure] = field(default_factory=list)
    rules_created: int = 0


class RuleRemembering(Protocol):
    """api.firm.rules.RuleKeeper: "remember" after an approval. Advisory - never raises."""

    async def remember(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        counterparty: str | None,
        account_code: str | None,
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
    ) -> bool: ...


class ProposalStore(Protocol):
    def isolated(self) -> AbstractAsyncContextManager[object]: ...

    async def get(self, *, proposal_id: uuid.UUID) -> ProposalRecord | None: ...

    async def claim(
        self, *, proposal_id: uuid.UUID, status: ProposalStatus, user_id: uuid.UUID
    ) -> bool: ...

    async def pending_rows(
        self, *, administration_ids: Sequence[uuid.UUID]
    ) -> Sequence[ProposalRow]: ...


class BankReconciler(Protocol):
    """api.bank.service.BankService's two match-and-book entry points."""

    async def reconcile_with_expense(
        self,
        *,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        expense_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> BankTransaction: ...

    async def reconcile_with_invoice(
        self,
        *,
        administration_id: uuid.UUID,
        transaction_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> BankTransaction: ...


class _AlreadyDecided(Exception):
    pass


class _NotBookable(Exception):
    pass


def failure_reason(exc: Exception) -> str:
    """The Bank screen's own `reason` for the same refusal (api.bank.routes._refuse)."""
    if isinstance(exc, TransactionAlreadyReconciled):
        return "bank_transaction_already_reconciled"
    if isinstance(exc, TransactionNotFound):
        return "bank_transaction_not_found"
    if isinstance(exc, ExpenseNotMatchable):
        return "bank_expense_not_matchable"
    if isinstance(exc, NoOpenPeriod):
        return "period_invalid"
    if isinstance(exc, NoActiveBankJournal):
        return "bank_no_active_journal"
    if isinstance(exc, (InvalidBankField, BankError)):
        return "bank_field_invalid"
    if isinstance(exc, _NotBookable):
        return "proposal_not_bookable"
    return "posting_failed"


async def book(
    bank: BankReconciler,
    *,
    administration_id: uuid.UUID,
    bank_transaction_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    document_kind: CandidateKind | None,
    actor_user_id: uuid.UUID,
) -> None:
    """The approve path's posting step: what a click on the Bank screen calls. Shared by a
    person's approval and a rule's (ADR-113), so there is exactly one."""
    if bank_transaction_id is None or document_id is None:
        raise _NotBookable
    if document_kind is CandidateKind.EXPENSE:
        await bank.reconcile_with_expense(
            administration_id=administration_id,
            transaction_id=bank_transaction_id,
            expense_id=document_id,
            actor_user_id=actor_user_id,
        )
    elif document_kind is CandidateKind.SALES_INVOICE:
        await bank.reconcile_with_invoice(
            administration_id=administration_id,
            transaction_id=bank_transaction_id,
            invoice_id=document_id,
            actor_user_id=actor_user_id,
        )
    else:
        raise _NotBookable


# ---------------------------------------------------------------------------
# The review sheet
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalGroup:
    group_key: str
    counterparty: str | None
    account_code: str | None
    account_name: str | None
    proposals: tuple[ProposalRow, ...]

    @property
    def count(self) -> int:
        return len(self.proposals)

    @property
    def client_count(self) -> int:
        return len({p.administration_id for p in self.proposals})

    @property
    def total_amount(self) -> Decimal:
        return sum((p.amount for p in self.proposals), start=Decimal("0.00"))


def group(rows: Sequence[ProposalRow]) -> list[ProposalGroup]:
    """Pure. One group per group_key, the biggest first (the review sheet's "approve these 12 in
    one go" is worth most there), then by key for a stable order."""
    by_key: dict[str, list[ProposalRow]] = {}
    for row in rows:
        by_key.setdefault(row.group_key, []).append(row)
    groups = [
        ProposalGroup(
            group_key=key,
            counterparty=members[0].counterparty,
            account_code=members[0].account_code,
            account_name=next((m.account_name for m in members if m.account_name), None),
            proposals=tuple(members),
        )
        for key, members in by_key.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.group_key))
    return groups


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ProposalReviewService:
    def __init__(
        self,
        *,
        store: ProposalStore,
        authorization: AuthorizationService,
        bank: BankReconciler,
        audit_log: AuditLog,
        rules: RuleRemembering | None = None,
    ) -> None:
        self._store = store
        self._authorization = authorization
        self._bank = bank
        self._audit = audit_log
        self._rules = rules

    async def _allowed(
        self, user_id: uuid.UUID, administration_id: uuid.UUID, permission: tuple[str, str]
    ) -> tuple[bool, str]:
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        return decision.allowed, str(decision.reason)

    async def review(
        self, *, administration_ids: Sequence[uuid.UUID]
    ) -> tuple[int, list[ProposalGroup]]:
        """Pending proposals in `administration_ids`, grouped. Which administrations those are is
        the route's authorization answer - the portfolio from `require_portfolio_permission`
        (`view bank_transaction` per administration), or one administration `require_permission`
        already checked - never decided here."""
        rows = await self._store.pending_rows(administration_ids=administration_ids)
        return len(rows), group(rows)

    async def decide(
        self,
        *,
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
        decisions: Sequence[DecisionRequest],
        portfolio: frozenset[uuid.UUID],
    ) -> DecideOutcome:
        """`portfolio` is the administrations the route's `require_portfolio_permission` resolved
        for `reconcile bank_transaction`. A proposal outside it is refused before anything else;
        one inside it is still authorized again, per item, against current state.

        `acting_organization_id` is the request's tenant (the firm, for firm staff): the audit
        entries are written under it, like every other route's (audit_log's RLS only admits rows
        for the session's own organization - a firm deciding a client's proposal is the firm's
        act, recorded in the firm's trail with the client's administration_id)."""
        outcome = DecideOutcome()
        for request in decisions:
            reason, record = await self._decide_one(
                user_id, acting_organization_id, request, portfolio
            )
            if reason is not None:
                outcome.failed.append(Failure(request.proposal_id, reason))
                continue
            if request.decision is Decision.APPROVE:
                outcome.approved += 1
            else:
                outcome.rejected += 1
            if (
                request.remember
                and request.decision is Decision.APPROVE
                and record is not None
                and self._rules is not None
                and await self._rules.remember(
                    organization_id=record.organization_id,
                    administration_id=record.administration_id,
                    counterparty=record.counterparty,
                    account_code=record.account_code,
                    user_id=user_id,
                    acting_organization_id=acting_organization_id,
                )
            ):
                # ADR-113: one rule per administration, made only after this person's own
                # approval in it went through - authorized per item above.
                outcome.rules_created += 1
        return outcome

    async def _decide_one(
        self,
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
        request: DecisionRequest,
        portfolio: frozenset[uuid.UUID],
    ) -> tuple[str | None, ProposalRecord | None]:
        """(None, record) when the proposal now stands decided as asked; otherwise why not."""
        record = await self._store.get(proposal_id=request.proposal_id)
        if record is None:
            # Another tenant's proposal, or none at all - indistinguishable on purpose.
            return "proposal_not_found", None
        reason = await self._decide_found(
            user_id, acting_organization_id, request, portfolio, record
        )
        return reason, record

    async def _decide_found(
        self,
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
        request: DecisionRequest,
        portfolio: frozenset[uuid.UUID],
        record: ProposalRecord,
    ) -> str | None:
        """None when the proposal now stands decided as asked; otherwise why not."""

        async def audit(outcome: AuditOutcome, reason: str | None = None) -> None:
            await self._record(
                record, acting_organization_id, user_id, request.decision, outcome, reason
            )

        if record.administration_id not in portfolio:
            # Visible under the firm's RLS reach, but not one of this person's authorized
            # clients for reconciling.
            await audit(AuditOutcome.DENIED, "not_in_portfolio")
            return "not_permitted"

        allowed, why = await self._allowed(user_id, record.administration_id, APPROVE_PERMISSION)
        if not allowed:
            await audit(AuditOutcome.DENIED, why)
            return "not_permitted"

        wanted = request.decision.status
        if record.status is wanted:
            # A retry of a decision that already took effect: the same answer, nothing posted.
            return None
        if record.status is not ProposalStatus.PENDING:
            return "proposal_already_decided"

        try:
            async with self._store.isolated():
                if not await self._store.claim(
                    proposal_id=record.id, status=wanted, user_id=user_id
                ):
                    raise _AlreadyDecided
                if request.decision is Decision.APPROVE:
                    await self._book(record, user_id)
                await audit(AuditOutcome.SUCCESS)
        except _AlreadyDecided:
            # Decided concurrently between the read and the claim.
            now = await self._store.get(proposal_id=record.id)
            if now is not None and now.status is wanted:
                return None
            return "proposal_already_decided"
        except Exception as exc:  # reported per item, never a 500 for the batch
            reason = failure_reason(exc)
            await audit(AuditOutcome.FAILURE, reason)
            return reason
        return None

    async def _book(self, record: ProposalRecord, user_id: uuid.UUID) -> None:
        await book(
            self._bank,
            administration_id=record.administration_id,
            bank_transaction_id=record.bank_transaction_id,
            document_id=record.document_id,
            document_kind=record.document_kind,
            actor_user_id=user_id,
        )

    async def _record(
        self,
        record: ProposalRecord,
        acting_organization_id: uuid.UUID,
        user_id: uuid.UUID,
        decision: Decision,
        outcome: AuditOutcome,
        reason: str | None = None,
    ) -> None:
        detail: dict[str, str] = {"amount": str(record.amount)}
        if record.bank_transaction_id is not None:
            detail["bank_transaction_id"] = str(record.bank_transaction_id)
        if record.document_id is not None:
            detail["document_id"] = str(record.document_id)
        if reason is not None:
            detail["reason"] = reason
        await self._audit.record(
            AuditEvent(
                organization_id=acting_organization_id,
                administration_id=record.administration_id,
                # An approval books; a rejection is a decision that books nothing.
                category=(
                    AuditCategory.POSTING
                    if decision is Decision.APPROVE
                    else AuditCategory.APPROVAL
                ),
                action=f"{decision.value}_booking_proposal",
                resource_type="booking_proposal",
                resource_id=record.id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                detail=detail,
            )
        )

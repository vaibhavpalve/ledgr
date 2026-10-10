"""Approval rules: "always do this" for one client's certain matches (FR-BNK-004, FR-BNK-006,
ADR-113, docs/firm-home/contract-wave2.md decisions 1-4).

A rule belongs to ONE administration (FR-BNK-006: learning stays per tenant). It approves, without
waiting for a person, a pending HIGH booking proposal (ADR-110) whose normalised counterparty and
target account equal the rule's, and whose amount is at most `max_amount` when one is set. That is
the whole of what a rule can do: it never invents a booking without a document, and it never posts
through anything but the wave-1 approve path.

    rule_key()            pure: the (counterparty_key, account_code) a proposal would be ruled by
    rule_covers()         pure: does this active rule approve this proposal
    plan_auto_approvals() pure: which pending proposals to approve with which rule - each proposal,
                          and each bank line, at most once
    RuleKeeper            "remember": creates the rule after a person approves with
                          `remember: true`, or reactivates a suspended one
    RuleApplier           auto-approval at generation time: re-checks the rule creator's authority
                          against current state, suspends the rule if it is gone, otherwise
                          claims and books the proposal through the Bank screen's own path

--- Whose authority ---

The rule's creator approved the first booking themselves and asked for the rest to follow. At
apply time `authorize()` is asked again - the shared library, against current state - whether the
creator STILL holds `reconcile bank_transaction` on that administration. If not, nothing is booked:
the proposal stays pending for a person, and the rule is marked `suspended` with a reason. The
booking itself is made with the creator as the posting actor (the ledger and the payment service
make every check they make for a click), and the decision is audited as the SYSTEM, naming the rule
and its creator in `detail`.

--- Advisory ---

Rules never stand in the way. Creating one, and applying them, runs in its own savepoint; a failure
is logged and leaves the proposal pending, never fails the approval or the import that triggered it
(and keeps working on a deployment where migration 0082 is not applied yet - NFR-044).
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.bank.model import CandidateKind
from api.firm.proposals import normalise_counterparty
from api.firm.proposals_service import BankReconciler, book, failure_reason

logger = logging.getLogger(__name__)

#: What the creator must still hold, per administration, for their rule to book: the Bank
#: screen's own "Reconcile bank" - the same permission approving by hand takes (ADR-110).
RULE_PERMISSION = ("reconcile", "bank_transaction")

#: `suspended_reason` when the creator's authority is gone. A machine code (FR-UX-007).
CREATOR_NOT_AUTHORIZED = "creator_not_authorized"


class RuleStatus(enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class Rule:
    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    counterparty_key: str
    account_code: str
    max_amount: Decimal | None
    status: RuleStatus
    created_by_user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    """A pending proposal a rule might approve, as applying needs to know it."""

    proposal_id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    bank_transaction_id: uuid.UUID | None
    document_id: uuid.UUID | None
    document_kind: CandidateKind | None
    confidence: str
    counterparty: str | None
    account_code: str | None
    amount: Decimal


def rule_key(counterparty: str | None, account_code: str | None) -> tuple[str, str] | None:
    """The (counterparty_key, account_code) a rule for this proposal is keyed by - or None when
    either side is unknown, in which case no rule is ever made or applied."""
    key = normalise_counterparty(counterparty)
    code = (account_code or "").strip()
    if not key or not code:
        return None
    return key, code


def rule_covers(rule: Rule, candidate: RuleCandidate) -> bool:
    """Pure. Contract decision 2: only a single certain HIGH match of a bank line to an existing
    document, in the rule's own administration, with the rule's counterparty and account, at most
    `max_amount`."""
    if rule.status is not RuleStatus.ACTIVE:
        return False
    if rule.administration_id != candidate.administration_id:
        return False
    if candidate.confidence != "high":
        return False
    if (
        candidate.bank_transaction_id is None
        or candidate.document_id is None
        or candidate.document_kind is None
    ):
        return False
    if candidate.amount <= 0:
        return False
    if rule.max_amount is not None and candidate.amount > rule.max_amount:
        return False
    return rule_key(candidate.counterparty, candidate.account_code) == (
        rule.counterparty_key,
        rule.account_code,
    )


def plan_auto_approvals(
    rules: Sequence[Rule], candidates: Sequence[RuleCandidate]
) -> list[tuple[RuleCandidate, Rule]]:
    """Pure. Which candidates to approve, and by which rule. Each proposal and each bank line
    appears at most once - a line is never booked twice, whatever the input repeats."""
    planned: list[tuple[RuleCandidate, Rule]] = []
    seen_proposals: set[uuid.UUID] = set()
    seen_lines: set[uuid.UUID] = set()
    for candidate in candidates:
        if candidate.proposal_id in seen_proposals:
            continue
        if candidate.bank_transaction_id in seen_lines:
            continue
        rule = next((r for r in rules if rule_covers(r, candidate)), None)
        if rule is None:
            continue
        assert candidate.bank_transaction_id is not None  # rule_covers checked it
        seen_proposals.add(candidate.proposal_id)
        seen_lines.add(candidate.bank_transaction_id)
        planned.append((candidate, rule))
    return planned


# ---------------------------------------------------------------------------
# Storage and the bank, as rules need them
# ---------------------------------------------------------------------------


class RuleStore(Protocol):
    def isolated(self) -> AbstractAsyncContextManager[object]: ...

    async def live_rule(
        self, *, administration_id: uuid.UUID, counterparty_key: str, account_code: str
    ) -> Rule | None: ...

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        counterparty_key: str,
        account_code: str,
        user_id: uuid.UUID,
    ) -> Rule: ...

    async def reactivate(self, *, rule_id: uuid.UUID) -> bool: ...

    async def suspend(self, *, rule_id: uuid.UUID, reason: str) -> bool: ...

    async def retire(self, *, rule_id: uuid.UUID, user_id: uuid.UUID) -> bool: ...

    async def active_rules(self, *, administration_id: uuid.UUID) -> Sequence[Rule]: ...

    async def pending_candidates(
        self, *, administration_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
    ) -> Sequence[RuleCandidate]: ...

    async def claim_by_rule(self, *, proposal_id: uuid.UUID, rule_id: uuid.UUID) -> bool: ...


async def _creator_allowed(
    authorization: AuthorizationService, user_id: uuid.UUID, administration_id: uuid.UUID
) -> bool:
    action, resource_type = RULE_PERMISSION
    decision = await authorization.authorize(
        AuthorizationRequest(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            target=AdministrationScope(administration_id),
            attributes=ResourceAttributes(),
        )
    )
    return decision.allowed


# ---------------------------------------------------------------------------
# Remember
# ---------------------------------------------------------------------------


class RuleKeeper:
    """`remember: true` on an approval: the per-administration rule for that proposal's
    counterparty and account (contract decision 1)."""

    def __init__(self, *, store: RuleStore, audit_log: AuditLog) -> None:
        self._store = store
        self._audit = audit_log

    async def remember(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        counterparty: str | None,
        account_code: str | None,
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
    ) -> bool:
        """True when a rule was created or a suspended one reactivated; False when an active one
        already stood, the proposal cannot be keyed, or writing failed (logged, never raised).

        A suspended rule is reactivated only by its own creator: the creator's authority is what
        a rule books under, so a different person "remembering" retires the old rule and creates
        their own instead of lending their approval to someone else's rule."""
        key = rule_key(counterparty, account_code)
        if key is None:
            return False
        counterparty_key, code = key
        try:
            async with self._store.isolated():
                existing = await self._store.live_rule(
                    administration_id=administration_id,
                    counterparty_key=counterparty_key,
                    account_code=code,
                )
                if existing is not None and existing.status is RuleStatus.ACTIVE:
                    return False
                if existing is not None and existing.created_by_user_id == user_id:
                    await self._store.reactivate(rule_id=existing.id)
                    rule_id, action = existing.id, "reactivate_booking_rule"
                else:
                    if existing is not None:
                        await self._store.retire(rule_id=existing.id, user_id=user_id)
                    created = await self._store.create(
                        organization_id=organization_id,
                        administration_id=administration_id,
                        counterparty_key=counterparty_key,
                        account_code=code,
                        user_id=user_id,
                    )
                    rule_id, action = created.id, "create_booking_rule"
                await self._audit.record(
                    AuditEvent(
                        organization_id=acting_organization_id,
                        administration_id=administration_id,
                        category=AuditCategory.CONFIGURATION,
                        action=action,
                        resource_type="booking_rule",
                        resource_id=rule_id,
                        outcome=AuditOutcome.SUCCESS,
                        actor_type=ActorType.USER,
                        actor_user_id=user_id,
                        detail={"counterparty_key": counterparty_key, "account_code": code},
                    )
                )
                return True
        except Exception as exc:  # advisory: never fail the approval that asked for it
            logger.warning(
                "booking rule not remembered for administration %s: %s",
                administration_id,
                type(exc).__name__,
            )
            return False


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    approved: int = 0
    skipped: int = 0
    suspended: int = 0
    failed: int = 0


class RuleApplier:
    """Auto-approval at generation time (contract decision 3). What
    `api.firm.proposals.ProposalGenerator` calls with the lines it just proposed."""

    def __init__(
        self,
        *,
        store: RuleStore,
        authorization: AuthorizationService,
        bank: BankReconciler,
        audit_log: AuditLog,
    ) -> None:
        self._store = store
        self._authorization = authorization
        self._bank = bank
        self._audit = audit_log

    async def apply(
        self, *, administration_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
    ) -> ApplyResult:
        """Never raises: rules are advisory, and the caller is mid-import."""
        result = ApplyResult()
        if not transaction_ids:
            return result
        try:
            async with self._store.isolated():
                candidates = await self._store.pending_candidates(
                    administration_id=administration_id, transaction_ids=transaction_ids
                )
                if not candidates:
                    return result
                rules = await self._store.active_rules(administration_id=administration_id)
                planned = plan_auto_approvals(rules, candidates)
        except Exception as exc:
            logger.warning(
                "booking rules not read for administration %s: %s",
                administration_id,
                type(exc).__name__,
            )
            result.failed += 1
            return result

        authority: dict[uuid.UUID, bool] = {}
        for candidate, rule in planned:
            if rule.id not in authority:
                authority[rule.id] = await self._check_creator(rule)
                if not authority[rule.id]:
                    result.suspended += 1
            if not authority[rule.id]:
                result.skipped += 1
                continue
            if await self._approve(candidate, rule):
                result.approved += 1
            else:
                result.failed += 1
        return result

    async def _check_creator(self, rule: Rule) -> bool:
        """Against current state, every time. Denied -> the rule is suspended, with a reason."""
        if await _creator_allowed(
            self._authorization, rule.created_by_user_id, rule.administration_id
        ):
            return True
        try:
            async with self._store.isolated():
                if await self._store.suspend(rule_id=rule.id, reason=CREATOR_NOT_AUTHORIZED):
                    await self._audit.record(
                        AuditEvent(
                            organization_id=rule.organization_id,
                            administration_id=rule.administration_id,
                            category=AuditCategory.CONFIGURATION,
                            action="suspend_booking_rule",
                            resource_type="booking_rule",
                            resource_id=rule.id,
                            outcome=AuditOutcome.SUCCESS,
                            actor_type=ActorType.SYSTEM,
                            detail={
                                "reason": CREATOR_NOT_AUTHORIZED,
                                "rule_created_by": str(rule.created_by_user_id),
                            },
                        )
                    )
        except Exception as exc:
            logger.warning("booking rule %s not suspended: %s", rule.id, type(exc).__name__)
        return False

    async def _approve(self, candidate: RuleCandidate, rule: Rule) -> bool:
        """Claimed before it is booked, in one savepoint - ADR-110's never-twice discipline. A
        failed booking rolls the claim back: the proposal stays pending for a person."""
        try:
            async with self._store.isolated():
                if not await self._store.claim_by_rule(
                    proposal_id=candidate.proposal_id, rule_id=rule.id
                ):
                    return False
                # Exactly the wave-1 approve path: BankService reconcile -> ledger / payments.
                await book(
                    self._bank,
                    administration_id=candidate.administration_id,
                    bank_transaction_id=candidate.bank_transaction_id,
                    document_id=candidate.document_id,
                    document_kind=candidate.document_kind,
                    actor_user_id=rule.created_by_user_id,
                )
                await self._record(candidate, rule, AuditOutcome.SUCCESS)
        except Exception as exc:
            reason = failure_reason(exc)
            logger.warning(
                "booking rule %s did not approve proposal %s: %s",
                rule.id,
                candidate.proposal_id,
                reason,
            )
            try:
                await self._record(candidate, rule, AuditOutcome.FAILURE, reason)
            except Exception:  # the audit of a failure must not become the failure
                logger.warning("auto-approval failure not audited for %s", candidate.proposal_id)
            return False
        return True

    async def _record(
        self,
        candidate: RuleCandidate,
        rule: Rule,
        outcome: AuditOutcome,
        reason: str | None = None,
    ) -> None:
        detail: dict[str, str] = {
            "amount": str(candidate.amount),
            "rule_id": str(rule.id),
            "rule_created_by": str(rule.created_by_user_id),
        }
        if candidate.bank_transaction_id is not None:
            detail["bank_transaction_id"] = str(candidate.bank_transaction_id)
        if candidate.document_id is not None:
            detail["document_id"] = str(candidate.document_id)
        if reason is not None:
            detail["reason"] = reason
        await self._audit.record(
            AuditEvent(
                # The client's own chain (ADR-112 admits it from a firm session too).
                organization_id=candidate.organization_id,
                administration_id=candidate.administration_id,
                category=AuditCategory.POSTING,
                action="approve_booking_proposal",
                resource_type="booking_proposal",
                resource_id=candidate.proposal_id,
                outcome=outcome,
                actor_type=ActorType.SYSTEM,
                detail=detail,
            )
        )

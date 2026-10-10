"""Approval rules without a database (ADR-113, contract-wave2 decisions 1-4).

The pure core (`rule_key`, `rule_covers`, `plan_auto_approvals`) and the two services
(`RuleKeeper` for "remember", `RuleApplier` for auto-approval) against in-memory fakes. The
DB-backed proof - real RLS, real postings, 0082's trigger - is
tests/integration/test_booking_rules.py.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditOutcome
from api.authz.model import AuthorizationDecision
from api.bank.model import CandidateKind, ExpenseNotMatchable
from api.firm.proposals import ProposalGenerator, ProposalStatus
from api.firm.proposals_repository import ProposalRecord
from api.firm.proposals_service import Decision, DecisionRequest, ProposalReviewService
from api.firm.rules import (
    CREATOR_NOT_AUTHORIZED,
    Rule,
    RuleApplier,
    RuleCandidate,
    RuleKeeper,
    RuleStatus,
    plan_auto_approvals,
    rule_covers,
    rule_key,
)
from api.firm.rules_routes import parse_max_amount
from tests.firm.test_proposals import FakeMatching, FakeProposals, line, receipt

ADMIN = uuid.uuid4()
OTHER_ADMIN = uuid.uuid4()
CLIENT_ORG = uuid.uuid4()
FIRM_ORG = uuid.uuid4()
CREATOR = uuid.uuid4()
SOMEONE_ELSE = uuid.uuid4()


def _rule(**overrides: Any) -> Rule:
    base = Rule(
        id=uuid.uuid4(),
        organization_id=CLIENT_ORG,
        administration_id=ADMIN,
        counterparty_key="kpn",
        account_code="4500",
        max_amount=None,
        status=RuleStatus.ACTIVE,
        created_by_user_id=CREATOR,
    )
    return replace(base, **overrides)


def _candidate(**overrides: Any) -> RuleCandidate:
    base = RuleCandidate(
        proposal_id=uuid.uuid4(),
        organization_id=CLIENT_ORG,
        administration_id=ADMIN,
        bank_transaction_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_kind=CandidateKind.EXPENSE,
        confidence="high",
        counterparty="KPN B.V.",
        account_code="4500",
        amount=Decimal("70.27"),
    )
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# The pure core
# ---------------------------------------------------------------------------


def test_rule_key_is_the_normalised_counterparty_and_the_account() -> None:
    assert rule_key("KPN B.V.", "4500") == ("kpn", "4500")
    assert rule_key("kpn bv", " 4500 ") == ("kpn", "4500")
    assert rule_key(None, "4500") is None
    assert rule_key("B.V.", "4500") is None  # nothing but a legal form
    assert rule_key("KPN", None) is None


def test_a_rule_covers_only_a_certain_documented_match_of_its_own_key() -> None:
    rule = _rule()
    assert rule_covers(rule, _candidate())
    assert not rule_covers(rule, _candidate(confidence="medium"))
    assert not rule_covers(rule, _candidate(document_id=None))
    assert not rule_covers(rule, _candidate(document_kind=None))
    assert not rule_covers(rule, _candidate(bank_transaction_id=None))
    assert not rule_covers(rule, _candidate(counterparty="Vodafone"))
    assert not rule_covers(rule, _candidate(account_code="4600"))
    assert not rule_covers(replace(rule, status=RuleStatus.SUSPENDED), _candidate())
    assert not rule_covers(replace(rule, status=RuleStatus.RETIRED), _candidate())


def test_a_rule_of_one_administration_never_covers_another() -> None:
    """FR-BNK-006: same counterparty, same account, same firm - another client is untouched."""
    assert not rule_covers(_rule(), _candidate(administration_id=OTHER_ADMIN))
    assert plan_auto_approvals([_rule()], [_candidate(administration_id=OTHER_ADMIN)]) == []


def test_max_amount_is_inclusive_and_above_it_stays_pending() -> None:
    rule = _rule(max_amount=Decimal("70.27"))
    assert rule_covers(rule, _candidate(amount=Decimal("70.27")))
    assert not rule_covers(rule, _candidate(amount=Decimal("70.28")))


def test_one_line_is_planned_once_even_when_repeated() -> None:
    first = _candidate()
    again = replace(first, proposal_id=uuid.uuid4())  # a second proposal for the same line
    planned = plan_auto_approvals([_rule()], [first, first, again])
    assert [c.proposal_id for c, _ in planned] == [first.proposal_id]


_amounts = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("5000.00"), places=2, allow_nan=False
)


@st.composite
def _worlds(draw: st.DrawFn) -> tuple[list[Rule], list[RuleCandidate]]:
    admins = [ADMIN, OTHER_ADMIN]
    keys = ["kpn", "staples", "shell"]
    codes = ["4500", "4100"]
    rules = [
        _rule(
            administration_id=draw(st.sampled_from(admins)),
            counterparty_key=draw(st.sampled_from(keys)),
            account_code=draw(st.sampled_from(codes)),
            max_amount=draw(st.none() | _amounts),
            status=draw(st.sampled_from(list(RuleStatus))),
        )
        for _ in range(draw(st.integers(0, 6)))
    ]
    lines = [uuid.uuid4() for _ in range(draw(st.integers(1, 6)))]
    candidates = [
        _candidate(
            administration_id=draw(st.sampled_from(admins)),
            bank_transaction_id=draw(st.sampled_from(lines)),
            counterparty=draw(st.sampled_from(["KPN B.V.", "Staples", "Shell", "Esso"])),
            account_code=draw(st.sampled_from(codes)),
            confidence=draw(st.sampled_from(["high", "medium"])),
            amount=draw(_amounts),
        )
        for _ in range(draw(st.integers(0, 12)))
    ]
    return rules, candidates


@settings(max_examples=300, deadline=None)
@given(_worlds())
def test_a_rule_never_posts_a_line_twice_or_above_its_cap(
    world: tuple[list[Rule], list[RuleCandidate]],
) -> None:
    rules, candidates = world
    planned = plan_auto_approvals(rules, candidates)
    lines = [c.bank_transaction_id for c, _ in planned]
    assert len(lines) == len(set(lines)), "a bank line planned twice"
    assert len({c.proposal_id for c, _ in planned}) == len(planned)
    for candidate, rule in planned:
        assert rule.status is RuleStatus.ACTIVE
        assert rule.administration_id == candidate.administration_id
        assert candidate.confidence == "high"
        if rule.max_amount is not None:
            assert candidate.amount <= rule.max_amount, "booked above max_amount"
        assert rule_key(candidate.counterparty, candidate.account_code) == (
            rule.counterparty_key,
            rule.account_code,
        )


def test_max_amount_parsing_is_decimal_and_strict() -> None:
    assert parse_max_amount(None) is None
    assert parse_max_amount("250") == Decimal("250.00")
    assert parse_max_amount("250.5") == Decimal("250.50")
    for bad in ("0", "-1", "1.001", "abc", "NaN", "Infinity", "1e30", ""):
        try:
            parse_max_amount(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} accepted")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _StoredCandidate:
    candidate: RuleCandidate
    status: ProposalStatus = ProposalStatus.PENDING
    rule_id: uuid.UUID | None = None


@dataclass
class FakeRuleStore:
    """In-memory booking_rule plus the pending proposals rules look at. `isolated()` restores
    everything on failure, as a savepoint does."""

    rules: dict[uuid.UUID, Rule] = field(default_factory=dict)
    reasons: dict[uuid.UUID, str] = field(default_factory=dict)
    proposals: dict[uuid.UUID, _StoredCandidate] = field(default_factory=dict)
    fail_reads: bool = False

    @asynccontextmanager
    async def isolated(self) -> AsyncIterator[object]:
        snapshot = (
            copy.deepcopy(self.rules),
            copy.deepcopy(self.reasons),
            copy.deepcopy(self.proposals),
        )
        try:
            yield None
        except BaseException:
            self.rules, self.reasons, self.proposals = snapshot
            raise

    def add_rule(self, rule: Rule) -> Rule:
        self.rules[rule.id] = rule
        return rule

    def add_candidate(self, candidate: RuleCandidate) -> RuleCandidate:
        self.proposals[candidate.proposal_id] = _StoredCandidate(candidate)
        return candidate

    async def live_rule(
        self, *, administration_id: uuid.UUID, counterparty_key: str, account_code: str
    ) -> Rule | None:
        return next(
            (
                r
                for r in self.rules.values()
                if r.administration_id == administration_id
                and r.counterparty_key == counterparty_key
                and r.account_code == account_code
                and r.status is not RuleStatus.RETIRED
            ),
            None,
        )

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        counterparty_key: str,
        account_code: str,
        user_id: uuid.UUID,
    ) -> Rule:
        assert (
            await self.live_rule(
                administration_id=administration_id,
                counterparty_key=counterparty_key,
                account_code=account_code,
            )
            is None
        ), "0082's partial unique index"
        return self.add_rule(
            _rule(
                organization_id=organization_id,
                administration_id=administration_id,
                counterparty_key=counterparty_key,
                account_code=account_code,
                created_by_user_id=user_id,
            )
        )

    def _set(self, rule_id: uuid.UUID, status: RuleStatus, allowed_from: set[RuleStatus]) -> bool:
        rule = self.rules[rule_id]
        if rule.status not in allowed_from:
            return False
        self.rules[rule_id] = replace(rule, status=status)
        return True

    async def reactivate(self, *, rule_id: uuid.UUID) -> bool:
        self.reasons.pop(rule_id, None)
        return self._set(rule_id, RuleStatus.ACTIVE, {RuleStatus.SUSPENDED})

    async def suspend(self, *, rule_id: uuid.UUID, reason: str) -> bool:
        moved = self._set(rule_id, RuleStatus.SUSPENDED, {RuleStatus.ACTIVE})
        if moved:
            self.reasons[rule_id] = reason
        return moved

    async def retire(self, *, rule_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return self._set(rule_id, RuleStatus.RETIRED, {RuleStatus.ACTIVE, RuleStatus.SUSPENDED})

    async def active_rules(self, *, administration_id: uuid.UUID) -> Sequence[Rule]:
        if self.fail_reads:
            raise RuntimeError("relation booking_rule does not exist")
        return [
            r
            for r in self.rules.values()
            if r.administration_id == administration_id and r.status is RuleStatus.ACTIVE
        ]

    async def pending_candidates(
        self, *, administration_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
    ) -> Sequence[RuleCandidate]:
        return [
            s.candidate
            for s in self.proposals.values()
            if s.candidate.administration_id == administration_id
            and s.status is ProposalStatus.PENDING
            and s.candidate.bank_transaction_id in transaction_ids
        ]

    async def claim_by_rule(self, *, proposal_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        stored = self.proposals.get(proposal_id)
        if stored is None or stored.status is not ProposalStatus.PENDING:
            return False
        stored.status = ProposalStatus.APPROVED
        stored.rule_id = rule_id
        return True


class FakeAuthorization:
    def __init__(self, allowed: set[tuple[uuid.UUID, uuid.UUID]]) -> None:
        self.allowed = allowed
        self.asked: list[tuple[uuid.UUID, str, str, uuid.UUID]] = []

    async def authorize(self, request: Any) -> AuthorizationDecision:
        administration_id = request.target.administration_id
        self.asked.append(
            (request.user_id, request.action, request.resource_type, administration_id)
        )
        if (request.user_id, administration_id) in self.allowed:
            return AuthorizationDecision(allowed=True, reason="allowed")
        return AuthorizationDecision(allowed=False, reason="no_matching_grant")


@dataclass
class FakeBank:
    posted: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)  # (line, actor)
    refuse: bool = False

    async def _post(self, transaction_id: uuid.UUID, actor_user_id: uuid.UUID) -> None:
        if self.refuse:
            raise ExpenseNotMatchable("the receipt is already matched to another bank line")
        self.posted.append((transaction_id, actor_user_id))

    async def reconcile_with_expense(
        self, *, transaction_id: uuid.UUID, actor_user_id: uuid.UUID, **_: Any
    ) -> None:
        await self._post(transaction_id, actor_user_id)

    async def reconcile_with_invoice(
        self, *, transaction_id: uuid.UUID, actor_user_id: uuid.UUID, **_: Any
    ) -> None:
        await self._post(transaction_id, actor_user_id)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def _applier(
    store: FakeRuleStore, allowed: set[tuple[uuid.UUID, uuid.UUID]] | None = None
) -> tuple[RuleApplier, FakeBank, FakeAudit, FakeAuthorization]:
    bank = FakeBank()
    audit = FakeAudit()
    authorization = FakeAuthorization(allowed if allowed is not None else {(CREATOR, ADMIN)})
    applier = RuleApplier(
        store=store,
        authorization=authorization,  # type: ignore[arg-type]
        bank=bank,  # type: ignore[arg-type]
        audit_log=audit,  # type: ignore[arg-type]
    )
    return applier, bank, audit, authorization


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


async def test_an_active_rule_approves_through_the_bank_as_the_system() -> None:
    store = FakeRuleStore()
    rule = store.add_rule(_rule())
    candidate = store.add_candidate(_candidate())
    applier, bank, audit, authorization = _applier(store)

    result = await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[candidate.bank_transaction_id],  # type: ignore[list-item]
    )

    assert result.approved == 1
    # Booked once, by the bank path, with the creator as the posting actor.
    assert bank.posted == [(candidate.bank_transaction_id, CREATOR)]
    stored = store.proposals[candidate.proposal_id]
    assert stored.status is ProposalStatus.APPROVED and stored.rule_id == rule.id
    # The creator's authority was asked for, against this administration.
    assert authorization.asked == [(CREATOR, "reconcile", "bank_transaction", ADMIN)]
    [event] = audit.events
    assert event.action == "approve_booking_proposal"
    assert event.actor_type is ActorType.SYSTEM and event.actor_user_id is None
    assert event.category is AuditCategory.POSTING
    assert event.outcome is AuditOutcome.SUCCESS
    assert event.organization_id == CLIENT_ORG and event.administration_id == ADMIN
    assert event.detail["rule_id"] == str(rule.id)
    assert event.detail["rule_created_by"] == str(CREATOR)


async def test_applying_twice_books_once() -> None:
    store = FakeRuleStore()
    store.add_rule(_rule())
    candidate = store.add_candidate(_candidate())
    applier, bank, _, _ = _applier(store)
    lines = [candidate.bank_transaction_id]
    await applier.apply(administration_id=ADMIN, transaction_ids=lines)  # type: ignore[arg-type]
    again = await applier.apply(administration_id=ADMIN, transaction_ids=lines)  # type: ignore[arg-type]
    assert again.approved == 0
    assert len(bank.posted) == 1


async def test_a_creator_without_authority_suspends_the_rule_and_books_nothing() -> None:
    store = FakeRuleStore()
    rule = store.add_rule(_rule())
    first = store.add_candidate(_candidate())
    second = store.add_candidate(_candidate(amount=Decimal("12.00")))
    applier, bank, audit, authorization = _applier(store, allowed=set())

    result = await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[first.bank_transaction_id, second.bank_transaction_id],  # type: ignore[list-item]
    )

    assert (result.approved, result.suspended, result.skipped) == (0, 1, 2)
    assert bank.posted == []
    assert store.rules[rule.id].status is RuleStatus.SUSPENDED
    assert store.reasons[rule.id] == CREATOR_NOT_AUTHORIZED
    assert all(s.status is ProposalStatus.PENDING for s in store.proposals.values())
    assert len(authorization.asked) == 1  # once per rule, per run
    [event] = audit.events
    assert event.action == "suspend_booking_rule"
    assert event.actor_type is ActorType.SYSTEM
    assert event.detail["reason"] == CREATOR_NOT_AUTHORIZED


async def test_a_failed_booking_leaves_the_proposal_pending_and_is_audited() -> None:
    store = FakeRuleStore()
    store.add_rule(_rule())
    candidate = store.add_candidate(_candidate())
    applier, bank, audit, _ = _applier(store)
    bank.refuse = True

    result = await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[candidate.bank_transaction_id],  # type: ignore[list-item]
    )

    assert (result.approved, result.failed) == (0, 1)
    stored = store.proposals[candidate.proposal_id]
    assert stored.status is ProposalStatus.PENDING and stored.rule_id is None
    [event] = audit.events
    assert event.outcome is AuditOutcome.FAILURE
    assert event.detail["reason"] == "bank_expense_not_matchable"


async def test_above_max_amount_stays_pending() -> None:
    store = FakeRuleStore()
    store.add_rule(_rule(max_amount=Decimal("50.00")))
    candidate = store.add_candidate(_candidate(amount=Decimal("70.27")))
    applier, bank, _, _ = _applier(store)
    await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[candidate.bank_transaction_id],  # type: ignore[list-item]
    )
    assert bank.posted == []
    assert store.proposals[candidate.proposal_id].status is ProposalStatus.PENDING


async def test_another_administrations_rule_is_never_applied() -> None:
    store = FakeRuleStore()
    store.add_rule(_rule(administration_id=OTHER_ADMIN))
    candidate = store.add_candidate(_candidate())
    applier, bank, _, _ = _applier(store, allowed={(CREATOR, ADMIN), (CREATOR, OTHER_ADMIN)})
    await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[candidate.bank_transaction_id],  # type: ignore[list-item]
    )
    assert bank.posted == []


async def test_a_read_failure_is_swallowed() -> None:
    store = FakeRuleStore(fail_reads=True)
    candidate = store.add_candidate(_candidate())
    applier, bank, _, _ = _applier(store)
    result = await applier.apply(
        administration_id=ADMIN,
        transaction_ids=[candidate.bank_transaction_id],  # type: ignore[list-item]
    )
    assert (result.approved, result.failed) == (0, 1)
    assert bank.posted == []


# ---------------------------------------------------------------------------
# Remember
# ---------------------------------------------------------------------------


def _keeper(store: FakeRuleStore) -> tuple[RuleKeeper, FakeAudit]:
    audit = FakeAudit()
    return RuleKeeper(store=store, audit_log=audit), audit  # type: ignore[arg-type]


async def _remember(keeper: RuleKeeper, user: uuid.UUID = CREATOR, **overrides: Any) -> bool:
    arguments: dict[str, Any] = {
        "organization_id": CLIENT_ORG,
        "administration_id": ADMIN,
        "counterparty": "KPN B.V.",
        "account_code": "4500",
        "user_id": user,
        "acting_organization_id": FIRM_ORG,
    }
    arguments.update(overrides)
    return await keeper.remember(**arguments)


async def test_remember_creates_one_rule_per_administration() -> None:
    store = FakeRuleStore()
    keeper, audit = _keeper(store)
    assert await _remember(keeper)
    assert await _remember(keeper, administration_id=OTHER_ADMIN)
    assert not await _remember(keeper)  # already active: nothing new
    assert sorted(
        (r.administration_id, r.counterparty_key) for r in store.rules.values()
    ) == sorted([(ADMIN, "kpn"), (OTHER_ADMIN, "kpn")])
    assert [e.action for e in audit.events] == ["create_booking_rule"] * 2
    assert all(e.organization_id == FIRM_ORG for e in audit.events)
    assert all(r.organization_id == CLIENT_ORG for r in store.rules.values())


async def test_remember_reactivates_the_creators_suspended_rule() -> None:
    store = FakeRuleStore()
    rule = store.add_rule(_rule(status=RuleStatus.SUSPENDED))
    store.reasons[rule.id] = CREATOR_NOT_AUTHORIZED
    keeper, audit = _keeper(store)
    assert await _remember(keeper)
    assert store.rules[rule.id].status is RuleStatus.ACTIVE
    assert len(store.rules) == 1
    assert [e.action for e in audit.events] == ["reactivate_booking_rule"]


async def test_someone_else_remembering_replaces_a_suspended_rule_with_their_own() -> None:
    store = FakeRuleStore()
    old = store.add_rule(_rule(status=RuleStatus.SUSPENDED))
    keeper, _ = _keeper(store)
    assert await _remember(keeper, user=SOMEONE_ELSE)
    assert store.rules[old.id].status is RuleStatus.RETIRED
    [new] = [r for r in store.rules.values() if r.status is RuleStatus.ACTIVE]
    assert new.created_by_user_id == SOMEONE_ELSE


async def test_remember_without_a_key_makes_nothing() -> None:
    store = FakeRuleStore()
    keeper, _ = _keeper(store)
    assert not await _remember(keeper, account_code=None)
    assert not await _remember(keeper, counterparty=None)
    assert store.rules == {}


# ---------------------------------------------------------------------------
# Wiring: decide(remember) and the generator
# ---------------------------------------------------------------------------


class _RecordingKeeper:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def remember(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


class _OneProposal:
    """Enough of a ProposalStore for decide()."""

    def __init__(self, record: ProposalRecord) -> None:
        self.record = record

    @asynccontextmanager
    async def isolated(self) -> AsyncIterator[object]:
        yield None

    async def get(self, *, proposal_id: uuid.UUID) -> ProposalRecord | None:
        return self.record if proposal_id == self.record.id else None

    async def claim(
        self, *, proposal_id: uuid.UUID, status: ProposalStatus, user_id: uuid.UUID
    ) -> bool:
        if self.record.status is not ProposalStatus.PENDING:
            return False
        self.record = replace(self.record, status=status)
        return True

    async def pending_rows(self, *, administration_ids: Sequence[uuid.UUID]) -> list[Any]:
        return []


def _record(**overrides: Any) -> ProposalRecord:
    base = ProposalRecord(
        id=uuid.uuid4(),
        organization_id=CLIENT_ORG,
        administration_id=ADMIN,
        bank_transaction_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_kind=CandidateKind.EXPENSE,
        status=ProposalStatus.PENDING,
        amount=Decimal("70.27"),
        counterparty="KPN B.V.",
        account_code="4500",
    )
    return replace(base, **overrides)


async def _decide(
    record: ProposalRecord, request: DecisionRequest, *, refuse: bool = False
) -> tuple[Any, _RecordingKeeper]:
    keeper = _RecordingKeeper()
    bank = FakeBank(refuse=refuse)
    service = ProposalReviewService(
        store=_OneProposal(record),
        authorization=FakeAuthorization({(CREATOR, ADMIN)}),  # type: ignore[arg-type]
        bank=bank,  # type: ignore[arg-type]
        audit_log=FakeAudit(),  # type: ignore[arg-type]
        rules=keeper,
    )
    outcome = await service.decide(
        user_id=CREATOR,
        acting_organization_id=FIRM_ORG,
        decisions=[request],
        portfolio=frozenset({ADMIN}),
    )
    return outcome, keeper


async def test_remember_on_an_approval_creates_the_rule_for_that_administration() -> None:
    record = _record()
    outcome, keeper = await _decide(record, DecisionRequest(record.id, Decision.APPROVE, True))
    assert (outcome.approved, outcome.rules_created) == (1, 1)
    [call] = keeper.calls
    assert call["administration_id"] == ADMIN and call["organization_id"] == CLIENT_ORG
    assert (call["counterparty"], call["account_code"]) == ("KPN B.V.", "4500")
    assert call["acting_organization_id"] == FIRM_ORG


async def test_no_rule_without_remember_or_when_the_approval_failed() -> None:
    record = _record()
    outcome, keeper = await _decide(record, DecisionRequest(record.id, Decision.APPROVE))
    assert (outcome.approved, outcome.rules_created, keeper.calls) == (1, 0, [])

    record = _record()
    outcome, keeper = await _decide(
        record, DecisionRequest(record.id, Decision.APPROVE, True), refuse=True
    )
    assert (outcome.approved, outcome.rules_created, keeper.calls) == (0, 0, [])
    assert outcome.failed[0].reason == "bank_expense_not_matchable"

    record = _record()
    outcome, keeper = await _decide(record, DecisionRequest(record.id, Decision.REJECT, True))
    assert (outcome.rejected, outcome.rules_created, keeper.calls) == (1, 0, [])


class _RecordingApprover:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, list[uuid.UUID]]] = []

    @dataclass
    class _Result:
        approved: int

    async def apply(
        self, *, administration_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
    ) -> Any:
        self.calls.append((administration_id, list(transaction_ids)))
        return self._Result(approved=len(transaction_ids))


async def test_the_generator_hands_only_newly_proposed_lines_to_the_rules() -> None:
    kpn = line("-70.27", "KPN B.V.")
    matching = FakeMatching([kpn], [receipt("KPN", "70.27")])
    proposals = FakeProposals()
    approver = _RecordingApprover()
    generator = ProposalGenerator(proposals=proposals, matching=matching, auto_approve=approver)

    first = await generator.refresh(administration_id=ADMIN)
    assert (first.created, first.auto_approved) == (1, 1)
    assert approver.calls == [(ADMIN, [kpn.id])]

    # Nothing new on the second run (the pending row is kept): the rules are not asked again.
    second = await generator.refresh(administration_id=ADMIN)
    assert (second.created, second.auto_approved) == (0, 0)
    assert len(approver.calls) == 1

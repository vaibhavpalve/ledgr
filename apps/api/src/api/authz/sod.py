"""Segregation of duties: IAM-060 through IAM-065 (PRD §8.7).

    IAM-060  the creator or editor of a purchase invoice cannot be its SOLE
             approver
    IAM-061  the approver of a payment batch cannot release it, WHERE the
             organization has more than two active users
    IAM-062  the submitter of an expense cannot approve it
    IAM-063  a user cannot grant themselves a permission they do not hold,
             nor approve their own access request
    IAM-064  rules are configurable per organization, but each deviation is
             explicitly acknowledged by the Owner, recorded with a reason,
             and appears in the audit report
    IAM-065  single-user organizations are exempt by necessity, and the
             exemption is disclosed in the audit report rather than hidden

--- Why this lives inside api.authz ---

SoD answers a different question from authorize() - not "may this user do X"
but "may THIS user do X to THIS record, given who did the earlier steps" -
and it needs facts authorize() never sees: who created the invoice, who
approved the batch. It is nonetheless an access decision, so it lives in the
authorization package rather than becoming the second authorization system
CLAUDE.md's third non-negotiable forbids. The two compose: a caller needs
authorize() to say the actor holds the permission AND check() to say duties
are properly segregated. Neither substitutes for the other.

--- Two words that carry the whole design ---

IAM-060 says "sole", and IAM-062 does not. A creator may therefore approve
their own invoice when a second approval is still required, and an expense
submitter may never approve their own expense. Those are different rules,
and _creator_not_sole_approver / _submitter_not_approver below are
deliberately not factored into one "actor did an earlier step" check that
would silently make one of them wrong.

IAM-061 says "where the organization has more than two active users", which
is a second, narrower exemption than IAM-065's single-user one, with its own
threshold. Both are computed from the same live count and both are disclosed
in the report.

--- Integration status ---

Purchase invoices (FR-AP), payment batches and expenses (FR-EXP) do not
exist in this codebase. This module is the enforcement point those modules
will call, with DutyContext carrying the record facts each rule needs. The
one rule with a live caller today is NO_SELF_GRANT, which
api.authz.service.AuthorizationService.assign_role consults so that a
refused self-grant is RECORDED, not merely refused - the ceiling check in
assign_role (ADR-013) already prevented it, but silently.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from api.audit.log import AuditOutcome
from api.audit.trail import AuditTrail


class SodRule(enum.Enum):
    """The requirement each rule implements is part of its identity - the
    value is what lands in sod_event.rule and in the audit report, so a
    reviewer reading a row can find the requirement without a lookup table.
    """

    CREATOR_NOT_SOLE_APPROVER = "iam_060_creator_not_sole_approver"
    APPROVER_NOT_RELEASER = "iam_061_approver_not_releaser"
    SUBMITTER_NOT_APPROVER = "iam_062_submitter_not_approver"
    NO_SELF_GRANT = "iam_063_no_self_grant"
    NO_SELF_APPROVED_ACCESS_REQUEST = "iam_063_no_self_approved_access_request"


# IAM-064 makes SoD rules configurable per organization. The two IAM-063
# rules are deliberately excluded from that, because a deviation on them
# would defeat IAM-036 ("no privilege escalation by role authoring"), an
# unqualified M requirement with no deviation clause of its own: an
# Organization Admin with a self-grant deviation could grant themselves
# anything, which is precisely what IAM-036 forbids. Where two M
# requirements collide, the reading that keeps both intact wins - a
# deviation on the OTHER three rules costs an organization a control it
# chose to give up, while a deviation on these would silently undo a
# guarantee the rest of the system is built on.
#
# This is an interpretation, not something §8.7 states. Making one of these
# deviable is a one-line change here, deliberately, so it can be reversed
# cheaply if the intended reading is the literal one.
DEVIABLE_RULES: frozenset[SodRule] = frozenset(
    {
        SodRule.CREATOR_NOT_SOLE_APPROVER,
        SodRule.APPROVER_NOT_RELEASER,
        SodRule.SUBMITTER_NOT_APPROVER,
    }
)

# IAM-061's own threshold: the rule applies only "where the organization has
# more than two active users."
APPROVER_RELEASER_MIN_USERS = 3

# IAM-065: at or below this many active users, SoD cannot be satisfied by
# anyone and is exempt by necessity.
SINGLE_USER_THRESHOLD = 1

SodOutcome = Literal[
    "permitted",
    "blocked",
    "deviation_applied",
    "single_user_exempt",
    "two_user_exempt",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DutyContext:
    """Everything the rules need about one attempted action.

    The actor-history fields default to empty, and a rule whose facts are
    absent does NOT fire. That is safe here, unlike in authorization: a rule
    is a prohibition, so an absent fact means "no conflicting earlier step is
    known", and the action still has to pass authorize() to happen at all.
    A caller that forgets to populate created_by is not bypassing a grant,
    it is failing to declare a conflict - which is why
    tests/authz/test_sod.py asserts every call site's context shape and why
    the future FR-AP/FR-EXP modules must build this from stored records
    rather than from request input.
    """

    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    action: str
    resource_type: str
    resource_id: uuid.UUID | None = None

    created_by: uuid.UUID | None = None
    edited_by: tuple[uuid.UUID, ...] = ()
    submitted_by: uuid.UUID | None = None
    approved_by: tuple[uuid.UUID, ...] = ()
    required_approvals: int = 1

    # IAM-063: who the grant or access request is about.
    subject_user_id: uuid.UUID | None = None
    permissions_not_held: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SodDecision:
    permitted: bool
    outcome: SodOutcome
    rule: SodRule | None = None
    detail: str = ""
    deviation_id: uuid.UUID | None = None

    @property
    def refused(self) -> bool:
        return not self.permitted


@dataclass(frozen=True, slots=True)
class Deviation:
    id: uuid.UUID
    organization_id: uuid.UUID
    rule: SodRule
    acknowledged_by_user_id: uuid.UUID
    reason: str
    acknowledged_at: datetime
    revoked_at: datetime | None = None
    revoked_by_user_id: uuid.UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class SodEvent:
    id: uuid.UUID
    organization_id: uuid.UUID
    rule: SodRule
    outcome: SodOutcome
    actor_user_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID | None
    detail: str
    deviation_id: uuid.UUID | None
    occurred_at: datetime


class NotOwnerError(Exception):
    """IAM-064: only the Owner may acknowledge a deviation."""


class RuleNotDeviableError(Exception):
    """See DEVIABLE_RULES."""


class DeviationReasonRequiredError(Exception):
    """IAM-064: a deviation is "recorded with a reason". A blank one is not
    a reason, and the database rejects it too (CHECK on sod_policy_deviation).
    """


# ---------------------------------------------------------------------------
# The rules themselves - pure functions over a DutyContext
# ---------------------------------------------------------------------------
# Each returns a refusal detail, or None if the rule does not object. Pure
# and side-effect free so they can be reasoned about (and tested) without a
# repository, a clock or a database.


def _creator_not_sole_approver(context: DutyContext) -> str | None:
    """IAM-060. Note "sole": the creator MAY approve when a second approval
    is still required, because they will not end up the only approver. What
    they cannot do is provide the single approval that completes it.
    """
    if context.action != "approve" or context.resource_type != "purchase_invoice":
        return None

    if context.actor_user_id in context.approved_by:
        return "this user has already approved this invoice"

    involved = context.actor_user_id == context.created_by or (
        context.actor_user_id in context.edited_by
    )
    if not involved:
        return None

    others = [user for user in context.approved_by if user != context.actor_user_id]
    if context.required_approvals > 1 and len(others) + 1 < context.required_approvals:
        # Their approval will not complete the invoice, so another approver
        # is still required and they cannot end up the sole one.
        return None
    if others:
        return None

    return (
        "the user who created or edited this invoice would be its sole approver; "
        "a second approver is required"
    )


def _approver_not_releaser(context: DutyContext) -> str | None:
    """IAM-061. The user-count exemption is applied by the service, not
    here, so this function stays a pure statement of the rule.
    """
    if context.action != "release" or context.resource_type != "payment_batch":
        return None
    if context.actor_user_id not in context.approved_by:
        return None
    return "the user who approved this payment batch cannot also release it to the bank"


def _submitter_not_approver(context: DutyContext) -> str | None:
    """IAM-062. Unlike IAM-060 there is no "sole" here - the submitter
    cannot approve their own expense at all, however many other approvals
    exist.
    """
    if context.action != "approve" or context.resource_type != "expense":
        return None
    if context.actor_user_id != context.submitted_by:
        return None
    return "the user who submitted this expense cannot approve it"


def _no_self_grant(context: DutyContext) -> str | None:
    """IAM-063, first half: "a user cannot grant themselves a permission
    they do not hold." Granting yourself something you DO hold is not
    forbidden by this requirement - it is a no-op in permission terms.
    """
    if context.action != "grant" or context.resource_type != "user_role":
        return None
    if context.actor_user_id != context.subject_user_id:
        return None
    if not context.permissions_not_held:
        return None
    listed = ", ".join(f"{a} {r}" for a, r in sorted(context.permissions_not_held))
    return f"a user cannot grant themselves permissions they do not hold: {listed}"


def _no_self_approved_access_request(context: DutyContext) -> str | None:
    """IAM-063, second half: "nor approve their own access request." """
    if context.action != "approve" or context.resource_type != "access_request":
        return None
    if context.actor_user_id != context.subject_user_id:
        return None
    return "a user cannot approve their own access request"


RULES: dict[SodRule, Callable[[DutyContext], str | None]] = {
    SodRule.CREATOR_NOT_SOLE_APPROVER: _creator_not_sole_approver,
    SodRule.APPROVER_NOT_RELEASER: _approver_not_releaser,
    SodRule.SUBMITTER_NOT_APPROVER: _submitter_not_approver,
    SodRule.NO_SELF_GRANT: _no_self_grant,
    SodRule.NO_SELF_APPROVED_ACCESS_REQUEST: _no_self_approved_access_request,
}


# ---------------------------------------------------------------------------
# The audit report (IAM-064, IAM-065)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExemptionDisclosure:
    """IAM-065's "disclosed in the audit report rather than hidden", as a
    value. Present in every report whether or not it is active, so the
    report answers "is SoD being enforced here" positively rather than by
    the absence of a warning - a reader must never have to infer enforcement
    from silence.
    """

    active_user_count: int
    single_user_exempt: bool
    approver_releaser_exempt: bool
    note: str


@dataclass(frozen=True, slots=True)
class SodAuditReport:
    organization_id: uuid.UUID
    generated_at: datetime
    exemption: ExemptionDisclosure
    active_deviations: tuple[Deviation, ...] = ()
    revoked_deviations: tuple[Deviation, ...] = ()
    events: tuple[SodEvent, ...] = ()
    enforced_rules: tuple[SodRule, ...] = ()

    @property
    def fully_enforced(self) -> bool:
        return (
            not self.active_deviations
            and not self.exemption.single_user_exempt
            and not self.exemption.approver_releaser_exempt
        )


class SodRepository(Protocol):
    async def active_user_count(self, organization_id: uuid.UUID) -> int:
        """Distinct users holding a live role assignment reaching this
        organization, whose account is active. There is no membership table
        (users are global - see 0003_authentication.sql); membership IS
        holding a grant, so this count is derived from the same rows
        authorization reads rather than from a separate list that could
        disagree with it.
        """
        ...

    async def holds_owner_role(self, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> bool: ...

    async def active_deviation(
        self, *, organization_id: uuid.UUID, rule: SodRule
    ) -> Deviation | None: ...

    async def list_deviations(self, organization_id: uuid.UUID) -> Sequence[Deviation]: ...

    async def create_deviation(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        acknowledged_by_user_id: uuid.UUID,
        reason: str,
    ) -> uuid.UUID: ...

    async def revoke_deviation(
        self, *, deviation_id: uuid.UUID, revoked_by_user_id: uuid.UUID, at: datetime
    ) -> None: ...

    async def record_event(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        outcome: SodOutcome,
        actor_user_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID | None,
        detail: str,
        deviation_id: uuid.UUID | None,
        at: datetime,
    ) -> uuid.UUID: ...

    async def list_events(self, organization_id: uuid.UUID) -> Sequence[SodEvent]: ...


class SegregationOfDutiesService:
    def __init__(
        self,
        repository: SodRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
        audit: AuditTrail | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        # IAM-090's "configuration changes". sod_event (0012) already records
        # what SoD DID; this records who changed what it is configured to do,
        # in the tamper-evident log a reviewer reads (IAM-092).
        self._audit = audit

    async def check(self, context: DutyContext) -> SodDecision:
        """Evaluates every rule against one attempted action.

        Order matters and is deliberate: the rule must OBJECT before any
        exemption is considered, so an exemption is only ever recorded when
        it actually changed the outcome. Checking exemptions first would
        make a single-user organization report a permanent stream of
        exemptions for actions no rule would have blocked anyway.
        """
        for rule, evaluate in RULES.items():
            detail = evaluate(context)
            if detail is None:
                continue

            return await self._resolve(rule, detail, context)

        return SodDecision(permitted=True, outcome="permitted")

    async def _resolve(self, rule: SodRule, detail: str, context: DutyContext) -> SodDecision:
        active_users = await self._repository.active_user_count(context.organization_id)

        # IAM-065. Applies to every rule, including the non-deviable ones:
        # "by necessity" means there is literally nobody else to perform the
        # second step, which is a fact about the organization rather than a
        # policy choice. Not recorded per action - it would be one row per
        # action forever; audit_report() discloses it from the live count.
        if active_users <= SINGLE_USER_THRESHOLD:
            return SodDecision(
                permitted=True,
                outcome="single_user_exempt",
                rule=rule,
                detail=(
                    f"{detail} - permitted because this organization has "
                    f"{active_users} active user(s) and is exempt by necessity (IAM-065)"
                ),
            )

        # IAM-061's own, narrower threshold. Distinct from IAM-065 above:
        # this one applies to exactly one rule and at a different count.
        if rule is SodRule.APPROVER_NOT_RELEASER and active_users < APPROVER_RELEASER_MIN_USERS:
            return SodDecision(
                permitted=True,
                outcome="two_user_exempt",
                rule=rule,
                detail=(
                    f"{detail} - permitted because IAM-061 applies only where the "
                    f"organization has more than two active users (this one has "
                    f"{active_users})"
                ),
            )

        if rule in DEVIABLE_RULES:
            deviation = await self._repository.active_deviation(
                organization_id=context.organization_id, rule=rule
            )
            if deviation is not None:
                # IAM-064: this IS the row that must appear in the audit
                # report, so it is recorded before the decision is returned.
                await self._record(
                    context,
                    rule=rule,
                    outcome="deviation_applied",
                    detail=f"{detail} - permitted by deviation: {deviation.reason}",
                    deviation_id=deviation.id,
                )
                return SodDecision(
                    permitted=True,
                    outcome="deviation_applied",
                    rule=rule,
                    detail=f"{detail} - permitted by an Owner-acknowledged deviation",
                    deviation_id=deviation.id,
                )

        await self._record(context, rule=rule, outcome="blocked", detail=detail)
        return SodDecision(permitted=False, outcome="blocked", rule=rule, detail=detail)

    async def _record(
        self,
        context: DutyContext,
        *,
        rule: SodRule,
        outcome: SodOutcome,
        detail: str,
        deviation_id: uuid.UUID | None = None,
    ) -> None:
        await self._repository.record_event(
            organization_id=context.organization_id,
            rule=rule,
            outcome=outcome,
            actor_user_id=context.actor_user_id,
            resource_type=context.resource_type,
            resource_id=context.resource_id,
            detail=detail,
            deviation_id=deviation_id,
            at=self._clock(),
        )

    async def acknowledge_deviation(
        self,
        *,
        owner_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        rule: SodRule,
        reason: str,
    ) -> uuid.UUID:
        """IAM-064: "each deviation must be explicitly acknowledged by the
        Owner, is recorded with a reason, and appears in the audit report."

        Authority is checked against the Owner ROLE specifically, not
        against a permission. That is unusual in this codebase - everything
        else asks the authorization library about a permission - and it is
        deliberate: any permission-based check could be satisfied by a
        custom role composed to hold that permission (IAM-036 allows an
        admin to compose from what they hold), which would let an
        organization route around "the Owner acknowledged this". Owner is a
        system role, fixed by migration and not composable, so requiring it
        by name is the only formulation that cannot be arranged around.
        """
        if not reason or not reason.strip():
            raise DeviationReasonRequiredError(
                "IAM-064 requires a recorded reason for every SoD deviation"
            )

        if rule not in DEVIABLE_RULES:
            raise RuleNotDeviableError(
                f"{rule.value} cannot be deviated from - see DEVIABLE_RULES in api.authz.sod"
            )

        if not await self._repository.holds_owner_role(
            user_id=owner_user_id, organization_id=organization_id
        ):
            raise NotOwnerError(
                f"user {owner_user_id} does not hold the Owner role in organization "
                f"{organization_id}; only the Owner may acknowledge a SoD deviation (IAM-064)"
            )

        deviation_id = await self._repository.create_deviation(
            organization_id=organization_id,
            rule=rule,
            acknowledged_by_user_id=owner_user_id,
            reason=reason.strip(),
        )
        await self._audit_deviation(
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            rule=rule,
            action="acknowledge",
            deviation_id=deviation_id,
            detail={"reason": reason.strip()},
        )
        return deviation_id

    async def _audit_deviation(
        self,
        *,
        organization_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        rule: SodRule,
        action: str,
        deviation_id: uuid.UUID | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.configuration_change(
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            action=action,
            resource_type="sod_deviation",
            resource_id=deviation_id,
            outcome=AuditOutcome.SUCCESS,
            detail={"rule": rule.value, **(detail or {})},
        )

    async def revoke_deviation(
        self, *, owner_user_id: uuid.UUID, organization_id: uuid.UUID, rule: SodRule
    ) -> bool:
        """Restoring a rule needs the same authority as suspending it -
        otherwise the Owner's acknowledgement could be undone by someone the
        requirement never named. Returns False if there was nothing active
        to revoke.
        """
        if not await self._repository.holds_owner_role(
            user_id=owner_user_id, organization_id=organization_id
        ):
            raise NotOwnerError(
                f"user {owner_user_id} does not hold the Owner role in organization "
                f"{organization_id}"
            )

        deviation = await self._repository.active_deviation(
            organization_id=organization_id, rule=rule
        )
        if deviation is None:
            return False

        await self._repository.revoke_deviation(
            deviation_id=deviation.id, revoked_by_user_id=owner_user_id, at=self._clock()
        )
        await self._audit_deviation(
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            rule=rule,
            action="revoke",
            deviation_id=deviation.id,
        )
        return True

    async def audit_report(self, organization_id: uuid.UUID) -> SodAuditReport:
        """IAM-064 and IAM-065's shared destination.

        Every way SoD can fail to apply is stated positively here: active
        deviations with who acknowledged them and why, both exemptions with
        the user count that triggers them, and which rules are actually
        being enforced. A reader never has to infer enforcement from the
        absence of a warning, which is what "disclosed rather than hidden"
        rules out.
        """
        now = self._clock()
        active_users = await self._repository.active_user_count(organization_id)
        deviations = list(await self._repository.list_deviations(organization_id))
        events = tuple(await self._repository.list_events(organization_id))

        single_user_exempt = active_users <= SINGLE_USER_THRESHOLD
        approver_releaser_exempt = single_user_exempt or active_users < APPROVER_RELEASER_MIN_USERS

        if single_user_exempt:
            note = (
                f"This organization has {active_users} active user(s). Segregation of "
                "duties cannot be satisfied by a single person, so ALL SoD rules are "
                "exempt by necessity (IAM-065). Every action below was permitted on "
                "that basis, not because duties were segregated."
            )
        elif approver_releaser_exempt:
            note = (
                f"This organization has {active_users} active users. IAM-061 "
                "(payment approver may not release) applies only where an organization "
                "has more than two active users, so it is NOT enforced here. All other "
                "SoD rules are enforced."
            )
        else:
            note = (
                f"This organization has {active_users} active users. No exemption by "
                "user count applies; every SoD rule is enforced except where an "
                "Owner-acknowledged deviation is listed below."
            )

        active = tuple(d for d in deviations if d.is_active)
        deviated = {d.rule for d in active}
        enforced = tuple(
            rule
            for rule in RULES
            if rule not in deviated
            and not single_user_exempt
            and not (rule is SodRule.APPROVER_NOT_RELEASER and approver_releaser_exempt)
        )

        return SodAuditReport(
            organization_id=organization_id,
            generated_at=now,
            exemption=ExemptionDisclosure(
                active_user_count=active_users,
                single_user_exempt=single_user_exempt,
                approver_releaser_exempt=approver_releaser_exempt,
                note=note,
            ),
            active_deviations=active,
            revoked_deviations=tuple(d for d in deviations if not d.is_active),
            events=events,
            enforced_rules=enforced,
        )


# Re-exported for callers building a DutyContext for a grant, so the action
# and resource_type strings the rules match on are not retyped at each call
# site (a typo there would silently mean "no rule applies").
GRANT_CONTEXT: dict[str, str] = {"action": "grant", "resource_type": "user_role"}
ACCESS_REQUEST_CONTEXT: dict[str, str] = {
    "action": "approve",
    "resource_type": "access_request",
}
INVOICE_APPROVAL_CONTEXT: dict[str, str] = {
    "action": "approve",
    "resource_type": "purchase_invoice",
}
PAYMENT_RELEASE_CONTEXT: dict[str, str] = {
    "action": "release",
    "resource_type": "payment_batch",
}
EXPENSE_APPROVAL_CONTEXT: dict[str, str] = {"action": "approve", "resource_type": "expense"}

__all__ = [
    "ACCESS_REQUEST_CONTEXT",
    "APPROVER_RELEASER_MIN_USERS",
    "DEVIABLE_RULES",
    "EXPENSE_APPROVAL_CONTEXT",
    "GRANT_CONTEXT",
    "INVOICE_APPROVAL_CONTEXT",
    "PAYMENT_RELEASE_CONTEXT",
    "RULES",
    "SINGLE_USER_THRESHOLD",
    "Deviation",
    "DeviationReasonRequiredError",
    "DutyContext",
    "ExemptionDisclosure",
    "NotOwnerError",
    "RuleNotDeviableError",
    "SegregationOfDutiesService",
    "SodAuditReport",
    "SodDecision",
    "SodEvent",
    "SodOutcome",
    "SodRepository",
    "SodRule",
]

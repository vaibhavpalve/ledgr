"""IAM-060 through IAM-065 (PRD §8.7): segregation of duties.

One section per requirement. The rules are tested through the real
SegregationOfDutiesService against the in-memory repository fake - no
database - because what matters is the decision and the record it leaves,
both of which are the service's job rather than the pure rule functions'.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.authz.sod import (
    APPROVER_RELEASER_MIN_USERS,
    DEVIABLE_RULES,
    RULES,
    DeviationReasonRequiredError,
    DutyContext,
    NotOwnerError,
    RuleNotDeviableError,
    SegregationOfDutiesService,
    SodRule,
)
from tests.support.fake_sod_repository import InMemorySodRepository

ORG = uuid.uuid4()
ALICE, BOB, CAROL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _service(*, active_users: int = 10) -> tuple[SegregationOfDutiesService, InMemorySodRepository]:
    repository = InMemorySodRepository()
    repository.set_active_user_count(ORG, active_users)
    repository.make_owner(user_id=ALICE, organization_id=ORG)
    return SegregationOfDutiesService(repository, clock=_FakeClock()), repository


# ===========================================================================
# IAM-060: the creator cannot be the SOLE approver of a purchase invoice
# ===========================================================================


def _approve_invoice(
    actor: uuid.UUID,
    *,
    created_by: uuid.UUID,
    approved_by: tuple[uuid.UUID, ...] = (),
    required_approvals: int = 1,
    edited_by: tuple[uuid.UUID, ...] = (),
) -> DutyContext:
    return DutyContext(
        organization_id=ORG,
        actor_user_id=actor,
        action="approve",
        resource_type="purchase_invoice",
        resource_id=uuid.UUID(int=1),
        created_by=created_by,
        edited_by=edited_by,
        approved_by=approved_by,
        required_approvals=required_approvals,
    )


async def test_the_creator_cannot_provide_the_only_approval() -> None:
    service, _ = _service()

    decision = await service.check(_approve_invoice(ALICE, created_by=ALICE))

    assert decision.refused
    assert decision.rule is SodRule.CREATOR_NOT_SOLE_APPROVER
    assert "sole approver" in decision.detail


async def test_someone_else_may_approve_an_invoice_its_creator_made() -> None:
    service, _ = _service()

    decision = await service.check(_approve_invoice(BOB, created_by=ALICE))

    assert decision.permitted
    assert decision.outcome == "permitted"


async def test_an_editor_is_treated_as_a_creator() -> None:
    """IAM-060 says "creates or edits" - someone who changed the amount is
    as conflicted as the person who entered it.
    """
    service, _ = _service()

    decision = await service.check(_approve_invoice(BOB, created_by=ALICE, edited_by=(BOB,)))

    assert decision.refused
    assert decision.rule is SodRule.CREATOR_NOT_SOLE_APPROVER


async def test_the_creator_may_approve_when_a_second_approval_is_still_required() -> None:
    """The word "sole" doing its work. With two approvals required, the
    creator's approval cannot complete the invoice, so they will not end up
    its only approver - which is all IAM-060 forbids.
    """
    service, _ = _service()

    decision = await service.check(_approve_invoice(ALICE, created_by=ALICE, required_approvals=2))

    assert decision.permitted


async def test_the_creator_may_approve_once_someone_else_already_has() -> None:
    service, _ = _service()

    decision = await service.check(_approve_invoice(ALICE, created_by=ALICE, approved_by=(BOB,)))

    assert decision.permitted


async def test_the_creator_cannot_approve_twice_to_satisfy_a_two_approval_rule() -> None:
    """The obvious way around the previous test: approve, then approve
    again. A user already in approved_by is refused outright.
    """
    service, _ = _service()

    decision = await service.check(
        _approve_invoice(ALICE, created_by=ALICE, approved_by=(ALICE,), required_approvals=2)
    )

    assert decision.refused
    assert "already approved" in decision.detail


# ===========================================================================
# IAM-061: the payment approver cannot release, above two active users
# ===========================================================================


def _release_batch(actor: uuid.UUID, *, approved_by: tuple[uuid.UUID, ...]) -> DutyContext:
    return DutyContext(
        organization_id=ORG,
        actor_user_id=actor,
        action="release",
        resource_type="payment_batch",
        resource_id=uuid.UUID(int=2),
        approved_by=approved_by,
    )


async def test_the_approver_of_a_payment_batch_cannot_release_it() -> None:
    service, _ = _service(active_users=5)

    decision = await service.check(_release_batch(ALICE, approved_by=(ALICE,)))

    assert decision.refused
    assert decision.rule is SodRule.APPROVER_NOT_RELEASER


async def test_a_different_user_may_release_what_someone_else_approved() -> None:
    service, _ = _service(active_users=5)

    decision = await service.check(_release_batch(BOB, approved_by=(ALICE,)))

    assert decision.permitted


@pytest.mark.parametrize("active_users", [2, 1])
async def test_iam_061_does_not_apply_at_or_below_two_active_users(active_users: int) -> None:
    """IAM-061 is qualified: "where the organization has more than two
    active users." At two, there may be nobody else who can release, so the
    rule is not in force - and the outcome says which exemption applied
    rather than reporting a plain permit.
    """
    service, _ = _service(active_users=active_users)

    decision = await service.check(_release_batch(ALICE, approved_by=(ALICE,)))

    assert decision.permitted
    assert decision.outcome in ("two_user_exempt", "single_user_exempt")
    assert decision.rule is SodRule.APPROVER_NOT_RELEASER


async def test_iam_061_applies_at_exactly_three_active_users() -> None:
    """The boundary "more than two" resolves to three, asserted directly so
    an off-by-one in APPROVER_RELEASER_MIN_USERS cannot pass.
    """
    assert APPROVER_RELEASER_MIN_USERS == 3
    service, _ = _service(active_users=3)

    decision = await service.check(_release_batch(ALICE, approved_by=(ALICE,)))

    assert decision.refused


async def test_the_two_user_exemption_does_not_relax_the_other_rules() -> None:
    """IAM-061's threshold is its own. A two-user organization is still
    subject to IAM-062, because two people are enough to segregate an
    expense submission from its approval.
    """
    service, _ = _service(active_users=2)

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="approve",
            resource_type="expense",
            submitted_by=ALICE,
        )
    )

    assert decision.refused
    assert decision.rule is SodRule.SUBMITTER_NOT_APPROVER


# ===========================================================================
# IAM-062: the submitter of an expense cannot approve it
# ===========================================================================


def _approve_expense(actor: uuid.UUID, *, submitted_by: uuid.UUID, **kwargs: object) -> DutyContext:
    return DutyContext(
        organization_id=ORG,
        actor_user_id=actor,
        action="approve",
        resource_type="expense",
        resource_id=uuid.UUID(int=3),
        submitted_by=submitted_by,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_the_submitter_cannot_approve_their_own_expense() -> None:
    service, _ = _service()

    decision = await service.check(_approve_expense(ALICE, submitted_by=ALICE))

    assert decision.refused
    assert decision.rule is SodRule.SUBMITTER_NOT_APPROVER


async def test_someone_else_may_approve_an_expense() -> None:
    service, _ = _service()

    decision = await service.check(_approve_expense(BOB, submitted_by=ALICE))

    assert decision.permitted


async def test_iam_062_has_no_sole_escape_unlike_iam_060() -> None:
    """IAM-060 says "sole approver"; IAM-062 does not. A submitter cannot
    approve their own expense however many other approvals exist - the two
    rules are deliberately not factored together.
    """
    service, _ = _service()

    decision = await service.check(
        _approve_expense(ALICE, submitted_by=ALICE, approved_by=(BOB, CAROL))
    )

    assert decision.refused


# ===========================================================================
# IAM-063: nobody grants themselves permissions
# ===========================================================================


async def test_a_user_cannot_grant_themselves_a_permission_they_lack() -> None:
    service, _ = _service()

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="grant",
            resource_type="user_role",
            subject_user_id=ALICE,
            permissions_not_held=(("post", "journal_entry"),),
        )
    )

    assert decision.refused
    assert decision.rule is SodRule.NO_SELF_GRANT
    assert "post journal_entry" in decision.detail


async def test_granting_yourself_something_you_already_hold_is_not_forbidden() -> None:
    """IAM-063's wording is "a permission they do not hold". Re-granting
    yourself something you already have changes nothing, and refusing it
    would block ordinary operations like re-scoping your own access.
    """
    service, _ = _service()

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="grant",
            resource_type="user_role",
            subject_user_id=ALICE,
            permissions_not_held=(),
        )
    )

    assert decision.permitted


async def test_granting_someone_else_a_permission_is_not_a_self_grant() -> None:
    service, _ = _service()

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="grant",
            resource_type="user_role",
            subject_user_id=BOB,
            permissions_not_held=(("post", "journal_entry"),),
        )
    )

    # Not an SoD problem. IAM-036's ceiling in
    # AuthorizationService.assign_role is what refuses this one.
    assert decision.permitted


async def test_a_user_cannot_approve_their_own_access_request() -> None:
    service, _ = _service()

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="approve",
            resource_type="access_request",
            subject_user_id=ALICE,
        )
    )

    assert decision.refused
    assert decision.rule is SodRule.NO_SELF_APPROVED_ACCESS_REQUEST


# ===========================================================================
# IAM-064: deviations are configurable, Owner-acknowledged, and recorded
# ===========================================================================


async def test_a_deviation_permits_what_the_rule_would_refuse() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Two-person finance team; the CFO submits and approves travel.",
    )

    decision = await service.check(_approve_expense(ALICE, submitted_by=ALICE))

    assert decision.permitted
    assert decision.outcome == "deviation_applied"
    assert decision.deviation_id is not None


async def test_only_the_owner_may_acknowledge_a_deviation() -> None:
    service, _ = _service()

    with pytest.raises(NotOwnerError):
        await service.acknowledge_deviation(
            owner_user_id=BOB,  # not an Owner
            organization_id=ORG,
            rule=SodRule.SUBMITTER_NOT_APPROVER,
            reason="I would like this off, please.",
        )


async def test_a_deviation_requires_a_reason() -> None:
    service, _ = _service()

    for blank in ("", "   ", "\n\t"):
        with pytest.raises(DeviationReasonRequiredError):
            await service.acknowledge_deviation(
                owner_user_id=ALICE,
                organization_id=ORG,
                rule=SodRule.SUBMITTER_NOT_APPROVER,
                reason=blank,
            )


async def test_using_a_deviation_is_recorded_with_the_deviation_that_permitted_it() -> None:
    """IAM-064: a deviation "appears in the audit report". The report is
    built from these rows, so the recording is the requirement, not a
    nicety - and the event names the deviation, so a reviewer can trace an
    unblocked action back to whoever acknowledged it and why.
    """
    service, repository = _service()
    deviation_id = await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Two-person finance team.",
    )

    await service.check(_approve_expense(ALICE, submitted_by=ALICE))

    assert len(repository.events) == 1
    event = repository.events[0]
    assert event.outcome == "deviation_applied"
    assert event.deviation_id == deviation_id
    assert event.actor_user_id == ALICE
    assert "Two-person finance team." in event.detail


async def test_a_deviation_covers_only_the_rule_it_names() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Expenses only.",
    )

    still_blocked = await service.check(_approve_invoice(ALICE, created_by=ALICE))

    assert still_blocked.refused
    assert still_blocked.rule is SodRule.CREATOR_NOT_SOLE_APPROVER


async def test_a_deviation_covers_only_the_organization_it_names() -> None:
    service, repository = _service()
    other_org = uuid.uuid4()
    repository.set_active_user_count(other_org, 10)
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Only here.",
    )

    decision = await service.check(
        DutyContext(
            organization_id=other_org,
            actor_user_id=ALICE,
            action="approve",
            resource_type="expense",
            submitted_by=ALICE,
        )
    )

    assert decision.refused


async def test_the_self_grant_rules_cannot_be_deviated_from() -> None:
    """A deliberate reading, flagged in api.authz.sod's DEVIABLE_RULES: a
    deviation here would defeat IAM-036, an unqualified requirement with no
    deviation clause of its own.
    """
    service, _ = _service()

    for rule in (SodRule.NO_SELF_GRANT, SodRule.NO_SELF_APPROVED_ACCESS_REQUEST):
        with pytest.raises(RuleNotDeviableError):
            await service.acknowledge_deviation(
                owner_user_id=ALICE,
                organization_id=ORG,
                rule=rule,
                reason="I am the Owner and I would like to grant myself things.",
            )


async def test_an_owner_can_restore_a_rule_by_revoking_its_deviation() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Temporary, during the audit.",
    )
    assert (await service.check(_approve_expense(ALICE, submitted_by=ALICE))).permitted

    revoked = await service.revoke_deviation(
        owner_user_id=ALICE, organization_id=ORG, rule=SodRule.SUBMITTER_NOT_APPROVER
    )

    assert revoked
    assert (await service.check(_approve_expense(ALICE, submitted_by=ALICE))).refused


async def test_only_the_owner_may_revoke_a_deviation() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Temporary.",
    )

    with pytest.raises(NotOwnerError):
        await service.revoke_deviation(
            owner_user_id=BOB, organization_id=ORG, rule=SodRule.SUBMITTER_NOT_APPROVER
        )


async def test_two_active_deviations_for_one_rule_are_impossible() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="First.",
    )

    with pytest.raises(ValueError, match="already has an active deviation"):
        await service.acknowledge_deviation(
            owner_user_id=ALICE,
            organization_id=ORG,
            rule=SodRule.SUBMITTER_NOT_APPROVER,
            reason="Second.",
        )


# ===========================================================================
# IAM-065: single-user organizations are exempt, and it is disclosed
# ===========================================================================


async def test_a_single_user_organization_is_exempt_from_every_rule() -> None:
    service, _ = _service(active_users=1)

    for context in (
        _approve_invoice(ALICE, created_by=ALICE),
        _release_batch(ALICE, approved_by=(ALICE,)),
        _approve_expense(ALICE, submitted_by=ALICE),
    ):
        decision = await service.check(context)
        assert decision.permitted
        assert decision.outcome == "single_user_exempt"
        # The rule that WOULD have blocked is still named, so the exemption
        # is legible rather than an unexplained permit.
        assert decision.rule is not None


async def test_the_single_user_exemption_is_not_recorded_per_action() -> None:
    """One row per action forever would drown the audit report in a
    one-person organization. The exemption is disclosed structurally, from
    the live user count, by audit_report() instead.
    """
    service, repository = _service(active_users=1)

    for _ in range(20):
        await service.check(_approve_expense(ALICE, submitted_by=ALICE))

    assert repository.events == []


async def test_the_exemption_lapses_when_a_second_user_joins() -> None:
    """The exemption is a live fact, not a stored flag, so it stops
    applying the moment the organization stops being single-user - with no
    administrative action.
    """
    service, repository = _service(active_users=1)
    assert (await service.check(_approve_expense(ALICE, submitted_by=ALICE))).permitted

    repository.set_active_user_count(ORG, 2)

    assert (await service.check(_approve_expense(ALICE, submitted_by=ALICE))).refused


async def test_the_report_discloses_the_single_user_exemption() -> None:
    service, _ = _service(active_users=1)

    report = await service.audit_report(ORG)

    assert report.exemption.single_user_exempt
    assert report.exemption.active_user_count == 1
    assert "exempt by necessity" in report.exemption.note
    assert "IAM-065" in report.exemption.note
    assert not report.fully_enforced
    assert report.enforced_rules == ()


async def test_the_report_discloses_the_two_user_exemption_separately() -> None:
    service, _ = _service(active_users=2)

    report = await service.audit_report(ORG)

    assert not report.exemption.single_user_exempt
    assert report.exemption.approver_releaser_exempt
    assert "IAM-061" in report.exemption.note
    assert SodRule.APPROVER_NOT_RELEASER not in report.enforced_rules
    # Every other rule is still enforced at two users.
    assert SodRule.SUBMITTER_NOT_APPROVER in report.enforced_rules


async def test_the_report_states_enforcement_positively_when_nothing_is_exempt() -> None:
    """ "Disclosed rather than hidden" cuts both ways: a reader must not have
    to infer that SoD IS being enforced from the absence of a warning.
    """
    service, _ = _service(active_users=6)

    report = await service.audit_report(ORG)

    assert report.fully_enforced
    assert not report.exemption.single_user_exempt
    assert not report.exemption.approver_releaser_exempt
    assert set(report.enforced_rules) == set(RULES)
    assert "every SoD rule is enforced" in report.exemption.note


async def test_the_report_lists_active_deviations_with_who_and_why() -> None:
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Two-person finance team; documented in the 2026 controls review.",
    )

    report = await service.audit_report(ORG)

    assert len(report.active_deviations) == 1
    deviation = report.active_deviations[0]
    assert deviation.acknowledged_by_user_id == ALICE
    assert "2026 controls review" in deviation.reason
    assert deviation.rule is SodRule.SUBMITTER_NOT_APPROVER
    assert SodRule.SUBMITTER_NOT_APPROVER not in report.enforced_rules
    assert not report.fully_enforced


async def test_the_report_keeps_revoked_deviations_visible() -> None:
    """A deviation that was in force for six months and then revoked is
    exactly what an auditor reviewing that period needs to see.
    """
    service, _ = _service()
    await service.acknowledge_deviation(
        owner_user_id=ALICE,
        organization_id=ORG,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Temporary, pending a second hire.",
    )
    await service.revoke_deviation(
        owner_user_id=ALICE, organization_id=ORG, rule=SodRule.SUBMITTER_NOT_APPROVER
    )

    report = await service.audit_report(ORG)

    assert report.active_deviations == ()
    assert len(report.revoked_deviations) == 1
    assert report.revoked_deviations[0].revoked_by_user_id == ALICE
    assert report.fully_enforced


async def test_the_report_contains_every_blocked_attempt() -> None:
    service, _ = _service()
    await service.check(_approve_expense(ALICE, submitted_by=ALICE))
    await service.check(_approve_invoice(BOB, created_by=BOB))

    report = await service.audit_report(ORG)

    assert len(report.events) == 2
    assert {e.outcome for e in report.events} == {"blocked"}
    assert {e.rule for e in report.events} == {
        SodRule.SUBMITTER_NOT_APPROVER,
        SodRule.CREATOR_NOT_SOLE_APPROVER,
    }


async def test_a_permitted_action_leaves_no_event() -> None:
    """Only things a reviewer needs to look at are recorded; a report full
    of ordinary approvals is a report nobody reads.
    """
    service, repository = _service()

    await service.check(_approve_expense(BOB, submitted_by=ALICE))

    assert repository.events == []


# ===========================================================================
# Cross-cutting
# ===========================================================================


async def test_an_unrelated_action_is_permitted_without_touching_any_rule() -> None:
    service, repository = _service()

    decision = await service.check(
        DutyContext(
            organization_id=ORG,
            actor_user_id=ALICE,
            action="post",
            resource_type="journal_entry",
        )
    )

    assert decision.permitted
    assert decision.rule is None
    assert repository.events == []


def test_every_rule_has_an_implementation() -> None:
    """Guards against adding a SodRule member without wiring it into RULES,
    which would make it a rule that exists in the audit report's vocabulary
    and never fires.
    """
    assert set(RULES) == set(SodRule)


def test_deviable_rules_are_a_subset_of_the_rules() -> None:
    assert set(SodRule) >= DEVIABLE_RULES
    assert SodRule.NO_SELF_GRANT not in DEVIABLE_RULES
    assert SodRule.NO_SELF_APPROVED_ACCESS_REQUEST not in DEVIABLE_RULES

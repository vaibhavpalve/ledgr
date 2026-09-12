"""IAM-036: "Custom roles can be composed by organization admins, but only
from permissions they themselves hold - no privilege escalation by role
authoring."

Organised as: what composition legitimately allows, then every escalation
route into it. The escalation tests are the point of the module - each one
is a way an admin might try to end up holding more than they started with,
and each must fail.

The world these run against is the real Appendix A matrix
(tests/authz/helpers.py), so "Organization Admin" and "Accountant" mean here
what they mean in production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.authz.model import AdministrationScope, AuthorizationRequest, OrganizationScope
from api.authz.service import (
    AuthorizationService,
    NotPermittedToComposeError,
    PrivilegeEscalationError,
    RoleCompositionError,
    SegregationOfDutiesError,
)
from api.authz.sod import SegregationOfDutiesService, SodRule
from tests.authz.helpers import World, build_world
from tests.support.fake_sod_repository import InMemorySodRepository


def _admin_world() -> tuple[World, AuthorizationService]:
    """An Organization Admin at acme - IAM-036's subject. Holds `manage
    user_role`, so they may compose; holds no ledger permission, so the
    ceiling is meaningfully lower than "everything".
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    return world, AuthorizationService(world.repository)


async def _holds(
    service: AuthorizationService, world: World, action: str, resource_type: str
) -> bool:
    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action=action,
            resource_type=resource_type,
            target=AdministrationScope(world.acme_books),
        )
    )
    return decision.allowed


# ===========================================================================
# What composition legitimately allows
# ===========================================================================


async def test_an_admin_may_compose_a_role_from_permissions_they_hold() -> None:
    world, service = _admin_world()

    role_id = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Expense Desk",
        scope_type="administration",
        permissions=[("view", "administration"), ("submit", "expense")],
    )

    assert role_id is not None


async def test_an_admin_may_compose_a_role_from_another_role_they_hold() -> None:
    """Nesting, working. The admin also holds Accountant on one of acme's
    administrations, so Accountant's bundle is within their ceiling and may
    be used as a component.
    """
    world, service = _admin_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    accountant = world.repository.role_id("Accountant")

    role_id = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Senior Bookkeeper",
        scope_type="administration",
        component_roles=[accountant],
    )

    composed = await world.repository.role_permissions((role_id,))
    assert ("post", "journal_entry") in composed, "the component's permissions were inherited"
    assert ("view", "administration") in composed


async def test_a_composed_role_flattens_its_components_permissions() -> None:
    """Composition is static: the effective set is written into the new role
    at creation, not resolved from its components later. That is what stops
    a component changing under an already-composed role.
    """
    world, service = _admin_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    invoicer = world.repository.role_id("Invoicer")

    role_id = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Billing Clerk",
        scope_type="administration",
        permissions=[("view", "report")],
        component_roles=[invoicer],
    )

    composed = await world.repository.role_permissions((role_id,))
    invoicer_bundle = await world.repository.role_permissions((invoicer,))

    assert invoicer_bundle <= composed
    assert ("view", "report") in composed


# ===========================================================================
# Escalation: directly
# ===========================================================================


async def test_an_admin_cannot_author_a_role_granting_more_than_they_hold() -> None:
    """The headline case. An Organization Admin holds `manage user_role` but
    no ledger permission (Appendix A gives them "—" on "Post journal
    entries"), so they cannot write themselves one.
    """
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Admin Plus",
            scope_type="administration",
            permissions=[("submit", "expense"), ("post", "journal_entry")],
        )

    assert ("post", "journal_entry") in raised.value.missing
    # Only what they lack is reported; what they legitimately hold is not.
    assert ("submit", "expense") not in raised.value.missing
    assert raised.value.via[("post", "journal_entry")] == PrivilegeEscalationError.DIRECTLY


async def test_the_refusal_does_not_partially_create_the_role() -> None:
    """A refused composition must leave nothing behind - not a role holding
    the permissions that did pass the check.
    """
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Admin Plus",
            scope_type="administration",
            permissions=[("submit", "expense"), ("post", "journal_entry")],
        )

    assert world.repository.role_id_or_none("Admin Plus") is None


# ===========================================================================
# Escalation: through nesting
# ===========================================================================


async def test_an_admin_cannot_nest_a_role_they_do_not_hold() -> None:
    """The nesting case IAM-036's wording implies. The admin never names
    `post journal_entry`; they name Accountant, which contains it. Checking
    the flattened set is what catches this - a check against only the
    explicitly-listed permissions would pass.
    """
    world, service = _admin_world()
    accountant = world.repository.role_id("Accountant")

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Admin, Quietly Promoted",
            scope_type="administration",
            component_roles=[accountant],
        )

    assert ("post", "journal_entry") in raised.value.missing
    # The error names the component that contributed it, so the author is
    # not left guessing which of a sixty-permission bundle was the problem.
    assert raised.value.via[("post", "journal_entry")] == "Accountant"


async def test_an_admin_cannot_nest_the_owner_role() -> None:
    """The bluntest version: include Owner, inherit everything."""
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Definitely Not Owner",
            scope_type="organization",
            component_roles=[world.repository.role_id("Owner")],
        )

    assert ("post", "journal_entry") in raised.value.missing
    assert all(via == "Owner" for via in raised.value.via.values())


async def test_escalation_cannot_be_laundered_through_two_levels_of_nesting() -> None:
    """Transitivity. The admin legitimately composes level one from what
    they hold, then tries to compose level two from level one PLUS a role
    they do not hold - and then a third level from the second. Each level is
    checked against the same ceiling, and because every stored role's
    permissions are already flattened, level three inherits level one's
    closure without any recursive resolution.
    """
    world, service = _admin_world()

    level_one = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Level One",
        scope_type="administration",
        permissions=[("submit", "expense")],
    )

    # Level two: legitimate, nesting level one.
    level_two = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Level Two",
        scope_type="administration",
        component_roles=[level_one],
    )
    assert ("submit", "expense") in await world.repository.role_permissions((level_two,))

    # Level three: level two (fine) plus Bookkeeper (not held) - refused,
    # and the escalating permission is attributed to Bookkeeper, not to the
    # innocent component.
    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Level Three",
            scope_type="administration",
            component_roles=[level_two, world.repository.role_id("Bookkeeper")],
        )

    assert raised.value.via[("post", "journal_entry")] == "Bookkeeper"


async def test_a_composed_role_does_not_widen_when_its_component_would() -> None:
    """Why composition is flattened rather than resolved at evaluation time.

    The admin composes from a custom role, then that custom role's
    permissions are (hypothetically) extended. The composed role must not
    follow: its contents were authorized once, against what its author held
    at that moment.
    """
    world, service = _admin_world()

    base = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Base",
        scope_type="administration",
        permissions=[("submit", "expense")],
    )
    composed = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Composed",
        scope_type="administration",
        component_roles=[base],
    )

    # Simulate the component gaining a permission by some other path. In
    # production role_immutable_trg makes this impossible; here it is forced
    # to prove the composed role is insulated even if it were not.
    world.repository.force_add_permission("Base", ("post", "journal_entry"))

    assert ("post", "journal_entry") not in await world.repository.role_permissions((composed,))


# ===========================================================================
# Escalation: through conditions
# ===========================================================================


async def test_a_conditioned_grant_does_not_raise_the_authoring_ceiling() -> None:
    """The subtlest route, and the reason unconditionally_held_permissions
    exists.

    This admin is ALSO an Approver capped at EUR 5,000. If a capped grant
    counted toward the ceiling, they could author an uncapped role
    containing `approve purchase_invoice` and - since they hold `manage
    user_role` - assign it straight back to themselves. Holding a permission
    within limits is not holding the permission.
    """
    world, service = _admin_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Uncapped Approver",
            scope_type="administration",
            permissions=[("approve", "purchase_invoice")],
        )

    assert ("approve", "purchase_invoice") in raised.value.missing


async def test_the_same_permission_held_unconditionally_elsewhere_does_count() -> None:
    """The flip side, so the rule above is a real distinction rather than a
    blanket refusal: the same admin, additionally holding Accountant
    without conditions, may compose the permission - because they now do
    hold it outright.
    """
    world, service = _admin_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)

    role_id = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Approval Desk",
        scope_type="administration",
        permissions=[("approve", "purchase_invoice")],
    )

    assert role_id is not None


async def test_a_conditioned_grant_cannot_be_nested_either() -> None:
    """The condition rule must survive nesting: composing FROM the Approver
    role, while holding it only under a cap, is the same escalation with an
    extra step.
    """
    world, service = _admin_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Approver, Uncapped",
            scope_type="administration",
            component_roles=[world.repository.role_id("Approver")],
        )

    assert ("approve", "purchase_invoice") in raised.value.missing


# ===========================================================================
# Escalation: through authority, scope, time and tenancy
# ===========================================================================


async def test_holding_the_permissions_is_not_authority_to_compose() -> None:
    """IAM-036 says organization ADMINS compose roles. An Accountant holds a
    large bundle but no `manage user_role`, so they cannot define roles at
    all - even ones strictly within what they hold.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    with pytest.raises(NotPermittedToComposeError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Ledger Reviewer",
            scope_type="administration",
            permissions=[("view", "administration")],
        )


async def test_a_user_with_no_grants_at_all_can_compose_nothing() -> None:
    world = build_world()
    service = AuthorizationService(world.repository)

    with pytest.raises(NotPermittedToComposeError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Anything",
            scope_type="administration",
            permissions=[("view", "administration")],
        )


async def test_admin_authority_in_one_organization_does_not_reach_another() -> None:
    """An Organization Admin at acme composing inside klaver. IAM-032's
    narrowing applied to authoring: authority is per organization.
    """
    world, service = _admin_world()

    with pytest.raises(NotPermittedToComposeError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.klaver,
            name="Reaching Across",
            scope_type="administration",
            permissions=[("view", "administration")],
        )


async def test_permissions_held_in_another_organization_do_not_raise_the_ceiling() -> None:
    """The admin is an Organization Admin at acme AND an Accountant on
    klaver's books. The Accountant bundle must not count toward what they
    may author at acme.
    """
    world, service = _admin_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.klaver_books)

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Borrowed Authority",
            scope_type="administration",
            permissions=[("post", "journal_entry")],
        )

    assert ("post", "journal_entry") in raised.value.missing


async def test_an_expired_grant_does_not_raise_the_ceiling() -> None:
    world, service = _admin_world()
    world.repository.assign(
        user_id=world.user,
        role="Accountant",
        scope_id=world.acme_books,
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(PrivilegeEscalationError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Expired Authority",
            scope_type="administration",
            permissions=[("post", "journal_entry")],
        )


async def test_a_revoked_grant_does_not_raise_the_ceiling() -> None:
    world, service = _admin_world()
    world.repository.assign(
        user_id=world.user,
        role="Accountant",
        scope_id=world.acme_books,
        revoked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(PrivilegeEscalationError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Revoked Authority",
            scope_type="administration",
            permissions=[("post", "journal_entry")],
        )


async def test_an_administration_scope_role_cannot_nest_an_organization_scope_role() -> None:
    """IAM-032 through composition: an administration-scope role including
    an organization-scope one would carry organization-level permissions in
    an administration-level grant. Refused as a malformed composition, not
    as an escalation - the distinction matters because an Owner attempting
    it is making a modelling mistake, not an attack.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    with pytest.raises(RoleCompositionError, match="organization-scope role"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Sneaky",
            scope_type="administration",
            component_roles=[world.repository.role_id("Security Admin")],
        )


async def test_an_administration_scope_role_cannot_hold_an_organization_permission() -> None:
    """The same invariant reached by listing the permission directly rather
    than nesting a role.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    with pytest.raises(ValueError, match="organization-scope permission"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Sneaky Two",
            scope_type="administration",
            permissions=[("view", "administration"), ("manage", "security_policy")],
        )


async def test_an_unknown_component_role_is_refused() -> None:
    world, service = _admin_world()

    with pytest.raises(RoleCompositionError, match="not available"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Phantom",
            scope_type="administration",
            component_roles=[uuid.uuid4()],
        )


async def test_another_organizations_custom_role_cannot_be_nested() -> None:
    """And the refusal is worded identically to "no such role", so an author
    cannot probe for the existence of roles in organizations they cannot
    see.
    """
    world, service = _admin_world()
    foreign = world.repository.define_role(
        "Klaver Internal",
        scope_type="administration",
        permissions=[("view", "administration")],
        organization_id=world.klaver,
        is_system=False,
    )

    with pytest.raises(RoleCompositionError, match="not available"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Borrowed",
            scope_type="administration",
            component_roles=[foreign],
        )


async def test_an_archived_component_cannot_be_nested() -> None:
    world, service = _admin_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    retired = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Retired Desk",
        scope_type="administration",
        permissions=[("submit", "expense")],
    )
    world.repository.archive_role("Retired Desk")

    with pytest.raises(RoleCompositionError, match="archived"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Successor",
            scope_type="administration",
            component_roles=[retired],
        )


async def test_an_empty_role_is_rejected() -> None:
    world, service = _admin_world()

    with pytest.raises(ValueError, match="at least one permission"):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Empty",
            scope_type="administration",
            permissions=[],
        )


# ===========================================================================
# The adjacent control: composing safely is pointless if assigning is not
# ===========================================================================


async def test_an_admin_cannot_assign_a_role_conferring_more_than_they_hold() -> None:
    """Without this, every composition check above is theatre: an admin
    barred from AUTHORING a role with `post journal_entry` could simply
    assign the existing Accountant role instead.
    """
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=world.user,
            role_id=world.repository.role_id("Accountant"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    assert ("post", "journal_entry") in raised.value.missing


async def test_an_admin_can_assign_a_role_within_their_ceiling() -> None:
    """Compose then assign, end to end. Deliberately NOT the built-in
    Expense Submitter: that role holds `upload document` and `view document`
    (see EXTENSION_CAPABILITIES in api.authz.matrix), which an Organization
    Admin does not, so assigning it is correctly refused by the ceiling. An
    admin delegates what they actually hold, which is what composing a role
    from their own permissions gives them.
    """
    world, service = _admin_world()
    colleague = uuid.uuid4()

    role_id = await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Expense Desk",
        scope_type="administration",
        permissions=[("view", "administration"), ("submit", "expense")],
    )

    await service.assign_role(
        granter_user_id=world.user,
        subject_user_id=colleague,
        role_id=role_id,
        scope_type="administration",
        scope_id=world.acme_books,
    )

    assert await _holds_for(service, colleague, world, "submit", "expense")


async def test_an_admin_cannot_assign_a_builtin_role_that_exceeds_their_holdings() -> None:
    """The flip side, stated explicitly because it is easy to read the test
    above as a limitation rather than the control working: an Organization
    Admin cannot hand out Expense Submitter, because that role can read
    source documents and they cannot.
    """
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError) as raised:
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=uuid.uuid4(),
            role_id=world.repository.role_id("Expense Submitter"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    assert ("view", "document") in raised.value.missing


async def test_the_full_escalation_chain_compose_then_self_assign_is_closed() -> None:
    """End to end, the attack IAM-036 exists to stop: an Organization Admin
    writes a role containing a permission they lack and assigns it to
    themselves. Both halves refuse, and the admin still cannot post
    afterwards.
    """
    world, service = _admin_world()

    assert not await _holds(service, world, "post", "journal_entry")

    with pytest.raises(PrivilegeEscalationError):
        await service.create_custom_role(
            author_user_id=world.user,
            organization_id=world.acme,
            name="Self Promotion",
            scope_type="administration",
            permissions=[("post", "journal_entry")],
        )

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=world.user,
            role_id=world.repository.role_id("Bookkeeper"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    assert not await _holds(service, world, "post", "journal_entry")


async def test_assignment_authority_does_not_cross_organizations() -> None:
    world, service = _admin_world()

    with pytest.raises(NotPermittedToComposeError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=uuid.uuid4(),
            role_id=world.repository.role_id("Expense Submitter"),
            scope_type="administration",
            scope_id=world.klaver_books,
        )


async def test_a_conditioned_assignment_still_cannot_exceed_the_granters_ceiling() -> None:
    """Attaching conditions narrows a grant; it can never be used to slip
    past the ceiling, because the check is on what the role confers.
    """
    world, service = _admin_world()

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=uuid.uuid4(),
            role_id=world.repository.role_id("Approver"),
            scope_type="administration",
            scope_id=world.acme_books,
            conditions={"amount_ceiling": "1.00"},
        )


async def _holds_for(
    service: AuthorizationService,
    user_id: uuid.UUID,
    world: World,
    action: str,
    resource_type: str,
) -> bool:
    decision = await service.authorize(
        AuthorizationRequest(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            target=AdministrationScope(world.acme_books),
        )
    )
    return decision.allowed


async def test_a_refused_self_grant_is_recorded_when_sod_is_wired_in() -> None:
    """IAM-063 through the live caller. The ceiling check (ADR-013) already
    refused this, but silently; with the SoD service wired in the attempt
    also lands in sod_event, which is what IAM-064's audit report is built
    from. The refusal is typed as a SoD failure rather than an escalation,
    because that is the requirement it violates.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    sod_repository = InMemorySodRepository()
    sod_repository.set_active_user_count(world.acme, 5)
    service = AuthorizationService(world.repository, sod=SegregationOfDutiesService(sod_repository))

    with pytest.raises(SegregationOfDutiesError) as raised:
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=world.user,  # themselves
            role_id=world.repository.role_id("Accountant"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    assert raised.value.decision.rule is SodRule.NO_SELF_GRANT
    assert len(sod_repository.events) == 1
    assert sod_repository.events[0].outcome == "blocked"
    assert sod_repository.events[0].actor_user_id == world.user


async def test_granting_someone_else_still_fails_as_an_escalation_not_a_sod_breach() -> None:
    """The two controls stay distinct: handing an unheld permission to a
    COLLEAGUE is IAM-036's ceiling, not IAM-063's self-grant rule, and the
    error says so.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    sod_repository = InMemorySodRepository()
    sod_repository.set_active_user_count(world.acme, 5)
    service = AuthorizationService(world.repository, sod=SegregationOfDutiesService(sod_repository))

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=uuid.uuid4(),
            role_id=world.repository.role_id("Accountant"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    assert sod_repository.events == []


async def test_a_single_user_organization_still_cannot_self_grant_beyond_the_ceiling() -> None:
    """IAM-065 exempts a one-person organization from SoD, so the SoD rule
    stops objecting - but IAM-036's ceiling is not an SoD rule and still
    refuses. The two controls overlap deliberately, and this is the case
    that shows the overlap matters.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    sod_repository = InMemorySodRepository()
    sod_repository.set_active_user_count(world.acme, 1)
    service = AuthorizationService(world.repository, sod=SegregationOfDutiesService(sod_repository))

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=world.user,
            role_id=world.repository.role_id("Accountant"),
            scope_type="administration",
            scope_id=world.acme_books,
        )


async def test_an_organization_scoped_admin_grant_authorizes_composition() -> None:
    """Sanity check on the authority gate itself: `manage user_role` is an
    organization-level permission, and the Organization Admin holds it at
    the organization, so the check must pass there.
    """
    world, service = _admin_world()

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="manage",
            resource_type="user_role",
            target=OrganizationScope(world.acme),
        )
    )

    assert decision.allowed

"""Firm staff access: IAM-107 and IAM-108 (PRD §8.6).

    IAM-107  Firm staff access is granted per client administration, not per
             firm. A new firm employee starts with access to zero clients.
    IAM-108  Firm staff grants may carry an expiry, and support seasonal or
             interim staff working on a defined client set for a defined
             period.

Two halves. The "zero clients" half is a claim about what a firm employee
can reach with nothing granted, so it is tested by hiring someone into every
firm-level role available and asserting they still reach nothing. The
per-administration half is tested by granting access and checking it lands
on exactly the administration named and nowhere adjacent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.authz.firm_staff import (
    FirmStaffAccessService,
    NoActiveEngagementError,
    NotPermittedToAssignStaffError,
)
from api.authz.model import AdministrationScope, AuthorizationRequest, OrganizationScope
from api.authz.service import AuthorizationService
from api.authz.sod import SegregationOfDutiesService
from tests.authz.helpers import World, build_world
from tests.support.fake_firm_staff_repository import InMemoryFirmStaffRepository
from tests.support.fake_sod_repository import InMemorySodRepository


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _firm_world() -> tuple[World, FirmStaffAccessService, AuthorizationService, _FakeClock]:
    """klaver (a firm) with an active engagement on acme_books, and a Firm
    Manager who can assign staff to clients.
    """
    world = build_world()
    clock = _FakeClock()
    sod_repository = InMemorySodRepository()
    sod_repository.set_active_user_count(world.klaver, 8)
    firm_staff = FirmStaffAccessService(
        InMemoryFirmStaffRepository(world.repository, firm_organization_id=world.klaver),
        sod=SegregationOfDutiesService(sod_repository, clock=clock),
        clock=clock,
    )
    return world, firm_staff, AuthorizationService(world.repository, clock=clock), clock


def _manager(world: World) -> uuid.UUID:
    manager = uuid.uuid4()
    world.repository.assign(user_id=manager, role="Firm Manager", scope_id=world.klaver)
    return manager


async def _can_post(service: AuthorizationService, user: uuid.UUID, admin: uuid.UUID) -> bool:
    decision = await service.authorize(
        AuthorizationRequest(
            user_id=user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(admin),
        )
    )
    return decision.allowed


# ===========================================================================
# IAM-107: a new firm employee starts with access to zero clients
# ===========================================================================


@pytest.mark.parametrize(
    "firm_role", ["Firm Manager", "Organization Admin", "Owner", "Security Admin"]
)
async def test_a_new_firm_employee_reaches_no_client_however_senior(firm_role: str) -> None:
    """The strongest form of "zero clients": hire them into the most
    powerful role the FIRM has, including Owner of the firm itself, and they
    still reach nothing on a client's books.

    This holds because an organization-scoped grant cascades only to the
    administrations that organization OWNS (ADR-011), and a firm never owns
    its client's administration - not because of a check that could be
    forgotten.
    """
    world, _, service, _ = _firm_world()
    employee = uuid.uuid4()
    world.repository.assign(user_id=employee, role=firm_role, scope_id=world.klaver)

    for permission in (
        ("post", "journal_entry"),
        ("view", "administration"),
        ("view", "document"),
        ("export", "report_data"),
        ("read", "audit_log"),
    ):
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=employee,
                action=permission[0],
                resource_type=permission[1],
                target=AdministrationScope(world.acme_books),
            )
        )
        assert decision.denied, f"{firm_role} reached the client via {permission}"


async def test_a_firm_employee_with_no_grants_at_all_reaches_nothing() -> None:
    world, _, service, _ = _firm_world()

    assert not await _can_post(service, uuid.uuid4(), world.acme_books)


async def test_the_firms_engagement_alone_grants_its_staff_nothing() -> None:
    """An active engagement is the client's consent for the firm to work on
    the books; it is a tenancy relationship, not an authorization one. Staff
    still need an individual grant.
    """
    world, _, service, _ = _firm_world()
    employee = _manager(world)

    assert world.repository.has_engagement(
        firm_organization_id=world.klaver, administration_id=world.acme_books
    )
    assert not await _can_post(service, employee, world.acme_books)


# ===========================================================================
# IAM-107: granted per client administration
# ===========================================================================


async def test_access_is_granted_to_the_named_administration_only() -> None:
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    staff = uuid.uuid4()

    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=staff,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    assert await _can_post(service, staff, world.acme_books)
    # acme_holding belongs to the same client and the firm has no engagement
    # on it - "per client administration" means exactly that.
    assert not await _can_post(service, staff, world.acme_holding)


async def test_access_cannot_be_granted_without_an_active_engagement() -> None:
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    with pytest.raises(NoActiveEngagementError):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=uuid.uuid4(),
            administration_ids=[world.acme_holding],
            role_name="Accountant",
        )


async def test_granting_requires_manage_user_role_at_the_firm() -> None:
    """§8.4 gives "assigning firm staff to clients" to the Firm Manager. An
    accountant, however senior on the books, does not allocate staff.
    """
    world, firm_staff, _, _ = _firm_world()
    accountant = uuid.uuid4()
    world.repository.assign(
        user_id=accountant,
        role="Accountant",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
    )

    with pytest.raises(NotPermittedToAssignStaffError):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=accountant,
            staff_user_id=uuid.uuid4(),
            administration_ids=[world.acme_books],
            role_name="Accountant",
        )


async def test_an_organization_scope_role_cannot_be_granted_per_client() -> None:
    """IAM-107's "not per firm" as a type error: an organization-scoped role
    has no per-client meaning, and granting one would reach the firm's own
    books rather than the client's.
    """
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    with pytest.raises(ValueError, match="organization-scope role"):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=uuid.uuid4(),
            administration_ids=[world.acme_books],
            role_name="Organization Admin",
        )


async def test_a_firm_manager_cannot_grant_themselves_a_client_role() -> None:
    """IAM-063 survives the engagement bound. §8.4's Firm Manager "cannot
    post to a client's ledger without also holding Accountant on it" - and
    they cannot hand themselves Accountant, because that is granting
    themselves a permission they do not hold.
    """
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    with pytest.raises(NotPermittedToAssignStaffError):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=manager,
            administration_ids=[world.acme_books],
            role_name="Accountant",
        )


async def test_a_firm_manager_can_grant_a_colleague_a_role_they_lack() -> None:
    """The counterpart, and the reason the IAM-036 ceiling cannot apply
    here: allocating staff to clients IS the Firm Manager's job, and they
    hold no ledger permissions themselves.
    """
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    colleague = uuid.uuid4()

    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=colleague,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    assert await _can_post(service, colleague, world.acme_books)
    assert not await _can_post(service, manager, world.acme_books)


async def test_a_grant_on_a_client_confers_nothing_at_the_clients_organization() -> None:
    """Firm staff work inside the administration; they do not become members
    of the client organization.
    """
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    staff = uuid.uuid4()
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=staff,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=staff,
            action="manage",
            resource_type="user_role",
            target=OrganizationScope(world.acme),
        )
    )

    assert decision.denied


# ===========================================================================
# IAM-108: expiry, and a defined client set for a defined period
# ===========================================================================


async def test_a_seasonal_grant_covers_a_defined_client_set() -> None:
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    interim = uuid.uuid4()
    second_client = uuid.uuid4()
    world.repository.add_administration(second_client, owned_by=uuid.uuid4())
    world.repository.engage_firm(firm_organization_id=world.klaver, administration_id=second_client)

    assignments = await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=interim,
        administration_ids=[world.acme_books, second_client],
        role_name="Bookkeeper",
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert len(assignments) == 2
    assert await _can_post(service, interim, world.acme_books)
    assert await _can_post(service, interim, second_client)


async def test_a_seasonal_grant_lapses_with_no_administrative_action() -> None:
    """IAM-108 with IAM-035: the expiry is a predicate in the evaluation
    query, so nothing runs between the season ending and access stopping.
    """
    world, firm_staff, service, clock = _firm_world()
    manager = _manager(world)
    interim = uuid.uuid4()
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=interim,
        administration_ids=[world.acme_books],
        role_name="Bookkeeper",
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert await _can_post(service, interim, world.acme_books)

    clock.advance(days=120)

    assert not await _can_post(service, interim, world.acme_books)


async def test_a_grant_without_an_expiry_does_not_lapse() -> None:
    """ "May carry an expiry" - permanent staff do not need one."""
    world, firm_staff, service, clock = _firm_world()
    manager = _manager(world)
    permanent = uuid.uuid4()
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=permanent,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    clock.advance(days=3650)

    assert await _can_post(service, permanent, world.acme_books)


async def test_an_already_expired_grant_is_refused_rather_than_written() -> None:
    """A grant that confers nothing from the moment it is made is a mistake,
    and one that would look like access in a listing until someone checked
    the date.
    """
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    with pytest.raises(ValueError, match="already expired"):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=uuid.uuid4(),
            administration_ids=[world.acme_books],
            role_name="Accountant",
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )


async def test_a_client_set_is_validated_in_full_before_anything_is_written() -> None:
    """ "A defined client set for a defined period" is one decision. A set
    where the engagement on one member has lapsed must not leave the others
    granted - that would be a different, silently smaller decision nobody
    made.
    """
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    interim = uuid.uuid4()

    with pytest.raises(NoActiveEngagementError):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=interim,
            # The second has no engagement.
            administration_ids=[world.acme_books, world.acme_holding],
            role_name="Bookkeeper",
        )

    assert not await _can_post(service, interim, world.acme_books)
    assert (
        await firm_staff.list_access(firm_organization_id=world.klaver, staff_user_id=interim) == []
    )


async def test_an_empty_client_set_is_refused() -> None:
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    with pytest.raises(ValueError, match="no administrations"):
        await firm_staff.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=uuid.uuid4(),
            administration_ids=[],
            role_name="Accountant",
        )


# ===========================================================================
# Listing and revocation
# ===========================================================================


async def test_the_firm_can_see_which_clients_an_employee_reaches() -> None:
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)
    staff = uuid.uuid4()
    expiry = datetime(2026, 9, 1, tzinfo=UTC)
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=staff,
        administration_ids=[world.acme_books],
        role_name="Accountant",
        expires_at=expiry,
    )

    grants = await firm_staff.list_access(firm_organization_id=world.klaver, staff_user_id=staff)

    assert len(grants) == 1
    assert grants[0].administration_id == world.acme_books
    assert grants[0].role_name == "Accountant"
    assert grants[0].expires_at == expiry
    assert grants[0].granted_by_user_id == manager


async def test_an_expired_grant_disappears_from_the_listing() -> None:
    world, firm_staff, _, clock = _firm_world()
    manager = _manager(world)
    staff = uuid.uuid4()
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=staff,
        administration_ids=[world.acme_books],
        role_name="Accountant",
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    clock.advance(days=120)

    assert (
        await firm_staff.list_access(firm_organization_id=world.klaver, staff_user_id=staff) == []
    )


async def test_revoking_access_takes_a_staff_member_off_one_client() -> None:
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    staff = uuid.uuid4()
    second_client = uuid.uuid4()
    world.repository.add_administration(second_client, owned_by=uuid.uuid4())
    world.repository.engage_firm(firm_organization_id=world.klaver, administration_id=second_client)
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=staff,
        administration_ids=[world.acme_books, second_client],
        role_name="Accountant",
    )

    revoked = await firm_staff.revoke_access(
        firm_organization_id=world.klaver,
        revoker_user_id=manager,
        staff_user_id=staff,
        administration_id=world.acme_books,
    )

    assert revoked == 1
    assert not await _can_post(service, staff, world.acme_books)
    assert await _can_post(service, staff, second_client)


async def test_revoking_a_firms_staff_does_not_touch_the_clients_own_users() -> None:
    """The provenance column earning its place: the client's own bookkeeper
    holds an identically-shaped row on the same administration, and a firm
    tidying up its staff must not revoke it.
    """
    world, firm_staff, service, _ = _firm_world()
    manager = _manager(world)
    shared_user = uuid.uuid4()
    # The client's own grant, made by the client.
    world.repository.assign(user_id=shared_user, role="Bookkeeper", scope_id=world.acme_books)
    # And a firm grant to the same person - unusual, but the case that would
    # break a revocation keyed on user and administration alone.
    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=shared_user,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    revoked = await firm_staff.revoke_access(
        firm_organization_id=world.klaver,
        revoker_user_id=manager,
        staff_user_id=shared_user,
        administration_id=world.acme_books,
    )

    assert revoked == 1
    # The client's own Bookkeeper grant survives.
    assert await _can_post(service, shared_user, world.acme_books)


async def test_revoking_requires_manage_user_role_at_the_firm() -> None:
    world, firm_staff, _, _ = _firm_world()

    with pytest.raises(NotPermittedToAssignStaffError):
        await firm_staff.revoke_access(
            firm_organization_id=world.klaver,
            revoker_user_id=uuid.uuid4(),
            staff_user_id=uuid.uuid4(),
            administration_id=world.acme_books,
        )


async def test_revoking_access_nobody_holds_is_not_an_error() -> None:
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)

    revoked = await firm_staff.revoke_access(
        firm_organization_id=world.klaver,
        revoker_user_id=manager,
        staff_user_id=uuid.uuid4(),
        administration_id=world.acme_books,
    )

    assert revoked == 0


# ===========================================================================
# Provenance
# ===========================================================================


def test_the_repository_refuses_a_firm_grant_with_no_engagement() -> None:
    """Bypassing FirmStaffAccessService entirely. The fake mirrors
    role_assignment_firm_staff_guard_trg, so the refusal holds for a caller
    that writes the assignment directly - which is what the real trigger
    guarantees for psql and for any future service that does.
    """
    world = build_world()
    unengaged_firm = uuid.uuid4()

    with pytest.raises(ValueError, match="no active engagement"):
        world.repository.assign(
            user_id=uuid.uuid4(),
            role="Accountant",
            scope_id=world.acme_books,
            granted_by_organization_id=unengaged_firm,
        )


def test_the_repository_allows_the_client_granting_within_its_own_administration() -> None:
    """The engagement requirement applies only to grants made from another
    organization. A client granting on its own books needs no engagement
    with itself.
    """
    world = build_world()

    assignment_id = world.repository.assign(
        user_id=uuid.uuid4(),
        role="Bookkeeper",
        scope_id=world.acme_books,
        granted_by_organization_id=world.acme,
    )

    assert assignment_id is not None


async def test_a_firm_grant_is_attributed_to_the_firm_not_the_client() -> None:
    """What makes a firm staff grant distinguishable from the client's own,
    and therefore what makes the client access profile cap correct
    (IAM-100). Both rows are administration-scoped on the same
    administration; only the granting organization tells them apart.
    """
    world, firm_staff, _, _ = _firm_world()
    manager = _manager(world)
    firm_user, client_user = uuid.uuid4(), uuid.uuid4()

    await firm_staff.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=firm_user,
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )
    world.repository.assign(user_id=client_user, role="Bookkeeper", scope_id=world.acme_books)

    firm_grants = await world.repository.live_grants(
        user_id=firm_user,
        action="post",
        resource_type="journal_entry",
        now=datetime(2026, 6, 1, tzinfo=UTC),
    )
    client_grants = await world.repository.live_grants(
        user_id=client_user,
        action="post",
        resource_type="journal_entry",
        now=datetime(2026, 6, 1, tzinfo=UTC),
    )

    assert firm_grants[0].granted_by_organization_id == world.klaver
    assert client_grants[0].granted_by_organization_id == world.acme

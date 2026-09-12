"""IAM-105, the client rights floor.

    "Regardless of profile, the client's Owner always retains: read access to
     their own source documents and filed returns, export of their complete
     data, visibility of their own audit log, the ability to revoke the
     firm's access, and the ability to manage their own users' sign-in
     security. No firm setting can remove these."

The bulk of this module is ATTACKS. Each one takes an available mechanism
for removing access - a profile, a restriction, a condition, an expiry, a
revocation, a role edit, a cross-tenant write - points it at a floor right,
and asserts it fails. A test that only checks the floor works when nothing
is trying to remove it would prove nothing about a requirement whose whole
content is "cannot be removed".

Database-level paths (revoking the last Owner through raw SQL, an Owner
grant with an expiry, ops tooling holding BYPASSRLS) are attacked in
tests/integration/test_authorization_isolation.py, where a real Postgres
runs the triggers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from api.authz.matrix import ROLES, permission_catalogue, permissions_for_role
from api.authz.model import AdministrationScope, AuthorizationRequest, OrganizationScope
from api.authz.profiles import (
    BUILTIN_PROFILES,
    ClientAccessProfileService,
    ProfileRestrictions,
    resolve,
)
from api.authz.rights_floor import (
    ADMINISTRATION_FLOOR,
    CLIENT_RIGHTS_FLOOR,
    FLOOR_RIGHTS,
    ORGANIZATION_FLOOR,
    OWNER_ROLE_NAME,
    floor_phrase,
)
from api.authz.service import (
    AuthorizationService,
    ClientRightsFloorError,
    PrivilegeEscalationError,
)
from tests.authz.helpers import World, build_world
from tests.support.fake_profile_repository import InMemoryProfileRepository

STAFF = uuid.uuid4()


class _SilentNotifier:
    async def notify_client_owner(self, change: object) -> None:
        return None


async def _client_owner_world(
    *, profile: str | None = "Capture only"
) -> tuple[AuthorizationService, World, InMemoryProfileRepository]:
    """A client Owner at acme, whose administration the firm (klaver) has
    capped with the most restrictive built-in profile.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={p for _, _, perms, _ in BUILTIN_PROFILES for p in perms},
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())
    if profile is not None:
        await profile_service.assign_profile(
            firm_organization_id=world.klaver,
            administration_id=world.acme_books,
            profile_id=profiles.profile_id(profile),
            assigned_by_user_id=STAFF,
        )
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    return (
        AuthorizationService(world.repository, profiles=profile_service),
        world,
        profiles,
    )


async def _allows(service: AuthorizationService, world: World, permission: tuple[str, str]) -> bool:
    action, resource_type = permission
    scope = "organization" if permission in ORGANIZATION_FLOOR else "administration"
    target = (
        OrganizationScope(world.acme)
        if scope == "organization"
        else AdministrationScope(world.acme_books)
    )
    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user, action=action, resource_type=resource_type, target=target
        )
    )
    return decision.allowed


# ===========================================================================
# The floor is the requirement's own list, in the requirement's own words
# ===========================================================================


def test_every_clause_of_iam_105_has_a_permission() -> None:
    """Pins the floor to the six things §8.6 enumerates. A clause dropped
    during a refactor would leave a right nothing defends.
    """
    phrases = {right.phrase for right in FLOOR_RIGHTS}

    assert phrases == {
        "read access to their own source documents",
        "read access to their filed returns",
        "export of their complete data",
        "visibility of their own audit log",
        "the ability to revoke the firm's access",
        "the ability to manage their own users' sign-in security",
    }


def test_every_floor_permission_exists_in_the_catalogue() -> None:
    """A floor right naming a permission nothing grants would be a right
    that silently never applies.
    """
    catalogue = {permission.key for permission, _ in permission_catalogue()}

    for right in FLOOR_RIGHTS:
        assert right.permission in catalogue, f"{right.phrase}: {right.permission}"


def test_every_floor_permission_is_declared_at_the_scope_it_lives_at() -> None:
    scopes = {permission.key: scope for permission, scope in permission_catalogue()}

    for right in FLOOR_RIGHTS:
        assert scopes[right.permission] == right.scope, right.phrase


def test_the_owner_role_actually_holds_every_floor_permission() -> None:
    """The floor is the Owner's to retain, so the Owner role must carry it
    in the first place.
    """
    owner = next(role for role in ROLES if role.name == OWNER_ROLE_NAME)

    assert permissions_for_role(owner) >= CLIENT_RIGHTS_FLOOR


def test_the_two_scopes_partition_the_floor() -> None:
    assert ADMINISTRATION_FLOOR | ORGANIZATION_FLOOR == CLIENT_RIGHTS_FLOOR
    assert not (ADMINISTRATION_FLOOR & ORGANIZATION_FLOOR)


async def test_the_client_owner_holds_every_floor_right_under_the_tightest_profile() -> None:
    """The baseline the attacks below are measured against: all six, under
    'Capture only', which switches reports, bank detail and period editing
    off.
    """
    service, world, _ = await _client_owner_world()

    for right in FLOOR_RIGHTS:
        assert await _allows(service, world, right.permission), right.phrase


# ===========================================================================
# Attack: through a client access profile
# ===========================================================================


@pytest.mark.parametrize("profile_name", [name for name, _, _, _ in BUILTIN_PROFILES])
async def test_no_builtin_profile_removes_any_floor_right(profile_name: str) -> None:
    service, world, _ = await _client_owner_world(profile=profile_name)

    for right in FLOOR_RIGHTS:
        assert await _allows(service, world, right.permission), (
            f"{profile_name} removed: {right.phrase}"
        )


async def test_a_custom_profile_containing_nothing_removes_no_floor_right() -> None:
    """The most hostile profile a firm can write: one permission, chosen so
    the profile is valid, and nothing else. The client Owner keeps all six.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={("view", "administration")},
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())
    profile_id = await profile_service.create_profile(
        firm_organization_id=world.klaver,
        name="Nothing At All",
        description="",
        created_by_user_id=STAFF,
    )
    await profile_service.publish_version(
        firm_organization_id=world.klaver,
        profile_id=profile_id,
        summary="Client can see the administration and nothing else.",
        permissions=frozenset({("view", "administration")}),
        published_by_user_id=STAFF,
    )
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository, profiles=profile_service)

    for right in FLOOR_RIGHTS:
        assert await _allows(service, world, right.permission), right.phrase


async def test_a_profile_cannot_contain_the_organization_scoped_floor_rights() -> None:
    """The structural reason two of the six are beyond a firm's reach: a
    profile governs one administration and cannot hold an
    organization-scope permission at all.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={("view", "administration")},
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())
    profile_id = await profile_service.create_profile(
        firm_organization_id=world.klaver,
        name="Reaching Too Far",
        description="",
        created_by_user_id=STAFF,
    )

    for permission in ORGANIZATION_FLOOR:
        with pytest.raises(ValueError, match="organization-scope"):
            await profile_service.publish_version(
                firm_organization_id=world.klaver,
                profile_id=profile_id,
                summary="Trying to control the client's own organization.",
                permissions=frozenset({("view", "administration"), permission}),
                published_by_user_id=STAFF,
            )


# ===========================================================================
# Attack: through IAM-104 restrictions
# ===========================================================================


def test_switching_reports_off_does_not_remove_the_owners_export() -> None:
    """The restriction that comes closest: reports_visible=False withholds
    `export report_data`, which is IAM-105's "export of their complete
    data". The floor is applied AFTER restrictions, so it survives.
    """
    version = _version(
        permissions=frozenset(ADMINISTRATION_FLOOR),
        restrictions=ProfileRestrictions(reports_visible=False),
    )

    for_staff = resolve(version)
    for_owner = resolve(version, is_client_owner=True)

    assert ("export", "report_data") not in for_staff.permissions
    assert for_owner.permissions >= ADMINISTRATION_FLOOR


def test_no_combination_of_restrictions_removes_a_floor_right() -> None:
    version = _version(
        permissions=frozenset(ADMINISTRATION_FLOOR),
        restrictions=ProfileRestrictions(
            bank_detail_visible=False,
            reports_visible=False,
            periods_editable=False,
            visible_journal_ids=(),
            approval_amount_ceiling=Decimal("0.00"),
        ),
    )

    assert resolve(version, is_client_owner=True).permissions >= ADMINISTRATION_FLOOR


async def test_a_journal_restriction_does_not_block_a_floor_right() -> None:
    """IAM-104 restrictions become IAM-033 conditions, which are evaluated
    against request attributes - and a request for a floor right carries
    none. The floor short-circuits before conditions are consulted at all,
    so an empty journal allowlist cannot lock the Owner out of their own
    documents.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={p for _, _, perms, _ in BUILTIN_PROFILES for p in perms},
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())
    profile_id = await profile_service.create_profile(
        firm_organization_id=world.klaver,
        name="No Journals",
        description="",
        created_by_user_id=STAFF,
    )
    await profile_service.publish_version(
        firm_organization_id=world.klaver,
        profile_id=profile_id,
        summary="No journals visible at all.",
        permissions=frozenset(ADMINISTRATION_FLOOR),
        restrictions=ProfileRestrictions(visible_journal_ids=()),
        published_by_user_id=STAFF,
    )
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository, profiles=profile_service)

    for right in FLOOR_RIGHTS:
        assert await _allows(service, world, right.permission), right.phrase


# ===========================================================================
# Attack: through the Owner's own grant
# ===========================================================================


def test_an_owner_assignment_cannot_carry_an_expiry() -> None:
    """The most silent removal available: a grant that lapses on a date
    nobody remembers, taking the floor with it and requiring no action at
    all.
    """
    world = build_world()

    with pytest.raises(ValueError, match="expiry"):
        world.repository.assign(
            user_id=world.user,
            role="Owner",
            scope_id=world.acme,
            expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        )


def test_an_owner_assignment_cannot_carry_attribute_conditions() -> None:
    """A condition on the Owner's own grant could withhold a floor right
    without anyone touching a profile - an ip_allowlist that matches
    nothing, for instance.
    """
    world = build_world()

    with pytest.raises(ValueError, match="conditions"):
        world.repository.assign(
            user_id=world.user,
            role="Owner",
            scope_id=world.acme,
            conditions={"ip_allowlist": ["203.0.113.0/24"]},
        )


async def test_the_last_owner_cannot_be_revoked() -> None:
    """An organization with no Owner has nobody holding the floor, so the
    revocation that would produce one is refused - even to an Owner acting
    entirely within their permissions, because the floor is not theirs to
    remove either.
    """
    world = build_world()
    assignment_id = world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    with pytest.raises(ClientRightsFloorError, match="last Owner"):
        await service.revoke_assignment(revoker_user_id=world.user, assignment_id=assignment_id)


async def test_an_owner_can_be_revoked_once_another_holds_the_role() -> None:
    """The floor requires AN Owner, not a specific one - handover has to
    stay possible or the requirement would freeze an organization's
    ownership forever.
    """
    world = build_world()
    first = world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    successor = uuid.uuid4()
    world.repository.assign(user_id=successor, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    await service.revoke_assignment(revoker_user_id=world.user, assignment_id=first)

    assert await world.repository.count_active_owners(world.acme) == 1
    assert await world.repository.is_organization_owner(
        user_id=successor, organization_id=world.acme
    )


async def test_revoking_down_to_the_last_owner_then_that_one_is_refused() -> None:
    """Two Owners, revoked one at a time. The first succeeds, the second is
    refused - the guard is on the state at the moment of revocation, not on
    a count taken once.
    """
    world = build_world()
    first = world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    successor = uuid.uuid4()
    second = world.repository.assign(user_id=successor, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    await service.revoke_assignment(revoker_user_id=world.user, assignment_id=first)

    with pytest.raises(ClientRightsFloorError):
        await service.revoke_assignment(revoker_user_id=successor, assignment_id=second)


async def test_the_repository_refuses_the_last_owner_revocation_directly() -> None:
    """Bypassing the service entirely. The fake mirrors
    role_assignment_owner_floor_guard_trg, so the refusal holds for a caller
    that never goes through AuthorizationService - which is what the real
    trigger guarantees for psql and the operator scripts.
    """
    world = build_world()
    assignment_id = world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)

    with pytest.raises(ValueError, match="last Owner"):
        await world.repository.revoke_assignment(assignment_id=assignment_id, at=datetime.now(UTC))


# ===========================================================================
# Attack: through role composition and assignment
# ===========================================================================


async def test_a_firm_cannot_compose_a_role_that_strips_floor_rights() -> None:
    """Roles only ever ADD permissions - there is no "role that removes a
    permission" in this model - so the closest available attack is composing
    a diminished Owner-like role and assigning it. It changes nothing: the
    floor comes from holding the Owner ROLE, and a look-alike is not it.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    profiles = InMemoryProfileRepository()
    service = AuthorizationService(
        world.repository, profiles=ClientAccessProfileService(profiles, _SilentNotifier())
    )

    await service.create_custom_role(
        author_user_id=world.user,
        organization_id=world.acme,
        name="Owner But Less",
        scope_type="organization",
        permissions=[("view", "administration")],
    )

    for right in FLOOR_RIGHTS:
        assert await _allows(service, world, right.permission), right.phrase


async def test_the_floor_follows_the_owner_role_not_a_permission() -> None:
    """A custom role holding every floor permission does NOT confer the
    floor on its holder: the floor is checked by role, so it cannot be
    arranged by composing a role that happens to hold the right
    permissions (the same reasoning as IAM-064's Owner acknowledgement).
    """
    world = build_world()
    owner = uuid.uuid4()
    world.repository.assign(user_id=owner, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    look_alike = await service.create_custom_role(
        author_user_id=owner,
        organization_id=world.acme,
        name="Floor Holder",
        scope_type="organization",
        permissions=sorted(CLIENT_RIGHTS_FLOOR),
    )
    await service.assign_role(
        granter_user_id=owner,
        subject_user_id=world.user,
        role_id=look_alike,
        scope_type="organization",
        scope_id=world.acme,
    )

    assert not await world.repository.is_organization_owner(
        user_id=world.user, organization_id=world.acme
    )


# ===========================================================================
# Attack: from another tenant
# ===========================================================================


async def test_a_firms_owner_does_not_inherit_the_floor_on_a_client() -> None:
    """The floor is granted at the organization that OWNS the target. An
    Owner at the firm is not the Owner of the client, so holding the role
    elsewhere confers nothing here.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.klaver)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="view",
            resource_type="document",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied


async def test_the_floor_does_not_reach_a_sibling_organizations_books() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    own = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="view",
            resource_type="document",
            target=AdministrationScope(world.acme_books),
        )
    )
    foreign = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="view",
            resource_type="document",
            target=AdministrationScope(world.klaver_books),
        )
    )

    assert own.allowed
    assert foreign.denied


async def test_a_user_who_is_not_an_owner_gets_nothing_from_the_floor() -> None:
    """The floor is not a blanket grant. A Bookkeeper under 'Capture only'
    is still capped by the profile.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={p for _, _, perms, _ in BUILTIN_PROFILES for p in perms},
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )
    world.repository.assign(user_id=world.user, role="Bookkeeper", scope_id=world.acme_books)
    service = AuthorizationService(world.repository, profiles=profile_service)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="export",
            resource_type="report_data",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied
    assert decision.reason == "capped_by_client_access_profile"


async def test_the_floor_grants_only_its_six_rights() -> None:
    """It is an exception to the permission model, so its blast radius has
    to be exactly the enumerated list. An Owner under 'Capture only' still
    cannot post to the ledger.
    """
    service, world, _ = await _client_owner_world()

    for permission in (
        ("post", "journal_entry"),
        ("reverse", "journal_entry"),
        ("file", "vat_return"),
        ("release", "payment_batch"),
        ("lock", "period"),
    ):
        assert permission not in CLIENT_RIGHTS_FLOOR
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=world.user,
                action=permission[0],
                resource_type=permission[1],
                target=AdministrationScope(world.acme_books),
            )
        )
        assert decision.denied, f"{permission} should still be capped"


# ===========================================================================
# The floor is not the firm's to confer either
# ===========================================================================


async def test_the_floor_is_excluded_from_the_iam_102_ceiling() -> None:
    """A firm that cannot itself read the client's audit log does not
    thereby stop the client's Owner reading it: the floor is the client's
    own, so it is not measured against what the firm holds.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    # Everything 'Capture only' needs EXCEPT `view document`, which is a
    # floor right - so if the assignment succeeds, the exclusion worked.
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={
            ("view", "administration"),
            ("upload", "document"),
            ("submit", "expense"),
        },
    )
    profile_service = ClientAccessProfileService(profiles, _SilentNotifier())

    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    access = await profile_service.effective_access(world.acme_books, is_client_owner=True)
    assert access is not None
    assert access.permissions >= ADMINISTRATION_FLOOR


async def test_an_admin_cannot_grant_themselves_the_floor_by_assignment() -> None:
    """IAM-036 still applies to the Owner role itself: an Organization Admin
    cannot hand themselves Owner to acquire the floor, because Owner
    confers far more than they hold.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=world.user,
            role_id=world.repository.role_id("Owner"),
            scope_type="organization",
            scope_id=world.acme,
        )


def test_the_floor_phrase_is_available_for_a_client_facing_explanation() -> None:
    for right in FLOOR_RIGHTS:
        assert floor_phrase(right.permission) == right.phrase

    assert floor_phrase(("post", "journal_entry")) is None


def _version(
    *, permissions: frozenset[tuple[str, str]], restrictions: ProfileRestrictions
) -> object:
    from api.authz.profiles import ProfileVersion

    return ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=permissions,
        restrictions=restrictions,
    )

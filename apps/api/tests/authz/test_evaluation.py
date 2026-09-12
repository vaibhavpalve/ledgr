"""IAM-031 (default deny), IAM-034 (evaluated per request against current
state) and IAM-035 (expiring grants) against the real
AuthorizationService, backed by the in-memory repository fake - no database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from api.authz.model import AdministrationScope, AuthorizationRequest, OrganizationScope
from api.authz.service import AuthorizationService
from tests.authz.helpers import build_world


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


async def test_a_user_with_no_grants_at_all_is_denied() -> None:
    world = build_world()
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied
    assert decision.reason == "no_matching_grant"


async def test_a_matching_grant_allows_and_names_the_role_that_admitted_it() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.allowed
    assert decision.reason == "allowed"
    assert decision.detail is not None and "Accountant" in decision.detail


async def test_holding_a_role_does_not_confer_permissions_outside_its_bundle() -> None:
    """A Bookkeeper posts but does not approve. Appendix A's "—" cells are
    not a separate deny rule; the permission is simply absent from the
    bundle, and absence is denial (IAM-031).
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Bookkeeper", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    posts = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )
    approves = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="approve",
            resource_type="purchase_invoice",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert posts.allowed
    assert approves.denied
    assert approves.reason == "no_matching_grant"


async def test_an_unknown_action_or_resource_type_is_denied_not_an_error() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="obliterate",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    # Not an exception: an action nobody defined is simply one nobody holds.
    # Raising here would let a caller distinguish "no such permission" from
    # "you lack it," and would turn a typo in a route declaration into a 500
    # rather than the 403 it deserves.
    assert decision.denied
    assert decision.reason == "no_matching_grant"


async def test_a_revoked_assignment_stops_working_immediately() -> None:
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Accountant",
        scope_id=world.acme_books,
        revoked_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied


async def test_an_expired_grant_stops_working_with_no_administrative_action() -> None:
    """IAM-035: "Grants may carry an expiry timestamp. Expired grants stop
    working without any administrative action."

    Nothing runs between the two calls below - no revocation job, no cache
    flush, no session invalidation. Only the clock moves.
    """
    clock = _FakeClock()
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Accountant",
        scope_id=world.acme_books,
        expires_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    service = AuthorizationService(world.repository, clock=clock)

    request = AuthorizationRequest(
        user_id=world.user,
        action="post",
        resource_type="journal_entry",
        target=AdministrationScope(world.acme_books),
    )

    assert (await service.authorize(request)).allowed

    clock.advance(days=30)

    assert (await service.authorize(request)).denied


async def test_a_decision_reflects_state_at_the_moment_it_is_asked() -> None:
    """IAM-034/IAM-037: nothing is cached between calls, so a grant added
    after a denial takes effect on the very next request - not within 60
    seconds of one, and with no token to refresh.
    """
    world = build_world()
    service = AuthorizationService(world.repository)
    request = AuthorizationRequest(
        user_id=world.user,
        action="post",
        resource_type="journal_entry",
        target=AdministrationScope(world.acme_books),
    )

    assert (await service.authorize(request)).denied

    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)

    assert (await service.authorize(request)).allowed


async def test_an_archived_role_stops_admitting_its_existing_assignments() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)
    request = AuthorizationRequest(
        user_id=world.user,
        action="post",
        resource_type="journal_entry",
        target=AdministrationScope(world.acme_books),
    )

    assert (await service.authorize(request)).allowed

    world.repository.archive_role("Accountant")

    assert (await service.authorize(request)).denied


async def test_two_roles_grant_the_union_of_what_they_permit() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Bookkeeper", scope_id=world.acme_books)
    world.repository.assign(user_id=world.user, role="Approver", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    for action, resource_type in (("post", "journal_entry"), ("approve", "purchase_invoice")):
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=world.user,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(world.acme_books),
            )
        )
        assert decision.allowed, f"{action} {resource_type} should be allowed"


async def test_one_users_grant_never_admits_another_user() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=uuid.uuid4(),
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied


async def test_an_organization_level_action_cannot_be_requested_per_administration() -> None:
    """ "Manage the security policy of administration X" is not a question
    this model answers. Silently promoting the target to the owning
    organization would be exactly the widening IAM-032 forbids, so it denies.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Security Admin", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    at_administration = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="manage",
            resource_type="security_policy",
            target=AdministrationScope(world.acme_books),
        )
    )
    at_organization = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="manage",
            resource_type="security_policy",
            target=OrganizationScope(world.acme),
        )
    )

    assert at_administration.denied
    assert at_administration.reason == "resource_scope_mismatch"
    assert at_organization.allowed

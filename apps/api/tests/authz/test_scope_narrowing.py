"""IAM-032: "Scope narrowing only: a grant at administration level cannot be
widened to organization level by any code path."

The requirement names one direction, and the first three tests below pin it
from three angles: an administration grant does not reach the owning
organization, does not reach a sibling administration, and does not reach an
administration in another organization.

The rest pin the legitimate direction - an organization grant DOES reach the
administrations that organization owns - and, most importantly, its
boundary: it does not reach an administration the organization merely has a
firm engagement with. That last case is PRD §8.4's "a Firm Manager cannot
post to a client's ledger without also holding Accountant on it," and it
holds because the cascade keys on ownership rather than on access.
"""

from __future__ import annotations

import uuid

from api.authz.model import AdministrationScope, AuthorizationRequest, OrganizationScope
from api.authz.service import AuthorizationService
from tests.authz.helpers import build_world


async def test_an_administration_grant_does_not_reach_the_owning_organization() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="submit",
            resource_type="expense",
            target=OrganizationScope(world.acme),
        )
    )

    assert decision.denied
    assert decision.reason == "scope_not_covered"


async def test_an_administration_grant_does_not_reach_a_sibling_administration() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    own = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )
    sibling = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            # Same organization, different administration.
            target=AdministrationScope(world.acme_holding),
        )
    )

    assert own.allowed
    assert sibling.denied
    assert sibling.reason == "scope_not_covered"


async def test_an_administration_grant_does_not_reach_another_organizations_books() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.klaver_books),
        )
    )

    assert decision.denied


async def test_an_organization_grant_reaches_every_administration_it_owns() -> None:
    """Appendix A gives Owner an F on "Post journal entries" while Owner is
    an organization-scoped role - so an organization grant must cascade to
    the administrations that organization owns, for the permissions its
    bundle actually contains.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    for administration in (world.acme_books, world.acme_holding):
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=world.user,
                action="post",
                resource_type="journal_entry",
                target=AdministrationScope(administration),
            )
        )
        assert decision.allowed, f"owner should reach {administration}"


async def test_an_organization_grant_carries_only_what_its_bundle_contains() -> None:
    """The cascade is about SCOPE, not about capability. An Organization
    Admin's grant reaches acme's administrations, but Appendix A gives that
    role no ledger permission, so it still cannot post there.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    submits = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="submit",
            resource_type="expense",
            target=AdministrationScope(world.acme_books),
        )
    )
    posts = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert submits.allowed
    assert posts.denied


async def test_a_firms_organization_grant_does_not_reach_a_client_administration() -> None:
    """PRD §8.4, Firm Manager: "Explicitly cannot: post to a client's ledger
    without also holding Accountant on it."

    klaver holds an active firm_engagement on acme_books (that is what lets
    it see the administration at all, per ADR-002's tenancy model), and a
    Firm Manager grant at the klaver ORGANIZATION. Neither reaches acme's
    ledger: the organization cascade follows administration.organization_id,
    and acme_books is owned by acme, not klaver.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Firm Manager", scope_id=world.klaver)
    service = AuthorizationService(world.repository)

    own_books = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="view",
            resource_type="administration",
            target=AdministrationScope(world.klaver_books),
        )
    )
    client_books = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="view",
            resource_type="administration",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert own_books.allowed
    assert client_books.denied
    assert client_books.reason == "scope_not_covered"


async def test_firm_staff_reach_a_client_only_through_an_explicit_administration_grant() -> None:
    """The other half of the same story: a firm user DOES work on a client's
    books - via an assignment scoped to that administration specifically,
    which is the only representation of firm staff access there is. And that
    grant stays narrow: it does not spread to the client's other books.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Firm Manager", scope_id=world.klaver)
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    engaged = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )
    not_engaged = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_holding),
        )
    )
    client_organization = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="manage",
            resource_type="user_role",
            target=OrganizationScope(world.acme),
        )
    )

    assert engaged.allowed
    assert not_engaged.denied
    # The Firm Manager grant is scoped to klaver; holding Accountant on a
    # client's administration confers nothing at the client's ORGANIZATION.
    assert client_organization.denied


async def test_an_unknown_administration_denies_rather_than_cascading() -> None:
    """If the owning organization cannot be resolved - the administration
    does not exist, or RLS hid it from this session - an organization grant
    must not be assumed to cover it.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(uuid.uuid4()),
        )
    )

    assert decision.denied
    assert decision.reason == "scope_not_covered"

"""A small, fixed world every authorization test reasons about, so the
interesting part of each test is the grant under examination rather than
twenty lines of setup.

The roles and permissions are the REAL ones, loaded from api.authz.matrix -
not a hand-written test-only subset. That matters for the same reason the
matrix exists at all: a test asserting "a Bookkeeper cannot approve" is only
meaningful if "Bookkeeper" here means what it means in production. A local
copy would let the tests keep passing against a role definition the PRD no
longer describes.

Two organizations and three administrations, arranged so the scope-narrowing
cases are all expressible:

    acme (business)          klaver (firm)
      |- acme_books            |- klaver_books
      |- acme_holding
                             klaver has an active firm_engagement on
                             acme_books, which grants it nothing here on
                             purpose - engagement is a tenancy relationship
                             (ADR-002), not an authorization one, and this
                             module models only what authorization can see:
                             ownership.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from api.authz.matrix import ROLES, permission_catalogue, permissions_for_role
from tests.support.fake_authz_repository import InMemoryAuthorizationRepository


@dataclass(frozen=True, slots=True)
class World:
    repository: InMemoryAuthorizationRepository
    acme: uuid.UUID = field(default_factory=uuid.uuid4)
    klaver: uuid.UUID = field(default_factory=uuid.uuid4)
    acme_books: uuid.UUID = field(default_factory=uuid.uuid4)
    acme_holding: uuid.UUID = field(default_factory=uuid.uuid4)
    klaver_books: uuid.UUID = field(default_factory=uuid.uuid4)
    user: uuid.UUID = field(default_factory=uuid.uuid4)


def build_world() -> World:
    world = World(repository=InMemoryAuthorizationRepository())
    repo = world.repository

    for permission, scope in permission_catalogue():
        repo.define_permission(permission.action, permission.resource_type, scope)

    for role in ROLES:
        repo.define_role(
            role.name,
            scope_type=role.scope_type,
            permissions=sorted(permissions_for_role(role)),
        )

    repo.add_administration(world.acme_books, owned_by=world.acme)
    repo.add_administration(world.acme_holding, owned_by=world.acme)
    repo.add_administration(world.klaver_books, owned_by=world.klaver)

    # klaver's active engagement on acme_books - the tenancy relationship
    # that makes klaver's staff firm-side there (and grants them nothing on
    # its own; see the module docstring).
    repo.engage_firm(firm_organization_id=world.klaver, administration_id=world.acme_books)

    return world

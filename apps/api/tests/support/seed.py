"""Seeds tenants for tenant-isolation tests, through the same paths
production code uses — the SECURITY DEFINER bootstrap function for
organization creation, then a normal tenant-scoped INSERT for everything
else — rather than poking rows in directly. That way a seeding bug and a
production bug would be the same bug, not two divergent code paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class SeededTenants:
    org_a: uuid.UUID
    org_b: uuid.UUID
    admin_a: uuid.UUID
    admin_b: uuid.UUID
    # One Owner per organization (IAM-030). Requests in these tests are made
    # as a real, authorized user rather than as a bare organization id,
    # because every gate in the middleware chain now needs one: MFA
    # enforcement rejects a tenant context with no user (ADR-008), and the
    # authorization library needs a subject to look grants up for.
    owner_a: uuid.UUID
    owner_b: uuid.UUID


async def signup_organization(engine: AsyncEngine, *, name: str, kvk: str) -> uuid.UUID:
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT (app.signup_self_managed_organization(:name, :kvk)).id"),
            {"name": name, "kvk": kvk},
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def signup_firm_organization(engine: AsyncEngine, *, name: str, kvk: str) -> uuid.UUID:
    """FR-ONB-001b's branch, through the same SECURITY DEFINER function
    api.auth.signup.SignupService calls for it (migration 0046)."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT (app.signup_firm_organization(:name, :kvk)).id"),
            {"name": name, "kvk": kvk},
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def seed_administration(
    engine: AsyncEngine, *, org_id: uuid.UUID, legal_name: str, legal_form: str
) -> uuid.UUID:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO administration (organization_id, legal_name, legal_form) "
                "VALUES (:org_id, :legal_name, :legal_form) RETURNING id"
            ),
            {"org_id": str(org_id), "legal_name": legal_name, "legal_form": legal_form},
        )
        return result.scalar_one()  # type: ignore[no-any-return]


#: The live session each seeded user was given, by user id - what
#: tests.support.isolation.make_token puts in `sid` when a test does not
#: name a session itself. See seed_user.
SESSION_FOR_USER: dict[uuid.UUID, uuid.UUID] = {}


async def seed_user(
    engine: AsyncEngine, *, email: str, email_verified: bool = True, with_session: bool = True
) -> uuid.UUID:
    """A user who can act: verified (IAM-010b - the posting routes refuse an
    unverified address, and an isolation test that got a 403 there would
    prove nothing about isolation) and, by default, holding one live,
    MFA-verified session.

    The session matters since api.tenancy started resolving every token's
    `sid` against `sessions` (ADR-060): a token that names no session is
    refused at the door, so a test that mints one for this user needs a row
    to name. Recording it in SESSION_FOR_USER lets make_token find it without
    every call site being told - the same real lookup runs either way, which
    is what keeps those tests meaningful rather than merely passing. A test
    that wants a specific session (revocation, the switcher) still seeds and
    names its own.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO users (email, email_verified_at) "
                "VALUES (:email, :verified_at) RETURNING id"
            ),
            {"email": email, "verified_at": datetime.now(UTC) if email_verified else None},
        )
        user_id: uuid.UUID = result.scalar_one()
    if with_session:
        SESSION_FOR_USER[user_id] = await seed_session(engine, user_id=user_id)
    return user_id


async def seed_session(
    engine: AsyncEngine, *, user_id: uuid.UUID, mfa_verified: bool = True
) -> uuid.UUID:
    """A live session row - what a token's `sid` names, and what IAM-110's
    switcher writes its active administration onto. Sessions carry no tenant
    column (users are global - 0003), so this needs no tenant context.

    MFA-verified by default, for the reason make_token defaults
    mfa_verified=True: since ADR-060 the ROW is what
    api.mfa_middleware reads, and a test about anything other than the MFA
    gate wants to get past it.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO sessions (user_id, token_hash, expires_at, mfa_verified_at) "
                "VALUES (:user_id, :token_hash, now() + interval '12 hours', :mfa_verified_at) "
                "RETURNING id"
            ),
            {
                "user_id": str(user_id),
                "token_hash": f"test-{uuid.uuid4().hex}",
                "mfa_verified_at": datetime.now(UTC) if mfa_verified else None,
            },
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def seed_trusted_device(
    engine: AsyncEngine, *, user_id: uuid.UUID, name: str | None = None
) -> uuid.UUID:
    """A live trusted_device row (ADR-061) - carries no tenant column (users
    are global - 0003), same as seed_session above.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO trusted_device (user_id, token_hash, name, expires_at) "
                "VALUES (:user_id, :token_hash, :name, now() + interval '7 days') "
                "RETURNING id"
            ),
            {"user_id": str(user_id), "token_hash": f"test-{uuid.uuid4().hex}", "name": name},
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def grant_role(
    engine: AsyncEngine,
    *,
    acting_org_id: uuid.UUID,
    user_id: uuid.UUID,
    role_name: str,
    scope_type: str,
    scope_id: uuid.UUID,
    granted_by: uuid.UUID | None = None,
    conditions: str = "{}",
    expires_at: str | None = None,
) -> uuid.UUID:
    """Grants a standard role through the same tenant-scoped INSERT the
    application uses — so RLS (role_assignment_insert) and every guard
    trigger in 0009 apply here exactly as they would in production. A
    seeding helper that bypassed them could set up state the app can never
    reach.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(acting_org_id)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO role_assignment "
                "(user_id, role_id, scope_type, scope_id, conditions, "
                " granted_by_user_id, expires_at) "
                "SELECT :user_id, r.id, :scope_type, :scope_id, cast(:conditions as jsonb), "
                "       :granted_by, :expires_at "
                'FROM "role" r WHERE r.is_system AND r.name = :role_name '
                "RETURNING id"
            ),
            {
                "user_id": str(user_id),
                "role_name": role_name,
                "scope_type": scope_type,
                "scope_id": str(scope_id),
                "conditions": conditions,
                "granted_by": str(granted_by or user_id),
                # A real datetime, not the ISO string a caller passes in -
                # asyncpg coerces a bound parameter by its Python type before
                # any SQL-side cast runs, and only accepts a string for a
                # timestamptz column when it is written as a literal directly
                # in the SQL text, not bound (the same asyncpg behaviour
                # documents.retention's own test helpers hit with 'epoch').
                "expires_at": datetime.fromisoformat(expires_at) if expires_at else None,
            },
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def seed_two_organizations_with_overlapping_data(engine: AsyncEngine) -> SeededTenants:
    """Org A and org B get the *same* legal name and legal form, and only
    different KVK numbers — deliberately look-alike, so an isolation check
    that merely compares human-readable fields could pass by accident.
    Only checking by record id (which the two orgs can never share) proves
    isolation actually holds.
    """
    org_a = await signup_organization(engine, name="Bakker Consultancy B.V.", kvk="11111111")
    org_b = await signup_organization(engine, name="Bakker Consultancy B.V.", kvk="22222222")

    admin_a = await seed_administration(
        engine, org_id=org_a, legal_name="Bakker Consultancy B.V.", legal_form="BV"
    )
    admin_b = await seed_administration(
        engine, org_id=org_b, legal_name="Bakker Consultancy B.V.", legal_form="BV"
    )

    # Look-alike users too, for the same reason the administrations are
    # look-alikes: only the ids differ.
    suffix = uuid.uuid4().hex[:8]
    owner_a = await seed_user(engine, email=f"owner+a{suffix}@example.com")
    owner_b = await seed_user(engine, email=f"owner+b{suffix}@example.com")

    await grant_role(
        engine,
        acting_org_id=org_a,
        user_id=owner_a,
        role_name="Owner",
        scope_type="organization",
        scope_id=org_a,
    )
    await grant_role(
        engine,
        acting_org_id=org_b,
        user_id=owner_b,
        role_name="Owner",
        scope_type="organization",
        scope_id=org_b,
    )

    return SeededTenants(
        org_a=org_a,
        org_b=org_b,
        admin_a=admin_a,
        admin_b=admin_b,
        owner_a=owner_a,
        owner_b=owner_b,
    )

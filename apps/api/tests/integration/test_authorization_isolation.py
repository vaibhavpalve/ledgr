"""IAM-030 - IAM-037 against a real Postgres with migrations applied.

The pure-logic suite (tests/authz/) proves the evaluation ALGORITHM is
right against an in-memory fake. This file proves the things only a database
can prove: that the guard triggers in 0009_authorization.sql actually fire,
that RLS keeps one tenant's grants invisible to another, and that the seeded
standard roles match PRD Appendix A.

The most important tests here are the ones asserting a write is REFUSED.
IAM-032's "cannot be widened by any code path" is a claim about the
database, not only about api.authz.service - if an UPDATE could widen an
existing assignment, the service's care would be irrelevant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.firm_access_repository import (
    SqlEngagementRevocationRepository,
    SqlFirmAccessRegisterRepository,
)
from api.authz.matrix import ROLES, permission_catalogue, permissions_for_role
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    OrganizationScope,
)
from api.authz.profiles import (
    BUILTIN_PROFILES,
    ClientAccessProfileService,
    builtin_profile_names,
)
from api.authz.profiles_repository import SqlProfileRepository
from api.authz.repository import SqlAuthorizationRepository
from api.authz.rights_floor import FLOOR_RIGHTS
from api.authz.service import AuthorizationService
from api.authz.sod_repository import SqlSodRepository
from api.config import settings
from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_administration,
    seed_session,
    seed_user,
)


async def _as_org(
    org_id: uuid.UUID, sql: str, params: dict[str, object] | None = None
) -> list[Any]:
    """Runs one statement in its own transaction with tenant context set,
    exactly as api.db.get_db_session does for a real request - so RLS is in
    force for everything below.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        return list((await conn.execute(text(sql), params or {})).all())


# --- the seeded catalogue matches the matrix --------------------------------
#
# The last link in the chain. tests/authz/test_appendix_a_conformance.py ties
# prd.md to api.authz.matrix, and test_role_catalogue_generation.py ties the
# matrix to migrations/0010_role_catalogue.sql. These tie that migration to
# the rows a real database actually ends up holding - the only link the other
# two cannot check, because a migration is a file until it is applied.


async def test_the_seeded_permissions_are_exactly_the_matrixs_catalogue() -> None:
    rows = await _as_org(
        uuid.uuid4(),  # any tenant: the catalogue is global reference data
        "SELECT action, resource_type, resource_scope FROM permission",
    )
    seeded = {(action, rtype, scope) for action, rtype, scope in rows}
    expected = {(p.action, p.resource_type, scope) for p, scope in permission_catalogue()}

    assert seeded == expected, (
        "the database's permission catalogue has drifted from api.authz.matrix:\n"
        f"  in the database only: {sorted(seeded - expected)}\n"
        f"  in the matrix only:   {sorted(expected - seeded)}"
    )


async def test_the_seeded_roles_are_exactly_section_8_4s_twelve() -> None:
    rows = await _as_org(uuid.uuid4(), 'SELECT name, scope_type FROM "role" WHERE is_system')
    seeded = {(name, scope) for name, scope in rows}
    expected = {(role.name, role.scope_type) for role in ROLES}

    assert len(expected) == 12
    assert seeded == expected


async def test_every_seeded_bundle_matches_appendix_a() -> None:
    """The whole matrix, asserted against real rows, role by role. A failure
    names the role and the exact permissions that differ.
    """
    rows = await _as_org(
        uuid.uuid4(),
        'SELECT r.name, p.action, p.resource_type FROM "role" r '
        "JOIN role_permission rp ON rp.role_id = r.id "
        "JOIN permission p ON p.id = rp.permission_id "
        "WHERE r.is_system",
    )

    seeded: dict[str, set[tuple[str, str]]] = {}
    for name, action, resource_type in rows:
        seeded.setdefault(name, set()).add((action, resource_type))

    mismatches: list[str] = []
    for role in ROLES:
        expected = set(permissions_for_role(role))
        actual = seeded.get(role.name, set())
        if actual != expected:
            mismatches.append(
                f"  {role.name}:\n"
                f"    seeded but not in Appendix A: {sorted(actual - expected)}\n"
                f"    in Appendix A but not seeded: {sorted(expected - actual)}"
            )

    assert not mismatches, (
        "seeded role bundles have drifted from api.authz.matrix "
        "(is 0010_role_catalogue.sql stale, or was the database migrated from an "
        "older revision?):\n" + "\n".join(mismatches)
    )


# --- RLS over grants --------------------------------------------------------


async def test_one_organizations_grants_are_invisible_to_another(
    two_organizations: SeededTenants,
) -> None:
    from_a = await _as_org(
        two_organizations.org_a,
        "SELECT id FROM role_assignment WHERE user_id = :user_id",
        {"user_id": str(two_organizations.owner_b)},
    )
    from_b = await _as_org(
        two_organizations.org_b,
        "SELECT id FROM role_assignment WHERE user_id = :user_id",
        {"user_id": str(two_organizations.owner_b)},
    )

    assert from_a == [], "org A must not see who holds what in org B"
    assert len(from_b) == 1, "org B still sees its own grant"


async def test_a_grant_cannot_be_written_onto_another_organizations_scope(
    two_organizations: SeededTenants,
) -> None:
    """role_assignment_insert's WITH CHECK. Org A cannot grant itself - or
    anyone - a role on org B, even knowing every id involved.
    """
    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_assignment "
            "(user_id, role_id, scope_type, scope_id, granted_by_user_id) "
            "SELECT :user_id, r.id, 'organization', :scope_id, :user_id "
            "FROM \"role\" r WHERE r.is_system AND r.name = 'Owner'",
            {
                "user_id": str(two_organizations.owner_a),
                "scope_id": str(two_organizations.org_b),
            },
        )


# --- IAM-032 at the database ------------------------------------------------


async def test_an_assignments_scope_cannot_be_updated_to_widen_it(
    two_organizations: SeededTenants,
) -> None:
    """The claim IAM-032 actually makes: no code path widens a grant. Here
    the code path is a direct UPDATE by the tenant that owns the row, with
    every privilege the application has - and it is still refused.
    """
    assignment_id = await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE role_assignment SET scope_type = 'organization', scope_id = :org_id "
            "WHERE id = :id",
            {"org_id": str(two_organizations.org_a), "id": str(assignment_id)},
        )


async def test_an_assignments_conditions_cannot_be_relaxed_in_place(
    two_organizations: SeededTenants,
) -> None:
    """Raising an amount ceiling is widening too. Revoke and re-grant is the
    only path, so the change is attributable to whoever made it.
    """
    assignment_id = await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Approver",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
        conditions='{"amount_ceiling": "5000.00"}',
    )

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_a,
            """UPDATE role_assignment SET conditions = '{"amount_ceiling": "9999999.00"}'
               WHERE id = :id""",
            {"id": str(assignment_id)},
        )


async def test_a_revoked_assignment_cannot_be_un_revoked(
    two_organizations: SeededTenants,
) -> None:
    assignment_id = await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Viewer",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )
    await _as_org(
        two_organizations.org_a,
        "UPDATE role_assignment SET revoked_at = now() WHERE id = :id",
        {"id": str(assignment_id)},
    )

    with pytest.raises(DBAPIError, match="un-revoked"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE role_assignment SET revoked_at = NULL WHERE id = :id",
            {"id": str(assignment_id)},
        )


async def test_an_administration_role_cannot_be_assigned_at_organization_scope(
    two_organizations: SeededTenants,
) -> None:
    """role_assignment_scope_guard_trg. Accountant is an
    administration-scope role per §8.4; assigning it across a whole
    organization would grant its ledger rights on every administration at
    once - the widening in its most direct form.
    """
    with pytest.raises(DBAPIError, match="must match"):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_assignment "
            "(user_id, role_id, scope_type, scope_id, granted_by_user_id) "
            "SELECT :user_id, r.id, 'organization', :scope_id, :user_id "
            "FROM \"role\" r WHERE r.is_system AND r.name = 'Accountant'",
            {
                "user_id": str(two_organizations.owner_a),
                "scope_id": str(two_organizations.org_a),
            },
        )


async def test_an_organization_permission_cannot_be_bundled_into_an_administration_role(
    two_organizations: SeededTenants,
) -> None:
    """role_permission_scope_guard_trg, exercised through the custom-role
    path an organization actually has access to (system roles are
    migration-owned and RLS refuses writes to them entirely).
    """
    rows = await _as_org(
        two_organizations.org_a,
        'INSERT INTO "role" (organization_id, name, scope_type, is_system) '
        "VALUES (:org_id, 'Custom Admin Role', 'administration', false) RETURNING id",
        {"org_id": str(two_organizations.org_a)},
    )
    role_id = rows[0][0]

    with pytest.raises(DBAPIError, match="organization-scope permission"):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT :role_id, p.id FROM permission p "
            "WHERE p.action = 'manage' AND p.resource_type = 'security_policy'",
            {"role_id": str(role_id)},
        )


async def test_a_system_roles_contents_cannot_be_extended_at_runtime(
    two_organizations: SeededTenants,
) -> None:
    """role_permission_insert's WITH CHECK. If an organization could add a
    permission to the shared Owner or Viewer bundle, it would be granting
    itself - and every other tenant - a capability by writing one row.
    """
    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_permission (role_id, permission_id) "
            'SELECT r.id, p.id FROM "role" r, permission p '
            "WHERE r.is_system AND r.name = 'Viewer' "
            "  AND p.action = 'post' AND p.resource_type = 'journal_entry'",
        )


# --- IAM-036 composition at the database ------------------------------------


async def _custom_role(
    org_id: uuid.UUID, name: str, scope_type: str = "administration"
) -> uuid.UUID:
    rows = await _as_org(
        org_id,
        'INSERT INTO "role" (organization_id, name, scope_type, is_system) '
        "VALUES (:org_id, :name, :scope_type, false) RETURNING id",
        {"org_id": str(org_id), "name": name, "scope_type": scope_type},
    )
    return rows[0][0]  # type: ignore[no-any-return]


async def test_a_role_cannot_include_itself(two_organizations: SeededTenants) -> None:
    role_id = await _custom_role(two_organizations.org_a, f"Self {uuid.uuid4().hex[:6]}")

    with pytest.raises(DBAPIError, match="cannot include itself"):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_component (role_id, component_role_id) VALUES (:id, :id)",
            {"id": str(role_id)},
        )


async def test_a_cycle_in_composition_is_refused(two_organizations: SeededTenants) -> None:
    """Structurally unreachable in production - role_component has no UPDATE
    or DELETE path and roles are immutable, so an edge can only point at a
    role that already existed. Asserted anyway because "impossible" here
    rests on two invariants holding at once.
    """
    suffix = uuid.uuid4().hex[:6]
    first = await _custom_role(two_organizations.org_a, f"Cycle A {suffix}")
    second = await _custom_role(two_organizations.org_a, f"Cycle B {suffix}")

    await _as_org(
        two_organizations.org_a,
        "INSERT INTO role_component (role_id, component_role_id) VALUES (:a, :b)",
        {"a": str(second), "b": str(first)},
    )

    with pytest.raises(DBAPIError, match="cycle"):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_component (role_id, component_role_id) VALUES (:a, :b)",
            {"a": str(first), "b": str(second)},
        )


async def test_composition_cannot_be_edited_after_the_fact(
    two_organizations: SeededTenants,
) -> None:
    suffix = uuid.uuid4().hex[:6]
    role_id = await _custom_role(two_organizations.org_a, f"Composed {suffix}")
    other = await _custom_role(two_organizations.org_a, f"Other {suffix}")
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO role_component (role_id, component_role_id) "
        "SELECT :id, r.id FROM \"role\" r WHERE r.is_system AND r.name = 'Viewer'",
        {"id": str(role_id)},
    )

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE role_component SET component_role_id = :other WHERE role_id = :id",
            {"other": str(other), "id": str(role_id)},
        )


async def test_an_administration_role_cannot_include_an_organization_role(
    two_organizations: SeededTenants,
) -> None:
    role_id = await _custom_role(two_organizations.org_a, f"Admin scope {uuid.uuid4().hex[:6]}")

    with pytest.raises(DBAPIError, match="organization-scope role"):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_component (role_id, component_role_id) "
            "SELECT :id, r.id FROM \"role\" r WHERE r.is_system AND r.name = 'Security Admin'",
            {"id": str(role_id)},
        )


async def test_a_system_role_cannot_be_composed_from_other_roles(
    two_organizations: SeededTenants,
) -> None:
    """If an organization could add a component to the shared Viewer role,
    every tenant's Viewer would gain whatever that component holds.
    """
    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO role_component (role_id, component_role_id) "
            'SELECT v.id, a.id FROM "role" v, "role" a '
            "WHERE v.is_system AND v.name = 'Viewer' "
            "  AND a.is_system AND a.name = 'Accountant'",
        )


async def test_another_organizations_composition_is_invisible(
    two_organizations: SeededTenants,
) -> None:
    role_id = await _custom_role(two_organizations.org_a, f"Private {uuid.uuid4().hex[:6]}")
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO role_component (role_id, component_role_id) "
        "SELECT :id, r.id FROM \"role\" r WHERE r.is_system AND r.name = 'Viewer'",
        {"id": str(role_id)},
    )

    from_a = await _as_org(
        two_organizations.org_a,
        "SELECT component_role_id FROM role_component WHERE role_id = :id",
        {"id": str(role_id)},
    )
    from_b = await _as_org(
        two_organizations.org_b,
        "SELECT component_role_id FROM role_component WHERE role_id = :id",
        {"id": str(role_id)},
    )

    assert len(from_a) == 1
    assert from_b == [], "org B must not see how org A composed its roles"


# --- IAM-033 storage --------------------------------------------------------


async def test_an_unknown_condition_key_cannot_be_stored(
    two_organizations: SeededTenants,
) -> None:
    with pytest.raises(DBAPIError, match="unknown attribute condition"):
        await grant_role(
            app_engine,
            acting_org_id=two_organizations.org_a,
            user_id=two_organizations.owner_a,
            role_name="Approver",
            scope_type="administration",
            scope_id=two_organizations.admin_a,
            conditions='{"amount_cieling": "5000.00"}',
        )


async def test_an_amount_ceiling_cannot_be_stored_as_a_json_number(
    two_organizations: SeededTenants,
) -> None:
    """NFR-031 at rest. jsonb's number type is IEEE 754 double precision, so
    a ceiling stored as a number would already be a float before any
    comparison happens.
    """
    with pytest.raises(DBAPIError, match="decimal string"):
        await grant_role(
            app_engine,
            acting_org_id=two_organizations.org_a,
            user_id=two_organizations.owner_a,
            role_name="Approver",
            scope_type="administration",
            scope_id=two_organizations.admin_a,
            conditions='{"amount_ceiling": 5000.00}',
        )


# --- IAM-060 - IAM-065 at the database --------------------------------------


async def test_a_sod_deviation_requires_a_non_blank_reason(
    two_organizations: SeededTenants,
) -> None:
    """IAM-064's "recorded with a reason", as a CHECK. A bare NOT NULL would
    accept an empty string and defeat the requirement.
    """
    for blank in ("", "   "):
        with pytest.raises(DBAPIError):
            await _as_org(
                two_organizations.org_a,
                "INSERT INTO sod_policy_deviation "
                "(organization_id, rule, acknowledged_by_user_id, reason) "
                "VALUES (:org, 'iam_062_submitter_not_approver', :user, :reason)",
                {
                    "org": str(two_organizations.org_a),
                    "user": str(two_organizations.owner_a),
                    "reason": blank,
                },
            )


async def test_only_one_deviation_per_rule_can_be_active(
    two_organizations: SeededTenants,
) -> None:
    params = {
        "org": str(two_organizations.org_a),
        "user": str(two_organizations.owner_a),
    }
    sql = (
        "INSERT INTO sod_policy_deviation "
        "(organization_id, rule, acknowledged_by_user_id, reason) "
        "VALUES (:org, 'iam_062_submitter_not_approver', :user, 'first')"
    )
    await _as_org(two_organizations.org_a, sql, params)

    with pytest.raises(DBAPIError):
        await _as_org(two_organizations.org_a, sql, params)


async def test_a_deviations_reason_cannot_be_rewritten_after_the_fact(
    two_organizations: SeededTenants,
) -> None:
    """IAM-064 wants the justification that was actually given. Editing it
    later would let an organization launder a bad reason into a good one
    after an auditor asked.
    """
    rows = await _as_org(
        two_organizations.org_a,
        "INSERT INTO sod_policy_deviation "
        "(organization_id, rule, acknowledged_by_user_id, reason) "
        "VALUES (:org, 'iam_060_creator_not_sole_approver', :user, 'the real reason') "
        "RETURNING id",
        {"org": str(two_organizations.org_a), "user": str(two_organizations.owner_a)},
    )
    deviation_id = rows[0][0]

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE sod_policy_deviation SET reason = 'a better reason' WHERE id = :id",
            {"id": str(deviation_id)},
        )


async def test_a_revoked_deviation_cannot_be_un_revoked(
    two_organizations: SeededTenants,
) -> None:
    rows = await _as_org(
        two_organizations.org_a,
        "INSERT INTO sod_policy_deviation "
        "(organization_id, rule, acknowledged_by_user_id, reason) "
        "VALUES (:org, 'iam_061_approver_not_releaser', :user, 'temporary') RETURNING id",
        {"org": str(two_organizations.org_a), "user": str(two_organizations.owner_a)},
    )
    deviation_id = rows[0][0]
    await _as_org(
        two_organizations.org_a,
        "UPDATE sod_policy_deviation SET revoked_at = now(), revoked_by_user_id = :user "
        "WHERE id = :id",
        {"user": str(two_organizations.owner_a), "id": str(deviation_id)},
    )

    with pytest.raises(DBAPIError, match="un-revoked"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE sod_policy_deviation SET revoked_at = NULL WHERE id = :id",
            {"id": str(deviation_id)},
        )


async def test_a_deviation_applied_event_must_name_its_deviation(
    two_organizations: SeededTenants,
) -> None:
    """An unattributed bypass is unauditable: the report could say a rule
    was skipped but not on whose authority.
    """
    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO sod_event "
            "(organization_id, rule, outcome, actor_user_id, resource_type, detail) "
            "VALUES (:org, 'iam_062_submitter_not_approver', 'deviation_applied', "
            "        :user, 'expense', 'no deviation named')",
            {"org": str(two_organizations.org_a), "user": str(two_organizations.owner_a)},
        )


async def test_one_organizations_sod_record_is_invisible_to_another(
    two_organizations: SeededTenants,
) -> None:
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO sod_event "
        "(organization_id, rule, outcome, actor_user_id, resource_type, detail) "
        "VALUES (:org, 'iam_062_submitter_not_approver', 'blocked', :user, "
        "        'expense', 'blocked in org A')",
        {"org": str(two_organizations.org_a), "user": str(two_organizations.owner_a)},
    )

    from_a = await _as_org(
        two_organizations.org_a, "SELECT id FROM sod_event WHERE detail = 'blocked in org A'"
    )
    from_b = await _as_org(
        two_organizations.org_b, "SELECT id FROM sod_event WHERE detail = 'blocked in org A'"
    )

    assert len(from_a) == 1
    assert from_b == [], "org B must not see org A's SoD record"


async def test_the_active_user_count_is_derived_from_live_grants(
    two_organizations: SeededTenants,
) -> None:
    """The count drives both exemptions, so an undercount would silently
    exempt an organization from rules it should be subject to. Asserted
    against the real query rather than the fake: the seeded organization has
    exactly one Owner, so it IS single-user and must report as such.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        repository = SqlSodRepository(AsyncSession(bind=conn))
        before = await repository.active_user_count(two_organizations.org_a)

    assert before == 1

    second = await seed_user(app_engine, email=f"second+{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=second,
        role_name="Bookkeeper",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
        granted_by=two_organizations.owner_a,
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        repository = SqlSodRepository(AsyncSession(bind=conn))
        after = await repository.active_user_count(two_organizations.org_a)
        owner_check = await repository.holds_owner_role(
            user_id=two_organizations.owner_a, organization_id=two_organizations.org_a
        )
        non_owner_check = await repository.holds_owner_role(
            user_id=second, organization_id=two_organizations.org_a
        )

    assert after == 2, "a user holding an administration-scoped grant counts as a member"
    assert owner_check is True
    assert non_owner_check is False


# --- IAM-100 - IAM-104 at the database --------------------------------------


async def test_the_three_builtin_profiles_are_seeded_with_their_permissions() -> None:
    rows = await _as_org(
        uuid.uuid4(),  # built-ins are visible to every tenant
        "SELECT p.name, perm.action, perm.resource_type "
        "FROM client_access_profile p "
        "JOIN client_access_profile_version v ON v.profile_id = p.id "
        "JOIN client_access_profile_permission cp ON cp.profile_version_id = v.id "
        "JOIN permission perm ON perm.id = cp.permission_id "
        "WHERE p.is_builtin",
    )

    seeded: dict[str, set[tuple[str, str]]] = {}
    for name, action, resource_type in rows:
        seeded.setdefault(name, set()).add((action, resource_type))

    expected = {name: set(permissions) for name, _, permissions, _ in BUILTIN_PROFILES}
    assert seeded == expected


async def test_a_profile_cannot_contain_an_organization_scope_permission(
    two_organizations: SeededTenants,
) -> None:
    """A profile governs access within one administration. An
    organization-scope permission in one would be meaningless - and several
    of them are IAM-105 floor rights a firm must never be able to touch.
    """
    rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false) RETURNING id",
        {"org": str(two_organizations.org_b), "name": f"P{uuid.uuid4().hex[:6]}"},
    )
    profile_id = rows[0][0]
    version_rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile_version (profile_id, version, summary) "
        "VALUES (:pid, 1, 'v1') RETURNING id",
        {"pid": str(profile_id)},
    )
    version_id = version_rows[0][0]

    with pytest.raises(DBAPIError, match="organization-scope permission"):
        await _as_org(
            two_organizations.org_b,
            "INSERT INTO client_access_profile_permission (profile_version_id, permission_id) "
            "SELECT :vid, p.id FROM permission p "
            "WHERE p.action = 'manage' AND p.resource_type = 'security_policy'",
            {"vid": str(version_id)},
        )


async def test_a_published_profile_version_is_immutable(
    two_organizations: SeededTenants,
) -> None:
    """IAM-100's "versioned" only means anything if a published version
    cannot be rewritten - otherwise "what could this client do in March" has
    no answer.
    """
    rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false) RETURNING id",
        {"org": str(two_organizations.org_b), "name": f"P{uuid.uuid4().hex[:6]}"},
    )
    version_rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile_version (profile_id, version, summary) "
        "VALUES (:pid, 1, 'the original summary') RETURNING id",
        {"pid": str(rows[0][0])},
    )

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_b,
            "UPDATE client_access_profile_version SET summary = 'rewritten' WHERE id = :id",
            {"id": str(version_rows[0][0])},
        )


async def test_a_version_requires_a_non_blank_summary(
    two_organizations: SeededTenants,
) -> None:
    """IAM-103's plain-language summary, as a CHECK."""
    rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false) RETURNING id",
        {"org": str(two_organizations.org_b), "name": f"P{uuid.uuid4().hex[:6]}"},
    )

    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_b,
            "INSERT INTO client_access_profile_version (profile_id, version, summary) "
            "VALUES (:pid, 1, '   ')",
            {"pid": str(rows[0][0])},
        )


async def test_an_unknown_profile_restriction_cannot_be_stored(
    two_organizations: SeededTenants,
) -> None:
    rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false) RETURNING id",
        {"org": str(two_organizations.org_b), "name": f"P{uuid.uuid4().hex[:6]}"},
    )

    with pytest.raises(DBAPIError, match="unknown profile restriction"):
        await _as_org(
            two_organizations.org_b,
            "INSERT INTO client_access_profile_version "
            "(profile_id, version, summary, restrictions) "
            """VALUES (:pid, 1, 'v1', '{"bank_detail_visable": false}'::jsonb)""",
            {"pid": str(rows[0][0])},
        )


async def test_an_approval_ceiling_cannot_be_stored_as_a_json_number(
    two_organizations: SeededTenants,
) -> None:
    rows = await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false) RETURNING id",
        {"org": str(two_organizations.org_b), "name": f"P{uuid.uuid4().hex[:6]}"},
    )

    with pytest.raises(DBAPIError, match="decimal string"):
        await _as_org(
            two_organizations.org_b,
            "INSERT INTO client_access_profile_version "
            "(profile_id, version, summary, restrictions) "
            """VALUES (:pid, 1, 'v1', '{"approval_amount_ceiling": 5000.00}'::jsonb)""",
            {"pid": str(rows[0][0])},
        )


async def test_only_one_profile_governs_an_administration_at_a_time(
    two_organizations: SeededTenants,
) -> None:
    async def assign(name: str) -> None:
        await _as_org(
            two_organizations.org_a,
            "INSERT INTO administration_access_profile "
            "(administration_id, profile_id, assigned_by_user_id) "
            "SELECT :admin, p.id, :user FROM client_access_profile p "
            "WHERE p.is_builtin AND p.name = :name",
            {
                "admin": str(two_organizations.admin_a),
                "user": str(two_organizations.owner_a),
                "name": name,
            },
        )

    await assign("Capture only")

    with pytest.raises(DBAPIError):
        await assign("Full self-service")


async def test_another_firms_profile_is_invisible(two_organizations: SeededTenants) -> None:
    name = f"Private {uuid.uuid4().hex[:6]}"
    await _as_org(
        two_organizations.org_b,
        "INSERT INTO client_access_profile (firm_organization_id, name, is_builtin) "
        "VALUES (:org, :name, false)",
        {"org": str(two_organizations.org_b), "name": name},
    )

    from_b = await _as_org(
        two_organizations.org_b,
        "SELECT id FROM client_access_profile WHERE name = :name",
        {"name": name},
    )
    from_a = await _as_org(
        two_organizations.org_a,
        "SELECT id FROM client_access_profile WHERE name = :name",
        {"name": name},
    )

    assert len(from_b) == 1
    assert from_a == [], "one firm must not see another firm's profiles"


async def test_the_builtin_profiles_are_visible_to_every_tenant(
    two_organizations: SeededTenants,
) -> None:
    """The inverse of the test above: built-ins are shared reference data,
    so a firm can actually select one.
    """
    for org in (two_organizations.org_a, two_organizations.org_b):
        rows = await _as_org(
            org, "SELECT name FROM client_access_profile WHERE is_builtin ORDER BY name"
        )
        assert {name for (name,) in rows} == set(builtin_profile_names())


# --- IAM-105 at the database ------------------------------------------------
#
# The paths only a real Postgres can attack. These are the ones that matter
# most for "no admin action can remove these": role_assignment_owner_floor_guard_trg
# is a TRIGGER, and BYPASSRLS - which ledgr_ops holds and the operator
# scripts connect as - skips row-level security but never skips a trigger.


async def test_the_floor_is_stated_in_the_database_in_the_prds_own_words() -> None:
    rows = await _as_org(
        uuid.uuid4(),
        "SELECT action, resource_type, resource_scope, phrase FROM client_rights_floor_statement",
    )
    stated = {(a, r): (scope, phrase) for a, r, scope, phrase in rows}

    assert len(stated) == len(FLOOR_RIGHTS)
    for right in FLOOR_RIGHTS:
        scope, phrase = stated[right.permission]
        assert scope == right.scope
        assert phrase == right.phrase


async def test_the_floor_statement_cannot_be_edited_by_the_application(
    two_organizations: SeededTenants,
) -> None:
    """The floor is not configurable, so no write path to it exists at all -
    ledgr_app holds SELECT and nothing else.
    """
    for statement in (
        "DELETE FROM client_rights_floor_statement",
        "UPDATE client_rights_floor_statement SET phrase = 'nothing'",
        "INSERT INTO client_rights_floor_statement "
        "(action, resource_type, resource_scope, phrase) "
        "VALUES ('x', 'y', 'administration', 'z')",
    ):
        with pytest.raises(DBAPIError):
            await _as_org(two_organizations.org_a, statement)


async def test_an_owner_assignment_cannot_be_created_with_an_expiry(
    two_organizations: SeededTenants,
) -> None:
    with pytest.raises(DBAPIError, match="expiry"):
        await grant_role(
            app_engine,
            acting_org_id=two_organizations.org_a,
            user_id=two_organizations.owner_a,
            role_name="Owner",
            scope_type="organization",
            scope_id=two_organizations.org_a,
            expires_at="2030-01-01T00:00:00Z",
        )


async def test_an_owner_assignment_cannot_be_created_with_conditions(
    two_organizations: SeededTenants,
) -> None:
    with pytest.raises(DBAPIError, match="attribute conditions"):
        await grant_role(
            app_engine,
            acting_org_id=two_organizations.org_a,
            user_id=two_organizations.owner_a,
            role_name="Owner",
            scope_type="organization",
            scope_id=two_organizations.org_a,
            conditions='{"ip_allowlist": ["203.0.113.0/24"]}',
        )


async def test_the_last_owner_cannot_be_revoked_by_raw_sql(
    two_organizations: SeededTenants,
) -> None:
    """The attack that bypasses every application check: an UPDATE issued
    directly, by the tenant that owns the row, with every privilege the
    application has.
    """
    rows = await _as_org(
        two_organizations.org_a,
        "SELECT ra.id FROM role_assignment ra "
        'JOIN "role" r ON r.id = ra.role_id '
        "WHERE r.is_system AND r.name = 'Owner' AND ra.scope_type = 'organization' "
        "  AND ra.scope_id = :org AND ra.revoked_at IS NULL",
        {"org": str(two_organizations.org_a)},
    )
    assert len(rows) == 1, "the seeded organization has exactly one Owner"

    with pytest.raises(DBAPIError, match="last Owner"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE role_assignment SET revoked_at = now() WHERE id = :id",
            {"id": str(rows[0][0])},
        )


async def test_owner_handover_remains_possible(two_organizations: SeededTenants) -> None:
    """The floor requires AN Owner, not a particular one. If handover were
    blocked, the requirement would freeze ownership permanently - which is
    its own kind of harm.
    """
    successor = await seed_user(app_engine, email=f"succ+{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=successor,
        role_name="Owner",
        scope_type="organization",
        scope_id=two_organizations.org_a,
        granted_by=two_organizations.owner_a,
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT ra.id FROM role_assignment ra "
        'JOIN "role" r ON r.id = ra.role_id '
        "WHERE r.is_system AND r.name = 'Owner' AND ra.scope_type = 'organization' "
        "  AND ra.scope_id = :org AND ra.user_id = :user AND ra.revoked_at IS NULL",
        {"org": str(two_organizations.org_a), "user": str(two_organizations.owner_a)},
    )

    await _as_org(
        two_organizations.org_a,
        "UPDATE role_assignment SET revoked_at = now() WHERE id = :id",
        {"id": str(rows[0][0])},
    )

    remaining = await _as_org(
        two_organizations.org_a,
        "SELECT ra.user_id FROM role_assignment ra "
        'JOIN "role" r ON r.id = ra.role_id '
        "WHERE r.is_system AND r.name = 'Owner' AND ra.scope_type = 'organization' "
        "  AND ra.scope_id = :org AND ra.revoked_at IS NULL",
        {"org": str(two_organizations.org_a)},
    )
    assert [row[0] for row in remaining] == [successor]


async def test_a_firm_cannot_revoke_the_clients_owner(
    two_organizations: SeededTenants,
) -> None:
    """Cross-tenant. org B plays the firm; role_assignment's RLS scopes
    organization-level writes to app.current_org_id(), so the row is not
    merely protected - it is invisible.
    """
    visible = await _as_org(
        two_organizations.org_b,
        "SELECT ra.id FROM role_assignment ra "
        'JOIN "role" r ON r.id = ra.role_id '
        "WHERE r.is_system AND r.name = 'Owner' AND ra.scope_id = :org",
        {"org": str(two_organizations.org_a)},
    )
    assert visible == [], "org B cannot even see org A's Owner assignment"

    # And an UPDATE aimed at it changes nothing rather than erroring, which
    # is what an RLS-filtered write does.
    await _as_org(
        two_organizations.org_b,
        "UPDATE role_assignment SET revoked_at = now() "
        "WHERE scope_id = :org AND revoked_at IS NULL",
        {"org": str(two_organizations.org_a)},
    )
    still_there = await _as_org(
        two_organizations.org_a,
        "SELECT ra.id FROM role_assignment ra "
        'JOIN "role" r ON r.id = ra.role_id '
        "WHERE r.is_system AND r.name = 'Owner' AND ra.scope_type = 'organization' "
        "  AND ra.scope_id = :org AND ra.revoked_at IS NULL",
        {"org": str(two_organizations.org_a)},
    )
    assert len(still_there) == 1


async def test_a_floor_permission_cannot_be_removed_from_the_owner_role(
    two_organizations: SeededTenants,
) -> None:
    """ledgr_app has no DELETE grant on role_permission, so the Owner
    bundle cannot be hollowed out from the application at all.
    """
    with pytest.raises(DBAPIError):
        await _as_org(
            two_organizations.org_a,
            "DELETE FROM role_permission rp "
            'USING "role" r, permission p '
            "WHERE rp.role_id = r.id AND rp.permission_id = p.id "
            "  AND r.is_system AND r.name = 'Owner' "
            "  AND p.action = 'export' AND p.resource_type = 'report_data'",
        )


async def test_the_owner_role_cannot_be_archived(two_organizations: SeededTenants) -> None:
    """Archiving Owner would stop every Owner assignment resolving, taking
    the floor with it. role_update's policy refuses writes to system roles.
    """
    await _as_org(
        two_organizations.org_a,
        "UPDATE \"role\" SET archived_at = now() WHERE is_system AND name = 'Owner'",
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT archived_at FROM \"role\" WHERE is_system AND name = 'Owner'",
    )
    assert rows[0][0] is None, "the RLS policy filtered the update out"


async def test_the_client_owner_keeps_every_floor_right_under_the_tightest_profile(
    two_organizations: SeededTenants,
) -> None:
    """End to end against real rows: the most restrictive built-in profile
    assigned to the administration, and the Owner still holds all six.
    """
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO administration_access_profile "
        "(administration_id, profile_id, assigned_by_user_id) "
        "SELECT :admin, p.id, :user FROM client_access_profile p "
        "WHERE p.is_builtin AND p.name = 'Capture only'",
        {
            "admin": str(two_organizations.admin_a),
            "user": str(two_organizations.owner_a),
        },
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        session = AsyncSession(bind=conn)
        service = AuthorizationService(
            SqlAuthorizationRepository(session),
            profiles=ClientAccessProfileService(SqlProfileRepository(session), _NullNotifier()),
        )

        for right in FLOOR_RIGHTS:
            target = (
                OrganizationScope(two_organizations.org_a)
                if right.scope == "organization"
                else AdministrationScope(two_organizations.admin_a)
            )
            decision = await service.authorize(
                AuthorizationRequest(
                    user_id=two_organizations.owner_a,
                    action=right.permission[0],
                    resource_type=right.permission[1],
                    target=target,
                )
            )
            assert decision.allowed, f"floor right removed: {right.phrase}"


class _NullNotifier:
    async def notify_client_owner(self, change: object) -> None:
        return None


# --- IAM-107 / IAM-108 at the database --------------------------------------


async def _engage(firm_org: uuid.UUID, administration_id: uuid.UUID) -> None:
    """An active firm_engagement, created through the same transition the
    application uses: the firm proposes, the client accepts.
    """
    await _as_org(
        firm_org,
        "INSERT INTO firm_engagement "
        "(firm_organization_id, administration_id, status, initiated_by) "
        "VALUES (:firm, :admin, 'pending', 'firm')",
        {"firm": str(firm_org), "admin": str(administration_id)},
    )
    owner_org = (
        await _as_org(
            firm_org,
            "SELECT organization_id FROM administration WHERE id = :id",
            {"id": str(administration_id)},
        )
    )[0][0]
    await _as_org(
        owner_org,
        "UPDATE firm_engagement SET status = 'active' "
        "WHERE firm_organization_id = :firm AND administration_id = :admin",
        {"firm": str(firm_org), "admin": str(administration_id)},
    )


async def test_a_grants_granting_organization_is_derived_not_supplied(
    two_organizations: SeededTenants,
) -> None:
    """role_assignment_firm_staff_guard_trg overwrites whatever a caller
    passes with app.current_org_id(), so attribution cannot be spoofed.
    """
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO role_assignment "
        "(user_id, role_id, scope_type, scope_id, granted_by_user_id, "
        " granted_by_organization_id) "
        "SELECT :user, r.id, 'administration', :admin, :user, :lie "
        "FROM \"role\" r WHERE r.is_system AND r.name = 'Bookkeeper'",
        {
            "user": str(two_organizations.owner_a),
            "admin": str(two_organizations.admin_a),
            # Claiming the grant came from the other organization.
            "lie": str(two_organizations.org_b),
        },
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT granted_by_organization_id FROM role_assignment "
        "WHERE scope_id = :admin AND scope_type = 'administration'",
        {"admin": str(two_organizations.admin_a)},
    )

    assert [row[0] for row in rows] == [two_organizations.org_a]


async def test_a_firm_cannot_grant_staff_access_without_an_engagement(
    two_organizations: SeededTenants,
) -> None:
    """IAM-107: firm staff access flows through the engagement the client
    agreed to. org B plays the firm and has none on org A's administration.
    """
    firm_user = await seed_user(app_engine, email=f"fs+{uuid.uuid4().hex[:8]}@example.com")

    with pytest.raises(DBAPIError, match="no active engagement"):
        await _as_org(
            two_organizations.org_b,
            "INSERT INTO role_assignment "
            "(user_id, role_id, scope_type, scope_id, granted_by_user_id) "
            "SELECT :user, r.id, 'administration', :admin, :user "
            "FROM \"role\" r WHERE r.is_system AND r.name = 'Accountant'",
            {"user": str(firm_user), "admin": str(two_organizations.admin_a)},
        )


async def test_a_firm_with_an_engagement_can_grant_staff_access(
    two_organizations: SeededTenants,
) -> None:
    """The positive case, and it also proves the engagement is what the
    trigger checks rather than something incidental.
    """
    await _engage(two_organizations.org_b, two_organizations.admin_a)
    firm_user = await seed_user(app_engine, email=f"fs+{uuid.uuid4().hex[:8]}@example.com")

    await _as_org(
        two_organizations.org_b,
        "INSERT INTO role_assignment "
        "(user_id, role_id, scope_type, scope_id, granted_by_user_id, expires_at) "
        "SELECT :user, r.id, 'administration', :admin, :user, "
        "       cast('2027-01-01T00:00:00Z' as timestamptz) "
        "FROM \"role\" r WHERE r.is_system AND r.name = 'Accountant'",
        {"user": str(firm_user), "admin": str(two_organizations.admin_a)},
    )

    rows = await _as_org(
        two_organizations.org_b,
        "SELECT granted_by_organization_id, expires_at FROM role_assignment WHERE user_id = :user",
        {"user": str(firm_user)},
    )

    assert rows[0][0] == two_organizations.org_b, "attributed to the firm"
    assert rows[0][1] is not None, "IAM-108: a firm staff grant may carry an expiry"


async def test_a_grants_attribution_cannot_be_rewritten(
    two_organizations: SeededTenants,
) -> None:
    """Re-attributing a grant would change whether a client access profile
    caps it - a firm could relabel its own staff as the client's, or the
    reverse.
    """
    rows = await _as_org(
        two_organizations.org_a,
        "SELECT id FROM role_assignment WHERE scope_id = :org AND scope_type = 'organization'",
        {"org": str(two_organizations.org_a)},
    )

    with pytest.raises(DBAPIError, match="immutable"):
        await _as_org(
            two_organizations.org_a,
            "UPDATE role_assignment SET granted_by_organization_id = :other WHERE id = :id",
            {"other": str(two_organizations.org_b), "id": str(rows[0][0])},
        )


async def test_a_new_firm_employee_reaches_no_client_administration(
    two_organizations: SeededTenants,
) -> None:
    """IAM-107 end to end against real rows: org B is engaged on org A's
    administration and its new employee holds the firm's most powerful
    organization-scoped role. They still reach nothing.
    """
    await _engage(two_organizations.org_b, two_organizations.admin_a)
    employee = await seed_user(app_engine, email=f"new+{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_b,
        user_id=employee,
        role_name="Owner",
        scope_type="organization",
        scope_id=two_organizations.org_b,
        granted_by=two_organizations.owner_b,
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        service = AuthorizationService(SqlAuthorizationRepository(AsyncSession(bind=conn)))
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=employee,
                action="post",
                resource_type="journal_entry",
                target=AdministrationScope(two_organizations.admin_a),
            )
        )

    assert decision.denied, "Owner of the firm must not reach a client's ledger"


# --- IAM-109 / IAM-110 at the database --------------------------------------


async def test_the_access_register_is_readable_by_the_client_without_the_firms_help(
    two_organizations: SeededTenants,
) -> None:
    """IAM-109's "without asking", as an RLS policy. The client's own tenant
    context can read an access row written by a firm user - no firm
    involvement, nothing to request.
    """
    await _engage(two_organizations.org_b, two_organizations.admin_a)
    firm_user = await seed_user(app_engine, email=f"fs+{uuid.uuid4().hex[:8]}@example.com")

    # Written from the FIRM's context, as it would be during a real request.
    await _as_org(
        two_organizations.org_b,
        "INSERT INTO administration_access (user_id, administration_id) VALUES (:user, :admin)",
        {"user": str(firm_user), "admin": str(two_organizations.admin_a)},
    )

    from_client = await _as_org(
        two_organizations.org_a,
        "SELECT user_id, last_accessed_at FROM administration_access "
        "WHERE administration_id = :admin",
        {"admin": str(two_organizations.admin_a)},
    )

    assert [row[0] for row in from_client] == [firm_user]
    assert from_client[0][1] is not None


async def test_an_unrelated_organization_cannot_read_the_access_register(
    two_organizations: SeededTenants,
) -> None:
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO administration_access (user_id, administration_id) VALUES (:user, :admin)",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    from_b = await _as_org(
        two_organizations.org_b,
        "SELECT user_id FROM administration_access WHERE administration_id = :admin",
        {"admin": str(two_organizations.admin_a)},
    )

    assert from_b == []


async def test_last_access_cannot_be_moved_backwards_by_a_direct_update(
    two_organizations: SeededTenants,
) -> None:
    """administration_access_monotonic_trg. The throttle absorbs a stale
    timestamp on the upsert path, so this is the path that reaches the
    trigger: a raw UPDATE, as a backfill or an ops script would issue.
    """
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO administration_access "
        "(user_id, administration_id, first_accessed_at, last_accessed_at) "
        "VALUES (:user, :admin, now(), now())",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    await _as_org(
        two_organizations.org_a,
        "UPDATE administration_access SET last_accessed_at = now() - interval '30 days' "
        "WHERE user_id = :user AND administration_id = :admin",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT last_accessed_at > now() - interval '1 hour' FROM administration_access "
        "WHERE user_id = :user AND administration_id = :admin",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )
    assert rows[0][0] is True, "the trigger clamped the backwards write"


async def test_first_access_cannot_be_rewritten(
    two_organizations: SeededTenants,
) -> None:
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO administration_access "
        "(user_id, administration_id, first_accessed_at, last_accessed_at) "
        "VALUES (:user, :admin, now() - interval '100 days', now() - interval '100 days')",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    await _as_org(
        two_organizations.org_a,
        "UPDATE administration_access SET first_accessed_at = now() "
        "WHERE user_id = :user AND administration_id = :admin",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT first_accessed_at < now() - interval '90 days' FROM administration_access "
        "WHERE user_id = :user AND administration_id = :admin",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )
    assert rows[0][0] is True


async def test_an_access_record_cannot_be_deleted(
    two_organizations: SeededTenants,
) -> None:
    """ "The client retains all data" (IAM-110) includes the record of who was
    in their books: a firm losing access must not be able to erase the
    evidence of what it did.
    """
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO administration_access (user_id, administration_id) VALUES (:user, :admin)",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )

    with pytest.raises(DBAPIError):
        await _as_org(two_organizations.org_a, "DELETE FROM administration_access")


async def test_the_upsert_collapses_repeats_and_records_after_the_window(
    two_organizations: SeededTenants,
) -> None:
    """The real _RECORD_ACCESS_SQL, including its ON CONFLICT ... WHERE - the
    clause that makes a repeat a no-op rather than a rewrite.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        session = AsyncSession(bind=conn)
        repository = SqlFirmAccessRegisterRepository(session)
        now = datetime.now(UTC)

        first = await repository.record_access(
            user_id=two_organizations.owner_a,
            administration_id=two_organizations.admin_a,
            at=now,
            window=timedelta(minutes=5),
        )
        immediate_repeat = await repository.record_access(
            user_id=two_organizations.owner_a,
            administration_id=two_organizations.admin_a,
            at=now + timedelta(seconds=30),
            window=timedelta(minutes=5),
        )
        after_window = await repository.record_access(
            user_id=two_organizations.owner_a,
            administration_id=two_organizations.admin_a,
            at=now + timedelta(minutes=30),
            window=timedelta(minutes=5),
        )
        await conn.commit()

    assert first is True
    assert immediate_repeat is False, "a repeat inside the window is a no-op"
    assert after_window is True


async def test_a_session_can_be_moved_into_and_out_of_an_administration(
    two_organizations: SeededTenants,
) -> None:
    """sessions.active_administration_id (0017), through the real repository -
    the write IAM-110's revocation clears.
    """
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        repository = SqlEngagementRevocationRepository(AsyncSession(bind=conn))
        entered = await repository.set_active_administration(
            session_id=session_id,
            user_id=two_organizations.owner_a,
            administration_id=two_organizations.admin_a,
        )
        left = await repository.set_active_administration(
            session_id=session_id,
            user_id=two_organizations.owner_a,
            administration_id=None,
        )
        # Somebody else's session cannot be moved, even knowing its id.
        foreign = await repository.set_active_administration(
            session_id=session_id,
            user_id=two_organizations.owner_b,
            administration_id=two_organizations.admin_a,
        )
        await conn.commit()

    assert entered is True
    assert left is True
    assert foreign is False


# --- end to end through the HTTP stack --------------------------------------


@pytest.mark.isolation("GET", "/v1/switcher")
async def test_the_switcher_shows_only_the_callers_own_administrations(
    two_organizations: SeededTenants,
) -> None:
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/switcher",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.admin_b],
        )

    ids = {entry["administration_id"] for entry in response.json()}
    assert str(two_organizations.admin_a) in ids
    assert str(two_organizations.admin_b) not in ids


@pytest.mark.isolation("GET", "/v1/switcher/search")
async def test_switcher_search_never_reveals_another_tenants_client(
    two_organizations: SeededTenants,
) -> None:
    """FR-FRM-000's "showing only granted administrations" is a tenancy
    guarantee as well as a UX one: org A and org B's administrations are
    seeded with the SAME legal name, so a search that filtered after
    fetching would return org B's row to org A.
    """
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            "/v1/switcher/search?q=Bakker",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[two_organizations.admin_b],
        )

    ids = {entry["administration_id"] for entry in response.json()}
    assert str(two_organizations.admin_a) in ids
    assert str(two_organizations.admin_b) not in ids


@pytest.mark.isolation("GET", "/v1/switcher/active")
async def test_the_active_badge_is_null_for_a_client_the_user_cannot_reach(
    two_organizations: SeededTenants,
) -> None:
    """A session pointing at an administration the user cannot reach must
    render no header rather than a stale name - a stale name is exactly the
    wrong-client failure FR-FRM-000a is about.
    """
    token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
    # A token claiming org B's administration is open, which org A's user
    # holds no grant on.
    forged = jwt.encode(
        {
            **jwt.decode(token, settings.jwt_signing_key, algorithms=["HS256"]),
            "adm": str(two_organizations.admin_b),
        },
        settings.jwt_signing_key,
        algorithm="HS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/switcher/active", headers={"Authorization": f"Bearer {forged}"}
        )

    assert response.status_code == 200
    assert response.json() is None
    assert str(two_organizations.admin_b) not in response.text


@pytest.mark.isolation("PUT", "/v1/switcher/{administration_id}")
async def test_a_user_cannot_switch_into_another_tenants_administration(
    two_organizations: SeededTenants,
) -> None:
    session_id = await seed_session(app_engine, user_id=two_organizations.owner_a)
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )
    headers = {
        "Authorization": "Bearer "
        + make_token(
            two_organizations.org_a,
            user_id=two_organizations.owner_a,
            session_id=session_id,
        )
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # NFR-032: every mutating endpoint requires a key, and the two calls
        # below are different requests, so they need different ones. Reusing
        # one would be answered 422 (key reused for a different request),
        # which is correct behaviour and would hide what this test is about.
        own = await client.put(
            f"/v1/switcher/{two_organizations.admin_a}",
            headers={**headers, "Idempotency-Key": f"switch-own-{uuid.uuid4()}"},
        )
        foreign = await client.put(
            f"/v1/switcher/{two_organizations.admin_b}",
            headers={**headers, "Idempotency-Key": f"switch-foreign-{uuid.uuid4()}"},
        )

    assert own.status_code == 200
    assert foreign.status_code == 403, "org B's administration is not reachable at all"


async def test_switching_without_a_session_identifier_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """Writing to every session the user holds would move devices they are
    not holding, so a token with no `sid` cannot switch.
    """
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=two_organizations.owner_a,
        role_name="Accountant",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )
    headers = {
        "Authorization": "Bearer "
        + make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.put(
            f"/v1/switcher/{two_organizations.admin_a}",
            headers={**headers, "Idempotency-Key": f"switch-{uuid.uuid4()}"},
        )

    assert response.status_code == 409


async def test_reading_an_administration_records_the_access(
    two_organizations: SeededTenants,
) -> None:
    """IAM-109 end to end, and the wiring that was missing: an ordinary
    authorized read through require_permission leaves a last-access row.
    """
    headers = {
        "Authorization": "Bearer "
        + make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}", headers=headers
        )
    assert response.status_code == 200

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT access_count FROM administration_access "
        "WHERE user_id = :user AND administration_id = :admin",
        {"user": str(two_organizations.owner_a), "admin": str(two_organizations.admin_a)},
    )
    assert rows and rows[0][0] >= 1


async def test_a_denied_request_records_no_access(
    two_organizations: SeededTenants,
) -> None:
    """A row claiming someone was in the books must mean they got in.
    Recording happens after authorization, so a refused probe leaves nothing.
    """
    stranger = await seed_user(app_engine, email=f"probe+{uuid.uuid4().hex[:8]}@example.com")
    headers = {"Authorization": "Bearer " + make_token(two_organizations.org_a, user_id=stranger)}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}", headers=headers
        )
    assert response.status_code == 403

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT 1 FROM administration_access WHERE user_id = :user",
        {"user": str(stranger)},
    )
    assert rows == []


@pytest.mark.isolation("GET", "/v1/administrations")
async def test_a_user_without_a_grant_is_denied_even_inside_their_own_tenant(
    two_organizations: SeededTenants,
) -> None:
    """Default deny (IAM-031) is not the same thing as tenant isolation. A
    user who genuinely belongs to org A, authenticated, MFA-verified, and
    carrying correct tenant context, still sees nothing without a grant.
    """
    stranger = await seed_user(app_engine, email=f"stranger+{uuid.uuid4().hex[:8]}@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/administrations",
            headers={
                "Authorization": (f"Bearer {make_token(two_organizations.org_a, user_id=stranger)}")
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "no_matching_grant"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}")
async def test_an_administration_grant_does_not_reach_a_sibling_administration(
    two_organizations: SeededTenants,
) -> None:
    """IAM-032 end to end: same organization, same user, two administrations,
    a grant on only one of them.
    """
    second = await seed_administration(
        app_engine,
        org_id=two_organizations.org_a,
        legal_name="Bakker Holding B.V.",
        legal_form="BV",
    )
    bookkeeper = await seed_user(app_engine, email=f"bk+{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=bookkeeper,
        role_name="Bookkeeper",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
    )

    headers = {"Authorization": f"Bearer {make_token(two_organizations.org_a, user_id=bookkeeper)}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        granted = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}", headers=headers
        )
        sibling = await client.get(f"/v1/administrations/{second}", headers=headers)

    assert granted.status_code == 200
    assert sibling.status_code == 403


async def test_an_expired_grant_stops_working_with_no_administrative_action(
    two_organizations: SeededTenants,
) -> None:
    """IAM-035 against real rows: nothing runs between the grant expiring
    and the request being refused - the expiry is a predicate in the
    evaluation query, not a job that has to sweep anything.
    """
    user = await seed_user(app_engine, email=f"temp+{uuid.uuid4().hex[:8]}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=two_organizations.org_a,
        user_id=user,
        role_name="Viewer",
        scope_type="administration",
        scope_id=two_organizations.admin_a,
        expires_at="2020-01-01T00:00:00Z",
    )

    token = make_token(two_organizations.org_a, user_id=user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/administrations/{two_organizations.admin_a}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403

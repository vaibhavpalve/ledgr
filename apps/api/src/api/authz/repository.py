"""SQLAlchemy-backed AuthorizationRepository over the tables in
migrations/0009_authorization.sql.

Every query here runs on an ordinary tenant-scoped session (api.db.
get_db_session), so RLS applies to all of them. That is deliberate defence
in depth rather than redundancy: the row-level policies on role_assignment
can only ever REMOVE rows from these results, and a removed row is one fewer
grant, which is one more denial. A bug in either layer therefore fails
closed. The one place this is load-bearing is owning_organization() - if the
caller cannot see the administration at all, it returns None, and
_scope_covers denies rather than cascading an organization grant onto an
administration the session was never allowed to look at.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.model import AssignmentRecord, Grant, RoleRecord, ScopeType
from api.authz.rights_floor import OWNER_ROLE_NAME

# The joined shape every authorization decision reads. Filtering on
# action/resource_type in SQL keeps the row count proportional to the
# permission being asked about rather than to everything the user holds.
_LIVE_GRANTS_SQL = """
    SELECT ra.id            AS assignment_id,
           r.id             AS role_id,
           r.name           AS role_name,
           ra.scope_type    AS scope_type,
           ra.scope_id      AS scope_id,
           p.resource_scope AS resource_scope,
           ra.conditions    AS conditions,
           ra.granted_by_organization_id AS granted_by_organization_id
    FROM role_assignment ra
    JOIN "role" r            ON r.id = ra.role_id
    JOIN role_permission rp  ON rp.role_id = r.id
    JOIN permission p        ON p.id = rp.permission_id
    WHERE ra.user_id = :user_id
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      AND r.archived_at IS NULL
      AND p.action = :action
      AND p.resource_type = :resource_type
"""

# The IAM-036 ceiling. Note `ra.conditions = '{}'::jsonb`: a permission
# reachable only through a CONDITIONED grant is not held outright, so it
# cannot be composed into a role or handed to someone else - see the
# Protocol's docstring in api.authz.service for why that matters.
_PERMISSIONS_HELD_SQL = """
    SELECT DISTINCT p.action, p.resource_type
    FROM role_assignment ra
    JOIN "role" r            ON r.id = ra.role_id
    JOIN role_permission rp  ON rp.role_id = r.id
    JOIN permission p        ON p.id = rp.permission_id
    WHERE ra.user_id = :user_id
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      AND r.archived_at IS NULL
      AND ra.conditions = '{}'::jsonb
      AND (
            (ra.scope_type = 'organization' AND ra.scope_id = :organization_id)
         OR (ra.scope_type = 'administration' AND ra.scope_id IN (
                SELECT a.id FROM administration a WHERE a.organization_id = :organization_id
            ))
      )
"""


def _as_conditions(raw: Any) -> Mapping[str, Any]:
    """jsonb comes back as a dict or as a JSON string depending on whether
    the driver has a codec registered for it. Normalize both, and parse any
    JSON number as Decimal rather than float - NFR-031 applies to every path
    a monetary value can travel, and an amount ceiling travels this one.
    (role_assignment_conditions_guard already refuses to store a numeric
    ceiling, so this is the second line, not the first.)
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw, parse_float=Decimal)
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, Mapping):
        return raw
    return {}


class SqlAuthorizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def live_grants(
        self, *, user_id: uuid.UUID, action: str, resource_type: str, now: datetime
    ) -> Sequence[Grant]:
        result = await self._session.execute(
            text(_LIVE_GRANTS_SQL),
            {
                "user_id": str(user_id),
                "action": action,
                "resource_type": resource_type,
                "now": now,
            },
        )
        return [
            Grant(
                assignment_id=row.assignment_id,
                role_id=row.role_id,
                role_name=row.role_name,
                scope_type=row.scope_type,
                scope_id=row.scope_id,
                resource_scope=row.resource_scope,
                conditions=_as_conditions(row.conditions),
                granted_by_organization_id=row.granted_by_organization_id,
            )
            for row in result
        ]

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]:
        result = await self._session.execute(
            text(_PERMISSIONS_HELD_SQL),
            {"user_id": str(user_id), "organization_id": str(organization_id), "now": now},
        )
        return {(row.action, row.resource_type) for row in result}

    async def is_organization_owner(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM role_assignment ra "
                'JOIN "role" r ON r.id = ra.role_id '
                "WHERE ra.user_id = :user_id AND ra.scope_type = 'organization' "
                "  AND ra.scope_id = :organization_id AND ra.revoked_at IS NULL "
                "  AND (ra.expires_at IS NULL OR ra.expires_at > now()) "
                "  AND r.is_system AND r.name = :owner) AS is_owner"
            ),
            {
                "user_id": str(user_id),
                "organization_id": str(organization_id),
                "owner": OWNER_ROLE_NAME,
            },
        )
        return bool(result.scalar_one())

    async def count_active_owners(self, organization_id: uuid.UUID) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(DISTINCT ra.user_id) FROM role_assignment ra "
                'JOIN "role" r ON r.id = ra.role_id '
                "WHERE ra.scope_type = 'organization' AND ra.scope_id = :organization_id "
                "  AND ra.revoked_at IS NULL "
                "  AND (ra.expires_at IS NULL OR ra.expires_at > now()) "
                "  AND r.is_system AND r.name = :owner"
            ),
            {"organization_id": str(organization_id), "owner": OWNER_ROLE_NAME},
        )
        return int(result.scalar_one())

    async def get_assignment(self, assignment_id: uuid.UUID) -> AssignmentRecord | None:
        result = await self._session.execute(
            text(
                "SELECT ra.id, ra.user_id, ra.role_id, r.name AS role_name, "
                "       ra.scope_type, ra.scope_id, ra.revoked_at "
                'FROM role_assignment ra JOIN "role" r ON r.id = ra.role_id '
                "WHERE ra.id = :id"
            ),
            {"id": str(assignment_id)},
        )
        row = result.first()
        if row is None:
            return None
        return AssignmentRecord(
            id=row.id,
            user_id=row.user_id,
            role_id=row.role_id,
            role_name=row.role_name,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            revoked_at=row.revoked_at,
        )

    async def revoke_assignment(self, *, assignment_id: uuid.UUID, at: datetime) -> None:
        # role_assignment_owner_floor_guard_trg rejects this if it would
        # leave the organization without an Owner, whatever this code does.
        await self._session.execute(
            text(
                "UPDATE role_assignment SET revoked_at = :at WHERE id = :id AND revoked_at IS NULL"
            ),
            {"at": at, "id": str(assignment_id)},
        )

    async def get_roles(self, role_ids: Sequence[uuid.UUID]) -> Sequence[RoleRecord]:
        if not role_ids:
            return []
        result = await self._session.execute(
            text(
                "SELECT id, name, scope_type, organization_id, is_system, archived_at "
                'FROM "role" WHERE id = ANY(:ids)'
            ),
            {"ids": [str(role_id) for role_id in role_ids]},
        )
        return [
            RoleRecord(
                id=row.id,
                name=row.name,
                scope_type=row.scope_type,
                organization_id=row.organization_id,
                is_system=row.is_system,
                archived_at=row.archived_at,
            )
            for row in result
        ]

    async def role_permissions(self, role_ids: Sequence[uuid.UUID]) -> set[tuple[str, str]]:
        if not role_ids:
            return set()
        # Flat, deliberately: every role's role_permission rows are already
        # its full closure, so this resolves nesting transitively without
        # touching role_component. See 0011_custom_role_composition.sql.
        result = await self._session.execute(
            text(
                "SELECT DISTINCT p.action, p.resource_type "
                "FROM role_permission rp JOIN permission p ON p.id = rp.permission_id "
                "WHERE rp.role_id = ANY(:ids)"
            ),
            {"ids": [str(role_id) for role_id in role_ids]},
        )
        return {(row.action, row.resource_type) for row in result}

    async def create_custom_role(
        self,
        *,
        organization_id: uuid.UUID,
        name: str,
        scope_type: ScopeType,
        permissions: Iterable[tuple[str, str]],
        component_role_ids: Sequence[uuid.UUID],
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                'INSERT INTO "role" '
                "(organization_id, name, scope_type, is_system, created_by_user_id) "
                "VALUES (:organization_id, :name, :scope_type, false, :created_by) "
                "RETURNING id"
            ),
            {
                "organization_id": str(organization_id),
                "name": name,
                "scope_type": scope_type,
                "created_by": str(created_by_user_id),
            },
        )
        role_id: uuid.UUID = result.scalar_one()

        for action, resource_type in sorted(permissions):
            # role_permission_scope_guard_trg rejects an organization-scope
            # permission in an administration-scope role, so a caller
            # composing an incoherent bundle gets a database error naming
            # the reason rather than a role that silently never matches.
            await self._session.execute(
                text(
                    "INSERT INTO role_permission (role_id, permission_id) "
                    "SELECT :role_id, p.id FROM permission p "
                    "WHERE p.action = :action AND p.resource_type = :resource_type"
                ),
                {"role_id": str(role_id), "action": action, "resource_type": resource_type},
            )

        # Provenance only - never read when a request is authorized. The
        # permissions above are the flattened truth; these rows record what
        # they were flattened from.
        for component_id in component_role_ids:
            await self._session.execute(
                text(
                    "INSERT INTO role_component (role_id, component_role_id) "
                    "VALUES (:role_id, :component_role_id)"
                ),
                {"role_id": str(role_id), "component_role_id": str(component_id)},
            )

        return role_id

    async def create_assignment(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID,
        conditions: Mapping[str, Any],
        granted_by_user_id: uuid.UUID,
        expires_at: datetime | None,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO role_assignment "
                "(user_id, role_id, scope_type, scope_id, conditions, "
                " granted_by_user_id, expires_at) "
                "VALUES (:user_id, :role_id, :scope_type, :scope_id, "
                "        cast(:conditions as jsonb), :granted_by, :expires_at) "
                "RETURNING id"
            ),
            {
                "user_id": str(user_id),
                "role_id": str(role_id),
                "scope_type": scope_type,
                "scope_id": str(scope_id),
                "conditions": json.dumps(conditions),
                "granted_by": str(granted_by_user_id),
                "expires_at": expires_at,
            },
        )
        assignment_id: uuid.UUID = result.scalar_one()
        return assignment_id

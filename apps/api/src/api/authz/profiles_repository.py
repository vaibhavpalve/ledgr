"""SQLAlchemy-backed ProfileRepository over migrations/0013 and 0014.

Every query runs on the ordinary tenant-scoped session, so RLS applies.
That matters most for firm_permissions_on: it computes IAM-102's ceiling,
and a query returning FEWER rows than the truth makes the ceiling tighter,
never looser. Both layers therefore fail closed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.profiles import (
    ClientAccessProfile,
    Permission,
    ProfileRestrictions,
    ProfileVersion,
)

_PROFILE_COLUMNS = "id, firm_organization_id, name, description, is_builtin, archived_at"

# IAM-102's ceiling: what the FIRM can do on this administration - the union
# of live, UNCONDITIONED grants its users hold there. Conditioned grants are
# excluded on the same reasoning as the IAM-036 authoring ceiling (ADR-013):
# a firm whose staff may only approve up to EUR 5,000 does not hold "approve
# purchase invoices" outright, and so cannot confer it unconditionally on
# the client.
_FIRM_PERMISSIONS_SQL = """
    SELECT DISTINCT p.action, p.resource_type
    FROM role_assignment ra
    JOIN "role" r            ON r.id = ra.role_id
    JOIN role_permission rp  ON rp.role_id = r.id
    JOIN permission p        ON p.id = rp.permission_id
    WHERE ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > now())
      AND r.archived_at IS NULL
      AND ra.conditions = '{}'::jsonb
      AND ra.scope_type = 'administration'
      AND ra.scope_id = :administration_id
      AND ra.user_id IN (
            SELECT DISTINCT staff.user_id
            FROM role_assignment staff
            WHERE staff.revoked_at IS NULL
              AND staff.scope_type = 'organization'
              AND staff.scope_id = :firm_organization_id
      )
"""


def _profile(row: Any) -> ClientAccessProfile:
    return ClientAccessProfile(
        id=row.id,
        name=row.name,
        description=row.description,
        is_builtin=row.is_builtin,
        firm_organization_id=row.firm_organization_id,
        archived_at=row.archived_at,
    )


def _restrictions(raw: Any) -> ProfileRestrictions:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return ProfileRestrictions.from_mapping(raw or {})


class SqlProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_profile(self, profile_id: uuid.UUID) -> ClientAccessProfile | None:
        result = await self._session.execute(
            text(f"SELECT {_PROFILE_COLUMNS} FROM client_access_profile WHERE id = :id"),
            {"id": str(profile_id)},
        )
        row = result.first()
        return _profile(row) if row is not None else None

    async def get_profile_by_name(self, name: str) -> ClientAccessProfile | None:
        result = await self._session.execute(
            text(
                f"SELECT {_PROFILE_COLUMNS} FROM client_access_profile "
                "WHERE name = :name AND is_builtin"
            ),
            {"name": name},
        )
        row = result.first()
        return _profile(row) if row is not None else None

    async def _version_row(self, profile_id: uuid.UUID) -> Any:
        result = await self._session.execute(
            text(
                "SELECT id, profile_id, version, summary, restrictions, "
                "       published_by_user_id, published_at "
                "FROM client_access_profile_version WHERE profile_id = :profile_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"profile_id": str(profile_id)},
        )
        return result.first()

    async def _version_permissions(self, version_id: uuid.UUID) -> frozenset[Permission]:
        result = await self._session.execute(
            text(
                "SELECT p.action, p.resource_type "
                "FROM client_access_profile_permission cp "
                "JOIN permission p ON p.id = cp.permission_id "
                "WHERE cp.profile_version_id = :version_id"
            ),
            {"version_id": str(version_id)},
        )
        return frozenset((row.action, row.resource_type) for row in result)

    async def current_version(self, profile_id: uuid.UUID) -> ProfileVersion | None:
        row = await self._version_row(profile_id)
        if row is None:
            return None
        return ProfileVersion(
            id=row.id,
            profile_id=row.profile_id,
            version=row.version,
            summary=row.summary,
            permissions=await self._version_permissions(row.id),
            restrictions=_restrictions(row.restrictions),
            published_by_user_id=row.published_by_user_id,
            published_at=row.published_at,
        )

    async def current_profile_for(
        self, administration_id: uuid.UUID
    ) -> tuple[ClientAccessProfile, ProfileVersion] | None:
        result = await self._session.execute(
            text(
                f"SELECT {', '.join('p.' + c for c in _PROFILE_COLUMNS.split(', '))} "
                "FROM administration_access_profile aap "
                "JOIN client_access_profile p ON p.id = aap.profile_id "
                "WHERE aap.administration_id = :administration_id "
                "  AND aap.superseded_at IS NULL"
            ),
            {"administration_id": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        profile = _profile(row)
        version = await self.current_version(profile.id)
        return (profile, version) if version is not None else None

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

    async def administrations_using(self, profile_id: uuid.UUID) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            text(
                "SELECT administration_id FROM administration_access_profile "
                "WHERE profile_id = :profile_id AND superseded_at IS NULL"
            ),
            {"profile_id": str(profile_id)},
        )
        return [row.administration_id for row in result]

    async def firm_permissions_on(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> set[Permission]:
        result = await self._session.execute(
            text(_FIRM_PERMISSIONS_SQL),
            {
                "firm_organization_id": str(firm_organization_id),
                "administration_id": str(administration_id),
            },
        )
        return {(row.action, row.resource_type) for row in result}

    async def create_profile(
        self,
        *,
        firm_organization_id: uuid.UUID,
        name: str,
        description: str,
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO client_access_profile "
                "(firm_organization_id, name, description, is_builtin, created_by_user_id) "
                "VALUES (:firm, :name, :description, false, :created_by) RETURNING id"
            ),
            {
                "firm": str(firm_organization_id),
                "name": name,
                "description": description,
                "created_by": str(created_by_user_id),
            },
        )
        profile_id: uuid.UUID = result.scalar_one()
        return profile_id

    async def publish_version(
        self,
        *,
        profile_id: uuid.UUID,
        version: int,
        summary: str,
        permissions: frozenset[Permission],
        restrictions: Mapping[str, Any],
        published_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO client_access_profile_version "
                "(profile_id, version, summary, restrictions, published_by_user_id) "
                "VALUES (:profile_id, :version, :summary, cast(:restrictions as jsonb), "
                "        :published_by) RETURNING id"
            ),
            {
                "profile_id": str(profile_id),
                "version": version,
                "summary": summary,
                "restrictions": json.dumps(dict(restrictions), sort_keys=True),
                "published_by": str(published_by_user_id),
            },
        )
        version_id: uuid.UUID = result.scalar_one()

        for action, resource_type in sorted(permissions):
            # client_access_profile_permission_guard_trg rejects an
            # organization-scope permission here, so an incoherent profile
            # fails with a message naming the reason.
            await self._session.execute(
                text(
                    "INSERT INTO client_access_profile_permission "
                    "(profile_version_id, permission_id) "
                    "SELECT :version_id, p.id FROM permission p "
                    "WHERE p.action = :action AND p.resource_type = :resource_type"
                ),
                {
                    "version_id": str(version_id),
                    "action": action,
                    "resource_type": resource_type,
                },
            )

        return version_id

    async def assign_profile(
        self,
        *,
        administration_id: uuid.UUID,
        profile_id: uuid.UUID,
        assigned_by_user_id: uuid.UUID,
        at: datetime,
    ) -> uuid.UUID:
        # Supersede first: administration_access_profile_current_idx allows
        # exactly one assignment in force, so the order is load-bearing (the
        # same insert-before-retire bug class the encryption-key work hit).
        await self._session.execute(
            text(
                "UPDATE administration_access_profile SET superseded_at = :at "
                "WHERE administration_id = :administration_id AND superseded_at IS NULL"
            ),
            {"at": at, "administration_id": str(administration_id)},
        )
        result = await self._session.execute(
            text(
                "INSERT INTO administration_access_profile "
                "(administration_id, profile_id, assigned_by_user_id, assigned_at) "
                "VALUES (:administration_id, :profile_id, :assigned_by, :at) RETURNING id"
            ),
            {
                "administration_id": str(administration_id),
                "profile_id": str(profile_id),
                "assigned_by": str(assigned_by_user_id),
                "at": at,
            },
        )
        assignment_id: uuid.UUID = result.scalar_one()
        return assignment_id

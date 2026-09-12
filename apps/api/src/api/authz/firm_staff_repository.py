"""SQLAlchemy-backed FirmStaffRepository over role_assignment as extended by
migration 0016.

There is no firm_staff table. A firm staff grant IS a role_assignment -
administration-scoped, made from the firm's tenant context - and that is the
point of IAM-107: "granted per client administration" means the same row
shape as any other grant, so it expires the same way (IAM-035/108), is
revoked the same way, and is evaluated by the same query. A parallel table
would be a second access path to keep in step with the first.

create_firm_staff_assignment does not set granted_by_organization_id.
role_assignment_firm_staff_guard_trg derives it from app.current_org_id(),
so the attribution cannot be spoofed by a caller or forgotten by this class.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.firm_staff import FirmStaffGrant
from api.authz.model import ScopeType

_LIST_GRANTS_SQL = """
    SELECT ra.id, ra.user_id, ra.scope_id AS administration_id, r.name AS role_name,
           ra.created_at, ra.granted_by_user_id, ra.expires_at
    FROM role_assignment ra
    JOIN "role" r ON r.id = ra.role_id
    WHERE ra.scope_type = 'administration'
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      AND ra.granted_by_organization_id = :firm_organization_id
      AND (:staff_user_id IS NULL OR ra.user_id = :staff_user_id)
    ORDER BY ra.created_at DESC
"""


class SqlFirmStaffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_system_role(self, name: str) -> tuple[uuid.UUID, ScopeType] | None:
        result = await self._session.execute(
            text('SELECT id, scope_type FROM "role" WHERE is_system AND name = :name'),
            {"name": name},
        )
        row = result.first()
        return (row.id, row.scope_type) if row is not None else None

    async def holds_manage_user_role(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM role_assignment ra "
                'JOIN "role" r ON r.id = ra.role_id '
                "JOIN role_permission rp ON rp.role_id = r.id "
                "JOIN permission p ON p.id = rp.permission_id "
                "WHERE ra.user_id = :user_id AND ra.scope_type = 'organization' "
                "  AND ra.scope_id = :organization_id AND ra.revoked_at IS NULL "
                "  AND (ra.expires_at IS NULL OR ra.expires_at > now()) "
                "  AND r.archived_at IS NULL "
                "  AND p.action = 'manage' AND p.resource_type = 'user_role') AS held"
            ),
            {"user_id": str(user_id), "organization_id": str(organization_id)},
        )
        return bool(result.scalar_one())

    async def has_active_engagement(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM firm_engagement fe "
                "WHERE fe.firm_organization_id = :firm_organization_id "
                "  AND fe.administration_id = :administration_id "
                "  AND fe.status = 'active') AS engaged"
            ),
            {
                "firm_organization_id": str(firm_organization_id),
                "administration_id": str(administration_id),
            },
        )
        return bool(result.scalar_one())

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

    async def role_permissions(self, role_id: uuid.UUID) -> set[tuple[str, str]]:
        result = await self._session.execute(
            text(
                "SELECT p.action, p.resource_type FROM role_permission rp "
                "JOIN permission p ON p.id = rp.permission_id WHERE rp.role_id = :role_id"
            ),
            {"role_id": str(role_id)},
        )
        return {(row.action, row.resource_type) for row in result}

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]:
        result = await self._session.execute(
            text(
                "SELECT DISTINCT p.action, p.resource_type FROM role_assignment ra "
                'JOIN "role" r ON r.id = ra.role_id '
                "JOIN role_permission rp ON rp.role_id = r.id "
                "JOIN permission p ON p.id = rp.permission_id "
                "WHERE ra.user_id = :user_id AND ra.revoked_at IS NULL "
                "  AND (ra.expires_at IS NULL OR ra.expires_at > :now) "
                "  AND r.archived_at IS NULL AND ra.conditions = '{}'::jsonb "
                "  AND ra.scope_type = 'organization' AND ra.scope_id = :organization_id"
            ),
            {"user_id": str(user_id), "organization_id": str(organization_id), "now": now},
        )
        return {(row.action, row.resource_type) for row in result}

    async def create_firm_staff_assignment(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        administration_id: uuid.UUID,
        granted_by_user_id: uuid.UUID,
        expires_at: datetime | None,
        at: datetime,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO role_assignment "
                "(user_id, role_id, scope_type, scope_id, granted_by_user_id, expires_at) "
                "VALUES (:user_id, :role_id, 'administration', :scope_id, :granted_by, "
                "        :expires_at) RETURNING id"
            ),
            {
                "user_id": str(user_id),
                "role_id": str(role_id),
                "scope_id": str(administration_id),
                "granted_by": str(granted_by_user_id),
                "expires_at": expires_at,
            },
        )
        assignment_id: uuid.UUID = result.scalar_one()
        return assignment_id

    async def revoke_firm_staff_assignments(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> int:
        # granted_by_organization_id in the predicate is what keeps this to
        # the FIRM's own grants: a firm revoking a staff member from a client
        # must not also revoke that client's own users, who may hold grants
        # on the same administration.
        result = await self._session.execute(
            text(
                "UPDATE role_assignment SET revoked_at = :at "
                "WHERE user_id = :staff_user_id AND scope_type = 'administration' "
                "  AND scope_id = :administration_id "
                "  AND granted_by_organization_id = :firm_organization_id "
                "  AND revoked_at IS NULL "
                "RETURNING id"
            ),
            {
                "at": at,
                "staff_user_id": str(staff_user_id),
                "administration_id": str(administration_id),
                "firm_organization_id": str(firm_organization_id),
            },
        )
        # RETURNING rather than rowcount: an UPDATE filtered by RLS reports a
        # rowcount that has already had invisible rows removed, but counting
        # the returned ids makes the caller's number unambiguously "grants
        # this firm actually revoked".
        return len(result.all())

    async def list_firm_staff_grants(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID | None,
        now: datetime,
    ) -> Sequence[FirmStaffGrant]:
        result = await self._session.execute(
            text(_LIST_GRANTS_SQL),
            {
                "firm_organization_id": str(firm_organization_id),
                "staff_user_id": str(staff_user_id) if staff_user_id is not None else None,
                "now": now,
            },
        )
        return [
            FirmStaffGrant(
                assignment_id=row.id,
                user_id=row.user_id,
                administration_id=row.administration_id,
                role_name=row.role_name,
                granted_at=row.created_at,
                granted_by_user_id=row.granted_by_user_id,
                expires_at=row.expires_at,
            )
            for row in result
        ]

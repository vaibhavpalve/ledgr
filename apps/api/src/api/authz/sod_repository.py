"""SQLAlchemy-backed SodRepository over migrations/0012_segregation_of_duties.sql.

Every query runs on the ordinary tenant-scoped session, so RLS applies. For
the two counting queries that matters in a specific way: active_user_count
drives both exemptions (IAM-061's "more than two active users" and IAM-065's
single-user case), and an undercount would silently EXEMPT an organization
from rules it should be subject to. The count is therefore derived from
role_assignment - the same rows authorization itself reads - rather than
from any separate membership list that could drift below the real figure.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.sod import Deviation, SodEvent, SodOutcome, SodRule

# Membership is holding a live grant: there is no membership table, because
# users are global (0003_authentication.sql) and tenant-scoped access IS a
# role assignment. Counting distinct users this way means the figure the
# exemptions depend on cannot disagree with the figure authorization uses.
_ACTIVE_USER_COUNT_SQL = """
    SELECT COUNT(DISTINCT ra.user_id) AS n
    FROM role_assignment ra
    JOIN users u ON u.id = ra.user_id
    WHERE ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > now())
      AND u.status = 'active'
      AND (
            (ra.scope_type = 'organization' AND ra.scope_id = :organization_id)
         OR (ra.scope_type = 'administration' AND ra.scope_id IN (
                SELECT a.id FROM administration a WHERE a.organization_id = :organization_id
            ))
      )
"""

# By ROLE NAME, deliberately - see acknowledge_deviation's docstring in
# api.authz.sod for why a permission-based check would be routable around.
_HOLDS_OWNER_SQL = """
    SELECT EXISTS (
        SELECT 1
        FROM role_assignment ra
        JOIN "role" r ON r.id = ra.role_id
        WHERE ra.user_id = :user_id
          AND ra.revoked_at IS NULL
          AND (ra.expires_at IS NULL OR ra.expires_at > now())
          AND r.is_system
          AND r.name = 'Owner'
          AND ra.scope_type = 'organization'
          AND ra.scope_id = :organization_id
    ) AS held
"""

_DEVIATION_COLUMNS = (
    "id, organization_id, rule, acknowledged_by_user_id, reason, "
    "acknowledged_at, revoked_at, revoked_by_user_id"
)


def _deviation(row: object) -> Deviation:
    return Deviation(
        id=row.id,  # type: ignore[attr-defined]
        organization_id=row.organization_id,  # type: ignore[attr-defined]
        rule=SodRule(row.rule),  # type: ignore[attr-defined]
        acknowledged_by_user_id=row.acknowledged_by_user_id,  # type: ignore[attr-defined]
        reason=row.reason,  # type: ignore[attr-defined]
        acknowledged_at=row.acknowledged_at,  # type: ignore[attr-defined]
        revoked_at=row.revoked_at,  # type: ignore[attr-defined]
        revoked_by_user_id=row.revoked_by_user_id,  # type: ignore[attr-defined]
    )


class SqlSodRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_user_count(self, organization_id: uuid.UUID) -> int:
        result = await self._session.execute(
            text(_ACTIVE_USER_COUNT_SQL), {"organization_id": str(organization_id)}
        )
        return int(result.scalar_one())

    async def holds_owner_role(self, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text(_HOLDS_OWNER_SQL),
            {"user_id": str(user_id), "organization_id": str(organization_id)},
        )
        return bool(result.scalar_one())

    async def active_deviation(
        self, *, organization_id: uuid.UUID, rule: SodRule
    ) -> Deviation | None:
        result = await self._session.execute(
            text(
                f"SELECT {_DEVIATION_COLUMNS} FROM sod_policy_deviation "
                "WHERE organization_id = :organization_id AND rule = :rule "
                "  AND revoked_at IS NULL"
            ),
            {"organization_id": str(organization_id), "rule": rule.value},
        )
        row = result.first()
        return _deviation(row) if row is not None else None

    async def list_deviations(self, organization_id: uuid.UUID) -> Sequence[Deviation]:
        result = await self._session.execute(
            text(
                f"SELECT {_DEVIATION_COLUMNS} FROM sod_policy_deviation "
                "WHERE organization_id = :organization_id "
                "ORDER BY acknowledged_at DESC"
            ),
            {"organization_id": str(organization_id)},
        )
        return [_deviation(row) for row in result]

    async def create_deviation(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        acknowledged_by_user_id: uuid.UUID,
        reason: str,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO sod_policy_deviation "
                "(organization_id, rule, acknowledged_by_user_id, reason) "
                "VALUES (:organization_id, :rule, :acknowledged_by, :reason) RETURNING id"
            ),
            {
                "organization_id": str(organization_id),
                "rule": rule.value,
                "acknowledged_by": str(acknowledged_by_user_id),
                "reason": reason,
            },
        )
        deviation_id: uuid.UUID = result.scalar_one()
        return deviation_id

    async def revoke_deviation(
        self, *, deviation_id: uuid.UUID, revoked_by_user_id: uuid.UUID, at: datetime
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE sod_policy_deviation "
                "SET revoked_at = :at, revoked_by_user_id = :revoked_by "
                "WHERE id = :id AND revoked_at IS NULL"
            ),
            {"at": at, "revoked_by": str(revoked_by_user_id), "id": str(deviation_id)},
        )

    async def record_event(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        outcome: SodOutcome,
        actor_user_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID | None,
        detail: str,
        deviation_id: uuid.UUID | None,
        at: datetime,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO sod_event "
                "(organization_id, rule, outcome, actor_user_id, resource_type, "
                " resource_id, detail, deviation_id, occurred_at) "
                "VALUES (:organization_id, :rule, :outcome, :actor, :resource_type, "
                "        :resource_id, :detail, :deviation_id, :at) RETURNING id"
            ),
            {
                "organization_id": str(organization_id),
                "rule": rule.value,
                "outcome": outcome,
                "actor": str(actor_user_id),
                "resource_type": resource_type,
                "resource_id": str(resource_id) if resource_id is not None else None,
                "detail": detail,
                "deviation_id": str(deviation_id) if deviation_id is not None else None,
                "at": at,
            },
        )
        event_id: uuid.UUID = result.scalar_one()
        return event_id

    async def list_events(self, organization_id: uuid.UUID) -> Sequence[SodEvent]:
        result = await self._session.execute(
            text(
                "SELECT id, organization_id, rule, outcome, actor_user_id, resource_type, "
                "       resource_id, detail, deviation_id, occurred_at "
                "FROM sod_event WHERE organization_id = :organization_id "
                "ORDER BY occurred_at DESC"
            ),
            {"organization_id": str(organization_id)},
        )
        return [
            SodEvent(
                id=row.id,
                organization_id=row.organization_id,
                rule=SodRule(row.rule),
                outcome=row.outcome,
                actor_user_id=row.actor_user_id,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                detail=row.detail,
                deviation_id=row.deviation_id,
                occurred_at=row.occurred_at,
            )
            for row in result
        ]

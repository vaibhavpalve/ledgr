"""SQLAlchemy-backed repositories for IAM-109 and IAM-110, over
migrations/0016 and 0017.

Both run on the ordinary tenant-scoped session, so RLS applies. For the
register that is the requirement rather than a precaution: IAM-109's "without
asking" IS the administration_access select policy letting the owning
organization read every row, and a listing that worked only for the firm
would satisfy the SQL and miss the point.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.authz.engagement_revocation import SwitchableAdministration
from api.authz.firm_access_register import FirmAccessEntry

# The whole IAM-109 register in one statement. The LEFT JOIN on
# administration_access is what distinguishes "holds access and has never
# used it" from "was here this morning" - an INNER JOIN would silently drop
# the firm user who has never opened the client's books, which is precisely
# the row a client scanning this list should see.
_REGISTER_SQL = """
    SELECT ra.user_id,
           u.email                AS email,
           ra.granted_by_organization_id AS firm_organization_id,
           o.name                 AS firm_name,
           r.name                 AS role_name,
           ra.created_at          AS granted_at,
           ra.granted_by_user_id  AS granted_by_user_id,
           ra.expires_at          AS expires_at,
           aa.last_accessed_at    AS last_accessed_at,
           aa.first_accessed_at   AS first_accessed_at,
           coalesce(aa.access_count, 0) AS access_count
    FROM role_assignment ra
    JOIN "role" r        ON r.id = ra.role_id
    JOIN users u         ON u.id = ra.user_id
    JOIN organization o  ON o.id = ra.granted_by_organization_id
    JOIN administration a ON a.id = ra.scope_id
    LEFT JOIN administration_access aa
           ON aa.user_id = ra.user_id AND aa.administration_id = ra.scope_id
    WHERE ra.scope_type = 'administration'
      AND ra.scope_id = :administration_id
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      -- Firm grants only: provenance (0016) is what separates the firm's
      -- staff from the client's own users, who hold identically-shaped rows
      -- on the same administration.
      AND ra.granted_by_organization_id IS DISTINCT FROM a.organization_id
    ORDER BY aa.last_accessed_at DESC NULLS LAST, ra.created_at DESC
"""

# Collapses repeats inside the window. The WHERE on DO UPDATE is what makes
# it a no-op rather than a write when nothing has changed - Postgres still
# takes a row lock, but the row is not rewritten and no WAL is generated for
# an unchanged tuple.
_RECORD_ACCESS_SQL = """
    INSERT INTO administration_access
        (user_id, administration_id, first_accessed_at, last_accessed_at, access_count)
    VALUES (:user_id, :administration_id, :at, :at, 1)
    ON CONFLICT (user_id, administration_id) DO UPDATE
        SET last_accessed_at = excluded.last_accessed_at,
            access_count = administration_access.access_count + 1
        WHERE administration_access.last_accessed_at < :cutoff
    RETURNING user_id
"""

# Two ways a live grant reaches an administration, and the switcher lists
# both: an administration-scoped grant names it directly (firm staff, IAM-107;
# a client's own per-administration users), and an organization-scoped grant
# cascades to every administration that organization OWNS (ADR-011's
# _scope_covers - a Model B Owner, an Org Admin). The second arm is what puts a
# self-managed business's own books in its Owner's switcher without a second,
# redundant grant on each of them; it never reaches a client's administration
# from a firm's organization grant, because the join keys on ownership, not on
# firm_engagement - the same line api.authz.service draws. The ORDER BY
# prefers the more specific grant when a user holds both. See ADR-059.
_SWITCHER_SQL = """
    SELECT DISTINCT ON (a.id)
           a.id AS administration_id, a.legal_name, r.name AS role_name, ra.expires_at
    FROM role_assignment ra
    JOIN "role" r         ON r.id = ra.role_id
    JOIN administration a ON (ra.scope_type = 'administration' AND a.id = ra.scope_id)
                          OR (ra.scope_type = 'organization' AND a.organization_id = ra.scope_id)
    WHERE ra.user_id = :user_id
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      AND r.archived_at IS NULL
      AND a.status = 'active'
    ORDER BY a.id, (ra.scope_type = 'administration') DESC, ra.created_at DESC
"""


class SqlFirmAccessRegisterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_firm_access(
        self, *, administration_id: uuid.UUID, now: datetime
    ) -> Sequence[FirmAccessEntry]:
        result = await self._session.execute(
            text(_REGISTER_SQL),
            {"administration_id": str(administration_id), "now": now},
        )
        return [
            FirmAccessEntry(
                user_id=row.user_id,
                email=row.email,
                firm_organization_id=row.firm_organization_id,
                firm_name=row.firm_name,
                role_name=row.role_name,
                granted_at=row.granted_at,
                granted_by_user_id=row.granted_by_user_id,
                expires_at=row.expires_at,
                last_accessed_at=row.last_accessed_at,
                first_accessed_at=row.first_accessed_at,
                access_count=int(row.access_count),
            )
            for row in result
        ]

    async def record_access(
        self,
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
        window: timedelta,
    ) -> bool:
        result = await self._session.execute(
            text(_RECORD_ACCESS_SQL),
            {
                "user_id": str(user_id),
                "administration_id": str(administration_id),
                "at": at,
                "cutoff": at - window,
            },
        )
        return result.first() is not None


class SqlEngagementRevocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_engagement_exists(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM firm_engagement WHERE "
                "firm_organization_id = :firm AND administration_id = :admin "
                "AND status = 'active') AS engaged"
            ),
            {"firm": str(firm_organization_id), "admin": str(administration_id)},
        )
        return bool(result.scalar_one())

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return row.organization_id if row is not None else None

    async def revoke_engagement(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        revoked_by_user_id: uuid.UUID,
        at: datetime,
    ) -> None:
        # firm_engagement_transition_guard_trg (0001) owns the state machine
        # and sets revoked_at itself; it also checks that the caller is a
        # participant. Setting only `status` here keeps that trigger the
        # single authority on what a legal transition is.
        await self._session.execute(
            text(
                "UPDATE firm_engagement SET status = 'revoked', "
                "       revoked_by_user_id = :revoked_by "
                "WHERE firm_organization_id = :firm AND administration_id = :admin "
                "  AND status = 'active'"
            ),
            {
                "firm": str(firm_organization_id),
                "admin": str(administration_id),
                "revoked_by": str(revoked_by_user_id),
            },
        )

    async def revoke_firm_grants_on(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            text(
                "UPDATE role_assignment SET revoked_at = :at "
                "WHERE scope_type = 'administration' AND scope_id = :admin "
                "  AND granted_by_organization_id = :firm AND revoked_at IS NULL "
                "RETURNING user_id"
            ),
            {"at": at, "admin": str(administration_id), "firm": str(firm_organization_id)},
        )
        return [row.user_id for row in result]

    async def clear_administration_context(
        self, *, administration_id: uuid.UUID, user_ids: Sequence[uuid.UUID], at: datetime
    ) -> int:
        if not user_ids:
            return 0
        # Clears the context, does NOT revoke the session: losing access to
        # one client puts a firm employee back at the switcher, not signed
        # out of LEDGR. See the module docstring in api.authz.engagement_revocation.
        result = await self._session.execute(
            text(
                "UPDATE sessions SET active_administration_id = NULL "
                "WHERE active_administration_id = :admin "
                "  AND user_id = ANY(:user_ids) AND revoked_at IS NULL "
                "RETURNING id"
            ),
            {
                "admin": str(administration_id),
                "user_ids": [str(user_id) for user_id in user_ids],
            },
        )
        return len(result.all())

    async def set_active_administration(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        administration_id: uuid.UUID | None,
    ) -> bool:
        # user_id in the predicate, not only session_id: a session
        # identifier that reached the wrong hands must not be able to move
        # somebody else's session, and revoked sessions are excluded so a
        # signed-out session cannot be repositioned.
        result = await self._session.execute(
            text(
                "UPDATE sessions SET active_administration_id = :administration_id "
                "WHERE id = :session_id AND user_id = :user_id AND revoked_at IS NULL "
                "RETURNING id"
            ),
            {
                "administration_id": (
                    str(administration_id) if administration_id is not None else None
                ),
                "session_id": str(session_id),
                "user_id": str(user_id),
            },
        )
        return result.first() is not None

    async def switchable_administrations(
        self, *, user_id: uuid.UUID, now: datetime
    ) -> Sequence[SwitchableAdministration]:
        result = await self._session.execute(
            text(_SWITCHER_SQL), {"user_id": str(user_id), "now": now}
        )
        return [
            SwitchableAdministration(
                administration_id=row.administration_id,
                legal_name=row.legal_name,
                role_name=row.role_name,
                expires_at=row.expires_at,
            )
            for row in result
        ]

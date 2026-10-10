"""saved_view reads and writes (migration 0084, ADR-115).

RLS keeps every statement inside the session's organization. Views are per PERSON on top of
that, and the predicate for it is here: every statement names `user_id = :user`, the verified
token's user, so a colleague's view is never listed, renamed or archived - it reads as not found.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = "id, name, query, position, created_at"


@dataclass(frozen=True, slots=True)
class SavedView:
    id: uuid.UUID
    name: str
    query: object
    position: int
    created_at: datetime


def _view(row: Any) -> SavedView:
    query = row.query
    if isinstance(query, str):  # a driver that hands jsonb back as text
        query = json.loads(query)
    return SavedView(
        id=row.id,
        name=row.name,
        query=query,
        position=int(row.position),
        created_at=row.created_at,
    )


class LimitReached(Exception):
    """The person already has the maximum number of active views."""


class SqlSavedViewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active(self, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> list[SavedView]:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM saved_view "
                "WHERE organization_id = :org AND user_id = :user AND archived_at IS NULL "
                "ORDER BY position, created_at, id"
            ),
            {"org": str(organization_id), "user": str(user_id)},
        )
        return [_view(row) for row in result]

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        query: dict[str, object],
        limit: int,
    ) -> SavedView:
        """Appended after the person's last view. The per-person limit is checked under a
        transaction-scoped advisory lock on the person, so two tabs saving at once cannot both
        squeeze in as the twentieth."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"saved_view:{organization_id}:{user_id}"},
        )
        counted = await self._session.execute(
            text(
                "SELECT count(*) AS n, coalesce(max(position), -1) AS last FROM saved_view "
                "WHERE organization_id = :org AND user_id = :user AND archived_at IS NULL"
            ),
            {"org": str(organization_id), "user": str(user_id)},
        )
        row = counted.one()
        if int(row.n) >= limit:
            raise LimitReached
        result = await self._session.execute(
            text(
                "INSERT INTO saved_view (organization_id, user_id, name, query, position) "
                "VALUES (:org, :user, :name, CAST(:query AS jsonb), :position) "
                f"RETURNING {_COLUMNS}"
            ),
            {
                "org": str(organization_id),
                "user": str(user_id),
                "name": name,
                "query": json.dumps(query),
                "position": int(row.last) + 1,
            },
        )
        return _view(result.one())

    async def rename(
        self,
        view_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
    ) -> SavedView | None:
        result = await self._session.execute(
            text(
                "UPDATE saved_view SET name = :name "
                "WHERE id = :id AND organization_id = :org AND user_id = :user "
                "  AND archived_at IS NULL "
                f"RETURNING {_COLUMNS}"
            ),
            {"id": str(view_id), "org": str(organization_id), "user": str(user_id), "name": name},
        )
        row = result.first()
        return None if row is None else _view(row)

    async def archive(
        self, view_id: uuid.UUID, *, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE saved_view SET archived_at = now() "
                "WHERE id = :id AND organization_id = :org AND user_id = :user "
                "  AND archived_at IS NULL "
                "RETURNING id"
            ),
            {"id": str(view_id), "org": str(organization_id), "user": str(user_id)},
        )
        return result.first() is not None

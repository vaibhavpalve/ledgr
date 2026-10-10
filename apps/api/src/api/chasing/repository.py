"""The chasing tables as the HTTP routes read and write them (migration 0083, ADR-114).

Every statement runs in the request's tenant session, under RLS: a firm session reaches the
administrations it has an active engagement on, a client session its own. Reads that span a
portfolio take the whole id list at once (one query, whatever the number of clients), like the
firm worklist. Inserts read organization_id from the administration row in the same statement,
so it is always the owner's - and the 0083 trigger checks it again.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.chasing import missing
from api.chasing.model import Cadence


@dataclass(frozen=True, slots=True)
class ChaseSetting:
    enabled: bool
    cadence: Cadence
    set_at: datetime | None


#: What a client is chased with before anyone has chosen: nothing (chasing is opt-in).
DEFAULT_SETTING = ChaseSetting(enabled=False, cadence=Cadence.WEEKLY, set_at=None)


@dataclass(frozen=True, slots=True)
class ChaseFacts:
    last_chased_at: datetime | None = None
    last_requested_at: datetime | None = None
    recipient_count: int | None = None


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    request_id: uuid.UUID
    organization_id: uuid.UUID


_FACTS = """
    SELECT a.id AS administration_id,
           (SELECT max(s.sent_at) FROM chase_send s WHERE s.administration_id = a.id)
               AS last_chased_at,
           (SELECT max(r.requested_at) FROM chase_request r WHERE r.administration_id = a.id)
               AS last_requested_at,
           c.recipient_count
      FROM administration a
      LEFT JOIN chase_recipient_count c ON c.administration_id = a.id
     WHERE a.id = ANY(CAST(:ids AS uuid[]))
"""


def _ids(ids: Sequence[uuid.UUID]) -> list[str]:
    return [str(i) for i in ids]


class SqlChaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def missing_counts(self, administration_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        return await missing.missing_counts(self._session, administration_ids)

    async def missing_lines(self, administration_id: uuid.UUID) -> list[missing.MissingLine]:
        return await missing.missing_lines(self._session, administration_id)

    async def missing_count(self, administration_id: uuid.UUID) -> int:
        return await missing.missing_count(self._session, administration_id)

    async def facts(self, administration_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ChaseFacts]:
        if not administration_ids:
            return {}
        result = await self._session.execute(text(_FACTS), {"ids": _ids(administration_ids)})
        return {
            row.administration_id: ChaseFacts(
                last_chased_at=row.last_chased_at,
                last_requested_at=row.last_requested_at,
                recipient_count=(
                    int(row.recipient_count) if row.recipient_count is not None else None
                ),
            )
            for row in result
        }

    async def setting(self, administration_id: uuid.UUID) -> ChaseSetting:
        result = await self._session.execute(
            text(
                "SELECT enabled, cadence, created_at FROM chase_setting "
                "WHERE administration_id = :admin ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return DEFAULT_SETTING
        return ChaseSetting(
            enabled=bool(row.enabled), cadence=Cadence(row.cadence), set_at=row.created_at
        )

    async def record_setting(
        self,
        administration_id: uuid.UUID,
        *,
        enabled: bool,
        cadence: Cadence,
        user_id: uuid.UUID,
    ) -> ChaseSetting | None:
        """None when the administration is not visible to this session."""
        result = await self._session.execute(
            text(
                "INSERT INTO chase_setting "
                "  (organization_id, administration_id, enabled, cadence, set_by_user_id) "
                "SELECT a.organization_id, a.id, :enabled, :cadence, CAST(:user AS uuid) "
                "  FROM administration a WHERE a.id = :admin "
                "RETURNING enabled, cadence, created_at"
            ),
            {
                "admin": str(administration_id),
                "enabled": enabled,
                "cadence": cadence.value,
                "user": str(user_id),
            },
        )
        row = result.first()
        if row is None:
            return None
        return ChaseSetting(
            enabled=bool(row.enabled), cadence=Cadence(row.cadence), set_at=row.created_at
        )

    async def record_request(
        self, administration_id: uuid.UUID, *, missing_count: int, user_id: uuid.UUID
    ) -> RecordedRequest | None:
        result = await self._session.execute(
            text(
                "INSERT INTO chase_request "
                "  (organization_id, administration_id, requested_by_user_id, missing_count) "
                "SELECT a.organization_id, a.id, CAST(:user AS uuid), :missing "
                "  FROM administration a WHERE a.id = :admin "
                "RETURNING id, organization_id"
            ),
            {"admin": str(administration_id), "user": str(user_id), "missing": missing_count},
        )
        row = result.first()
        if row is None:
            return None
        return RecordedRequest(request_id=row.id, organization_id=row.organization_id)

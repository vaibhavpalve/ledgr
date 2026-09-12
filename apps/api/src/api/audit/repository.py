"""SQLAlchemy-backed AuditRepository over migration 0019.

Note what is absent: there is no update() and no delete(). Not because they
were left out for later - because no role has the privilege to execute one
and a trigger would reject it anyway (IAM-092). A repository method for a
statement the database refuses would be a lie about what this table can do.

`recorded_at` is passed in but the database overwrites it from the server
clock. It is in the signature so the Protocol matches what an in-memory fake
needs, and so the discrepancy is visible here rather than surprising someone
reading the row back.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import (
    ActorType,
    AuditCategory,
    AuditEntry,
    AuditEvent,
    AuditOutcome,
    ChainBreak,
    ChainHead,
)

_COLUMNS = """
    id, sequence_number, previous_hash, entry_hash, organization_id,
    administration_id, actor_user_id, actor_type, category, action,
    resource_type, resource_id, outcome, occurred_at, recorded_at,
    source_ip, user_agent, correlation_id, detail
"""

# sequence_number, previous_hash, entry_hash and recorded_at are absent from
# the column list on purpose: audit_log_seal_trg assigns all four, and
# passing them would only mean passing values that get overwritten.
_INSERT = f"""
    INSERT INTO audit_log (
        organization_id, administration_id, actor_user_id, actor_type,
        category, action, resource_type, resource_id, outcome,
        occurred_at, source_ip, user_agent, correlation_id, detail
    ) VALUES (
        :organization_id, :administration_id, :actor_user_id, :actor_type,
        :category, :action, :resource_type, :resource_id, :outcome,
        coalesce(cast(:occurred_at as timestamptz), now()),
        cast(:source_ip as inet), :user_agent, :correlation_id,
        cast(:detail as jsonb)
    )
    RETURNING {_COLUMNS}
"""


def _detail(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, Mapping) else {}


def _entry(row: Any) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        sequence_number=int(row.sequence_number),
        previous_hash=row.previous_hash,
        entry_hash=row.entry_hash,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        actor_user_id=row.actor_user_id,
        actor_type=ActorType(row.actor_type),
        category=AuditCategory(row.category),
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        outcome=AuditOutcome(row.outcome),
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        source_ip=str(row.source_ip) if row.source_ip is not None else None,
        user_agent=row.user_agent,
        correlation_id=row.correlation_id,
        detail=_detail(row.detail),
    )


class SqlAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent, *, recorded_at: datetime) -> AuditEntry:
        result = await self._session.execute(
            text(_INSERT),
            {
                "organization_id": str(event.organization_id),
                "administration_id": (
                    str(event.administration_id) if event.administration_id else None
                ),
                "actor_user_id": (str(event.actor_user_id) if event.actor_user_id else None),
                "actor_type": event.actor_type.value,
                "category": event.category.value,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": str(event.resource_id) if event.resource_id else None,
                "outcome": event.outcome.value,
                "occurred_at": event.occurred_at,
                "source_ip": event.source_ip,
                "user_agent": event.user_agent,
                "correlation_id": event.correlation_id,
                "detail": json.dumps(dict(event.detail), sort_keys=True),
            },
        )
        return _entry(result.one())

    async def verify_chain(self, organization_id: uuid.UUID) -> ChainBreak | None:
        # The verification runs in the DATABASE, over the stored rows,
        # recomputing each hash from the row's own fields. Doing it in Python
        # would mean transferring the whole chain and would also let a bug in
        # the transfer look like tampering.
        result = await self._session.execute(
            text(
                "SELECT broken_sequence_number, broken_entry_id, reason "
                "FROM app.verify_audit_chain(:organization_id)"
            ),
            {"organization_id": str(organization_id)},
        )
        row = result.first()
        if row is None:
            return None
        return ChainBreak(
            sequence_number=int(row.broken_sequence_number),
            entry_id=row.broken_entry_id,
            reason=row.reason,
        )

    async def chain_head(self, organization_id: uuid.UUID) -> ChainHead:
        result = await self._session.execute(
            text(
                "SELECT sequence_number, head_hash, entries "
                "FROM app.audit_chain_head(:organization_id)"
            ),
            {"organization_id": str(organization_id)},
        )
        row = result.one()
        return ChainHead(
            organization_id=organization_id,
            sequence_number=int(row.sequence_number or 0),
            head_hash=row.head_hash or "",
            entries=int(row.entries or 0),
        )

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        categories: Sequence[AuditCategory] | None = None,
        actor_user_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEntry]:
        clauses = ["organization_id = :organization_id"]
        params: dict[str, Any] = {
            "organization_id": str(organization_id),
            "limit": limit,
        }
        if categories:
            clauses.append("category = ANY(:categories)")
            params["categories"] = [c.value for c in categories]
        if actor_user_id is not None:
            clauses.append("actor_user_id = :actor_user_id")
            params["actor_user_id"] = str(actor_user_id)
        if since is not None:
            clauses.append("occurred_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("occurred_at <= :until")
            params["until"] = until

        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM audit_log WHERE {' AND '.join(clauses)} "
                "ORDER BY sequence_number DESC LIMIT :limit"
            ),
            params,
        )
        return [_entry(row) for row in result]

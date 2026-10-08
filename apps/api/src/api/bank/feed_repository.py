"""`bank_feed_connection` (migration 0077), read and written as ledgr_app under RLS."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ConnectionStatus(enum.Enum):
    PENDING = "pending"
    LINKED = "linked"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FeedConnection:
    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    bank_account_id: uuid.UUID
    provider: str
    institution_id: str
    institution_name: str | None
    provider_reference: str | None
    provider_account_id: str | None
    status: ConnectionStatus
    consent_expires_at: datetime | None
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime


_COLUMNS = (
    "id, organization_id, administration_id, bank_account_id, provider, institution_id, "
    "institution_name, provider_reference, provider_account_id, status, consent_expires_at, "
    "last_synced_at, last_error, created_at"
)


def _connection(row: Any) -> FeedConnection:
    return FeedConnection(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        bank_account_id=row.bank_account_id,
        provider=row.provider,
        institution_id=row.institution_id,
        institution_name=row.institution_name,
        provider_reference=row.provider_reference,
        provider_account_id=row.provider_account_id,
        status=ConnectionStatus(row.status),
        consent_expires_at=row.consent_expires_at,
        last_synced_at=row.last_synced_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )


class SqlBankFeedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_for_account(
        self, *, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> FeedConnection | None:
        """The pending or linked connection, if any (0077 allows one)."""
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM bank_feed_connection "
                "WHERE administration_id = :admin AND bank_account_id = :account "
                "  AND status IN ('pending', 'linked')"
            ),
            {"admin": str(administration_id), "account": str(bank_account_id)},
        )
        row = result.first()
        return None if row is None else _connection(row)

    async def latest_for_account(
        self, *, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> FeedConnection | None:
        """The most recent connection of any status: what the screen shows."""
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM bank_feed_connection "
                "WHERE administration_id = :admin AND bank_account_id = :account "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"admin": str(administration_id), "account": str(bank_account_id)},
        )
        row = result.first()
        return None if row is None else _connection(row)

    async def get(
        self, *, administration_id: uuid.UUID, connection_id: uuid.UUID
    ) -> FeedConnection | None:
        result = await self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM bank_feed_connection "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(connection_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _connection(row)

    async def create_pending(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        provider: str,
        institution_id: str,
        institution_name: str | None,
        user_id: uuid.UUID,
    ) -> FeedConnection:
        result = await self._session.execute(
            text(
                "INSERT INTO bank_feed_connection "
                "  (organization_id, administration_id, bank_account_id, provider, "
                "   institution_id, institution_name, created_by_user_id) "
                "VALUES (:org, :admin, :account, :provider, :institution, "
                "        :institution_name, :user) "
                f"RETURNING {_COLUMNS}"
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "account": str(bank_account_id),
                "provider": provider,
                "institution": institution_id,
                "institution_name": institution_name,
                "user": str(user_id),
            },
        )
        return _connection(result.one())

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        connection_id: uuid.UUID,
        **fields: object,
    ) -> FeedConnection:
        """Sets the named columns. Only the lifecycle columns are accepted; 0077's trigger refuses
        a change of account, administration, provider or bank."""
        allowed = {
            "status",
            "provider_reference",
            "provider_account_id",
            "consent_expires_at",
            "last_synced_at",
            "last_error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not a lifecycle column: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        params: dict[str, object] = {
            name: (value.value if isinstance(value, ConnectionStatus) else value)
            for name, value in fields.items()
        }
        params.update({"id": str(connection_id), "admin": str(administration_id)})
        result = await self._session.execute(
            text(
                f"UPDATE bank_feed_connection SET {assignments} "
                f"WHERE id = :id AND administration_id = :admin RETURNING {_COLUMNS}"
            ),
            params,
        )
        return _connection(result.one())

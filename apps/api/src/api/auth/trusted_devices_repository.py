"""SQLAlchemy-backed TrustedDeviceRepository, reading/writing
trusted_device through an ordinary AsyncSession - see migrations/0050_
trusted_devices.sql.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.trusted_devices import TrustedDevice

_SELECT_COLUMNS = "id, user_id, name, created_at, last_used_at, expires_at, revoked_at"


def _row_to_device(row: Row[Any]) -> TrustedDevice:
    return TrustedDevice(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


class SqlTrustedDeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        name: str | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> TrustedDevice:
        result = await self._session.execute(
            text(
                "INSERT INTO trusted_device "
                "(user_id, token_hash, name, created_at, last_used_at, expires_at) "
                "VALUES (:user_id, :token_hash, :name, :created_at, :created_at, :expires_at) "
                f"RETURNING {_SELECT_COLUMNS}"
            ),
            {
                "user_id": str(user_id),
                "token_hash": token_hash,
                "name": name,
                "created_at": created_at,
                "expires_at": expires_at,
            },
        )
        return _row_to_device(result.one())

    async def get_by_token_hash(self, token_hash: str) -> TrustedDevice | None:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM trusted_device WHERE token_hash = :token_hash"),
            {"token_hash": token_hash},
        )
        row = result.first()
        return _row_to_device(row) if row is not None else None

    async def list_for_user(self, user_id: uuid.UUID) -> list[TrustedDevice]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM trusted_device "
                "WHERE user_id = :user_id ORDER BY created_at DESC"
            ),
            {"user_id": str(user_id)},
        )
        return [_row_to_device(row) for row in result]

    async def touch_last_used(self, device_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE trusted_device SET last_used_at = :at WHERE id = :id"),
            {"id": str(device_id), "at": at},
        )

    async def revoke(self, device_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE trusted_device SET revoked_at = :at WHERE id = :id"),
            {"id": str(device_id), "at": at},
        )

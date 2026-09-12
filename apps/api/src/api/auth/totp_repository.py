"""SQLAlchemy-backed TotpRepository, reading/writing user_totp_credential
through an ordinary AsyncSession - see migrations/0006_mfa.sql.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.totp import TotpCredential

_SELECT_COLUMNS = (
    "id, user_id, wrapped_secret, secret_nonce, wrapped_dek, wrap_algorithm, "
    "kek_key_id, last_used_step, confirmed_at, created_at, revoked_at"
)


def _row_to_credential(row: Row[Any]) -> TotpCredential:
    return TotpCredential(
        id=row.id,
        user_id=row.user_id,
        wrapped_secret=bytes(row.wrapped_secret),
        secret_nonce=bytes(row.secret_nonce),
        wrapped_dek=bytes(row.wrapped_dek),
        wrap_algorithm=row.wrap_algorithm,
        kek_key_id=row.kek_key_id,
        last_used_step=row.last_used_step,
        confirmed_at=row.confirmed_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


class SqlTotpRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_user(self, user_id: uuid.UUID) -> TotpCredential | None:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM user_totp_credential WHERE user_id = :user_id"),
            {"user_id": str(user_id)},
        )
        row = result.first()
        return _row_to_credential(row) if row is not None else None

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        wrapped_secret: bytes,
        secret_nonce: bytes,
        wrapped_dek: bytes,
        wrap_algorithm: str,
        kek_key_id: str,
        confirmed_at: datetime,
        last_used_step: int,
    ) -> TotpCredential:
        result = await self._session.execute(
            text(
                "INSERT INTO user_totp_credential "
                "(user_id, wrapped_secret, secret_nonce, wrapped_dek, wrap_algorithm, "
                " kek_key_id, confirmed_at, last_used_step) "
                "VALUES "
                "(:user_id, :wrapped_secret, :secret_nonce, :wrapped_dek, :wrap_algorithm, "
                " :kek_key_id, :confirmed_at, :last_used_step) "
                f"RETURNING {_SELECT_COLUMNS}"
            ),
            {
                "user_id": str(user_id),
                "wrapped_secret": wrapped_secret,
                "secret_nonce": secret_nonce,
                "wrapped_dek": wrapped_dek,
                "wrap_algorithm": wrap_algorithm,
                "kek_key_id": kek_key_id,
                "confirmed_at": confirmed_at,
                "last_used_step": last_used_step,
            },
        )
        return _row_to_credential(result.one())

    async def update_last_used_step(self, credential_id: uuid.UUID, *, step: int) -> None:
        await self._session.execute(
            text("UPDATE user_totp_credential SET last_used_step = :step WHERE id = :id"),
            {"id": str(credential_id), "step": step},
        )

    async def revoke(self, credential_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE user_totp_credential SET revoked_at = :at WHERE id = :id"),
            {"id": str(credential_id), "at": at},
        )

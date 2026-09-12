"""SQLAlchemy-backed PasskeyRepository, reading/writing user_passkey
through an ordinary AsyncSession - see
migrations/0005_webauthn_passkeys.sql.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.passkeys import Passkey

_SELECT_COLUMNS = (
    "id, user_id, name, credential_id, public_key, sign_count, transports, "
    "aaguid, backup_eligible, backed_up, authenticator_attachment, "
    "created_at, last_used_at, revoked_at"
)


def _row_to_passkey(row: Row[Any]) -> Passkey:
    return Passkey(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        credential_id=bytes(row.credential_id),
        public_key=bytes(row.public_key),
        sign_count=row.sign_count,
        transports=list(row.transports or []),
        aaguid=row.aaguid,
        backup_eligible=row.backup_eligible,
        backed_up=row.backed_up,
        authenticator_attachment=row.authenticator_attachment,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class SqlPasskeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: list[str],
        aaguid: str | None,
        backup_eligible: bool,
        backed_up: bool,
        authenticator_attachment: str | None,
    ) -> Passkey:
        result = await self._session.execute(
            text(
                "INSERT INTO user_passkey "
                "(user_id, name, credential_id, public_key, sign_count, transports, "
                " aaguid, backup_eligible, backed_up, authenticator_attachment) "
                "VALUES "
                "(:user_id, :name, :credential_id, :public_key, :sign_count, :transports, "
                " :aaguid, :backup_eligible, :backed_up, :authenticator_attachment) "
                f"RETURNING {_SELECT_COLUMNS}"
            ),
            {
                "user_id": str(user_id),
                "name": name,
                "credential_id": credential_id,
                "public_key": public_key,
                "sign_count": sign_count,
                "transports": transports,
                "aaguid": aaguid,
                "backup_eligible": backup_eligible,
                "backed_up": backed_up,
                "authenticator_attachment": authenticator_attachment,
            },
        )
        return _row_to_passkey(result.one())

    async def get_by_credential_id(self, credential_id: bytes) -> Passkey | None:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM user_passkey WHERE credential_id = :credential_id"
            ),
            {"credential_id": credential_id},
        )
        row = result.first()
        return _row_to_passkey(row) if row is not None else None

    async def get_by_id(self, passkey_id: uuid.UUID) -> Passkey | None:
        result = await self._session.execute(
            text(f"SELECT {_SELECT_COLUMNS} FROM user_passkey WHERE id = :id"),
            {"id": str(passkey_id)},
        )
        row = result.first()
        return _row_to_passkey(row) if row is not None else None

    async def list_for_user(self, user_id: uuid.UUID) -> list[Passkey]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM user_passkey "
                "WHERE user_id = :user_id ORDER BY created_at"
            ),
            {"user_id": str(user_id)},
        )
        return [_row_to_passkey(row) for row in result]

    async def update_after_authentication(
        self, passkey_id: uuid.UUID, *, sign_count: int, backed_up: bool, at: datetime
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE user_passkey "
                "SET sign_count = :sign_count, backed_up = :backed_up, last_used_at = :at "
                "WHERE id = :id"
            ),
            {"id": str(passkey_id), "sign_count": sign_count, "backed_up": backed_up, "at": at},
        )

    async def revoke(self, passkey_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE user_passkey SET revoked_at = :at WHERE id = :id"),
            {"id": str(passkey_id), "at": at},
        )

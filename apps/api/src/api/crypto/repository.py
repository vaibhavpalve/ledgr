"""SQLAlchemy-backed AdministrationKeyRepository. Reads and writes
administration_encryption_key through an ordinary tenant-scoped AsyncSession
(the same kind api.db.get_db_session yields) - row-level security applies
to this table exactly as it does to any other; see
migrations/0002_encryption_keys.sql and
docs/decisions/ADR-004-envelope-encryption.md.

The cross-tenant sweep (list_active_wrapped_under, used by
rewrap_after_kek_rotation) is the one operation here that legitimately spans
every tenant. It only works when the session is connected as ledgr_ops
(BYPASSRLS, narrowly granted on this one table) rather than ledgr_app - see
api.db.get_ops_engine.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.crypto.envelope import EncryptionKeyRecord
from api.crypto.kms import WrappedKey

_SELECT_COLUMNS = (
    "id, administration_id, key_version, wrapped_dek, wrap_algorithm, kek_key_id, status"
)


def _row_to_record(row: Row[Any]) -> EncryptionKeyRecord:
    return EncryptionKeyRecord(
        id=row.id,
        administration_id=row.administration_id,
        key_version=row.key_version,
        wrapped_dek=bytes(row.wrapped_dek) if row.wrapped_dek is not None else None,
        wrap_algorithm=row.wrap_algorithm,
        kek_key_id=row.kek_key_id,
        status=row.status,
    )


class SqlAdministrationKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM administration_encryption_key "
                "WHERE administration_id = :administration_id AND status = 'active'"
            ),
            {"administration_id": str(administration_id)},
        )
        row = result.first()
        return _row_to_record(row) if row is not None else None

    async def get_latest(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM administration_encryption_key "
                "WHERE administration_id = :administration_id "
                "ORDER BY key_version DESC LIMIT 1"
            ),
            {"administration_id": str(administration_id)},
        )
        row = result.first()
        return _row_to_record(row) if row is not None else None

    async def get_by_version(
        self, administration_id: uuid.UUID, key_version: int
    ) -> EncryptionKeyRecord | None:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM administration_encryption_key "
                "WHERE administration_id = :administration_id AND key_version = :key_version"
            ),
            {"administration_id": str(administration_id), "key_version": key_version},
        )
        row = result.first()
        return _row_to_record(row) if row is not None else None

    async def insert_active(
        self,
        *,
        administration_id: uuid.UUID,
        key_version: int,
        wrapped: WrappedKey,
        rotation_reason: str,
    ) -> EncryptionKeyRecord:
        result = await self._session.execute(
            text(
                "INSERT INTO administration_encryption_key "
                "(organization_id, administration_id, key_version, wrapped_dek, "
                " wrap_algorithm, kek_key_id, status, rotation_reason) "
                "VALUES ("
                "  (SELECT organization_id FROM administration WHERE id = :administration_id), "
                "  :administration_id, :key_version, :wrapped_dek, :wrap_algorithm, "
                "  :kek_key_id, 'active', :rotation_reason"
                f") RETURNING {_SELECT_COLUMNS}"
            ),
            {
                "administration_id": str(administration_id),
                "key_version": key_version,
                "wrapped_dek": wrapped.ciphertext,
                "wrap_algorithm": wrapped.algorithm,
                "kek_key_id": wrapped.kek_key_id,
                "rotation_reason": rotation_reason,
            },
        )
        return _row_to_record(result.one())

    async def retire(self, key_id: uuid.UUID) -> None:
        await self._session.execute(
            text("UPDATE administration_encryption_key SET status = 'retired' WHERE id = :id"),
            {"id": str(key_id)},
        )

    async def revoke(self, key_id: uuid.UUID, *, revoked_by_user_id: uuid.UUID | None) -> None:
        await self._session.execute(
            text(
                "UPDATE administration_encryption_key "
                "SET status = 'revoked', revoked_by_user_id = :revoked_by_user_id "
                "WHERE id = :id"
            ),
            {
                "id": str(key_id),
                "revoked_by_user_id": str(revoked_by_user_id) if revoked_by_user_id else None,
            },
        )

    async def list_active_wrapped_under(self, kek_key_id: str) -> list[EncryptionKeyRecord]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM administration_encryption_key "
                "WHERE status = 'active' AND kek_key_id = :kek_key_id"
            ),
            {"kek_key_id": kek_key_id},
        )
        return [_row_to_record(row) for row in result]

    async def rewrap_in_place(self, key_id: uuid.UUID, wrapped: WrappedKey) -> None:
        await self._session.execute(
            text(
                "UPDATE administration_encryption_key "
                "SET wrapped_dek = :wrapped_dek, wrap_algorithm = :wrap_algorithm, "
                "    kek_key_id = :kek_key_id "
                "WHERE id = :id"
            ),
            {
                "id": str(key_id),
                "wrapped_dek": wrapped.ciphertext,
                "wrap_algorithm": wrapped.algorithm,
                "kek_key_id": wrapped.kek_key_id,
            },
        )

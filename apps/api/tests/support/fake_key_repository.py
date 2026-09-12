"""In-memory AdministrationKeyRepository double, for testing envelope
encryption logic (tests/crypto/) without a database. The real
SQLAlchemy-backed repository (api.crypto.repository) is exercised
separately by tests/integration/test_encryption_key_isolation.py.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from api.crypto.envelope import EncryptionKeyRecord
from api.crypto.kms import WrappedKey


class InMemoryAdministrationKeyRepository:
    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, EncryptionKeyRecord] = {}

    async def get_active(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None:
        return next(
            (
                r
                for r in self._rows.values()
                if r.administration_id == administration_id and r.status == "active"
            ),
            None,
        )

    async def get_latest(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None:
        candidates = [r for r in self._rows.values() if r.administration_id == administration_id]
        return max(candidates, key=lambda r: r.key_version) if candidates else None

    async def get_by_version(
        self, administration_id: uuid.UUID, key_version: int
    ) -> EncryptionKeyRecord | None:
        return next(
            (
                r
                for r in self._rows.values()
                if r.administration_id == administration_id and r.key_version == key_version
            ),
            None,
        )

    async def insert_active(
        self,
        *,
        administration_id: uuid.UUID,
        key_version: int,
        wrapped: WrappedKey,
        rotation_reason: str,
    ) -> EncryptionKeyRecord:
        # Mirrors administration_encryption_key_one_active_idx (a partial
        # unique index in migrations/0002_encryption_keys.sql): at most one
        # 'active' row per administration. Enforcing it here too means a
        # bug like "insert the new active key before retiring the old one"
        # fails a fast in-memory test instead of only surfacing against a
        # real Postgres nobody ran in this environment.
        if await self.get_active(administration_id) is not None:
            raise ValueError(
                f"administration {administration_id} already has an active key - "
                "retire it before inserting a new active one"
            )
        record = EncryptionKeyRecord(
            id=uuid.uuid4(),
            administration_id=administration_id,
            key_version=key_version,
            wrapped_dek=wrapped.ciphertext,
            wrap_algorithm=wrapped.algorithm,
            kek_key_id=wrapped.kek_key_id,
            status="active",
        )
        self._rows[record.id] = record
        return record

    async def retire(self, key_id: uuid.UUID) -> None:
        self._rows[key_id] = replace(self._rows[key_id], status="retired")

    async def revoke(self, key_id: uuid.UUID, *, revoked_by_user_id: uuid.UUID | None) -> None:
        self._rows[key_id] = replace(self._rows[key_id], status="revoked", wrapped_dek=None)

    async def list_active_wrapped_under(self, kek_key_id: str) -> list[EncryptionKeyRecord]:
        return [
            r for r in self._rows.values() if r.status == "active" and r.kek_key_id == kek_key_id
        ]

    async def rewrap_in_place(self, key_id: uuid.UUID, wrapped: WrappedKey) -> None:
        self._rows[key_id] = replace(
            self._rows[key_id],
            wrapped_dek=wrapped.ciphertext,
            wrap_algorithm=wrapped.algorithm,
            kek_key_id=wrapped.kek_key_id,
        )

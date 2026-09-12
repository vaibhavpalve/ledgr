"""In-memory PasskeyRepository double for testing api.auth.passkeys
without a database. Enforces the same UNIQUE(credential_id) constraint the
real schema does (migrations/0005_webauthn_passkeys.sql) so a bug that
would fail against real Postgres fails here too - see the encryption-key
and Google-identity harnesses for the precedent this follows.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from api.auth.passkeys import Passkey


class InMemoryPasskeyRepository:
    def __init__(self) -> None:
        self._passkeys: dict[uuid.UUID, Passkey] = {}

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
        if any(p.credential_id == credential_id for p in self._passkeys.values()):
            raise ValueError(f"credential_id {credential_id!r} is already registered")

        passkey = Passkey(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            aaguid=aaguid,
            backup_eligible=backup_eligible,
            backed_up=backed_up,
            authenticator_attachment=authenticator_attachment,
            created_at=datetime.now(UTC),
            last_used_at=None,
            revoked_at=None,
        )
        self._passkeys[passkey.id] = passkey
        return passkey

    async def get_by_credential_id(self, credential_id: bytes) -> Passkey | None:
        return next((p for p in self._passkeys.values() if p.credential_id == credential_id), None)

    async def get_by_id(self, passkey_id: uuid.UUID) -> Passkey | None:
        return self._passkeys.get(passkey_id)

    async def list_for_user(self, user_id: uuid.UUID) -> list[Passkey]:
        return sorted(
            (p for p in self._passkeys.values() if p.user_id == user_id),
            key=lambda p: p.created_at,
        )

    async def update_after_authentication(
        self, passkey_id: uuid.UUID, *, sign_count: int, backed_up: bool, at: datetime
    ) -> None:
        self._passkeys[passkey_id] = replace(
            self._passkeys[passkey_id], sign_count=sign_count, backed_up=backed_up, last_used_at=at
        )

    async def revoke(self, passkey_id: uuid.UUID, *, at: datetime) -> None:
        self._passkeys[passkey_id] = replace(self._passkeys[passkey_id], revoked_at=at)

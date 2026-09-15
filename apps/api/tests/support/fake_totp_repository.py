"""In-memory TotpRepository double for testing api.auth.totp without a
database. Enforces the same UNIQUE(user_id) constraint the real schema
does (migrations/0006_mfa.sql) so a bug that would fail against real
Postgres fails here too - see the other fake repositories for the
precedent.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from api.auth.totp import TotpCredential


class InMemoryTotpRepository:
    def __init__(self) -> None:
        self._credentials: dict[uuid.UUID, TotpCredential] = {}

    async def get_for_user(self, user_id: uuid.UUID) -> TotpCredential | None:
        return next(
            (
                c
                for c in self._credentials.values()
                if c.user_id == user_id and c.revoked_at is None
            ),
            None,
        )

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
        # Mirrors user_totp_credential_one_active_idx: at most one ACTIVE
        # (non-revoked) credential per user, but re-enrollment after a
        # revoke must succeed - see migrations/0006_mfa.sql.
        active = next(
            (
                c
                for c in self._credentials.values()
                if c.user_id == user_id and c.revoked_at is None
            ),
            None,
        )
        if active is not None:
            raise ValueError(f"user {user_id} already has an active TOTP credential")

        credential = TotpCredential(
            id=uuid.uuid4(),
            user_id=user_id,
            wrapped_secret=wrapped_secret,
            secret_nonce=secret_nonce,
            wrapped_dek=wrapped_dek,
            wrap_algorithm=wrap_algorithm,
            kek_key_id=kek_key_id,
            last_used_step=last_used_step,
            confirmed_at=confirmed_at,
            created_at=confirmed_at,
            revoked_at=None,
        )
        self._credentials[credential.id] = credential
        return credential

    async def update_last_used_step(self, credential_id: uuid.UUID, *, step: int) -> None:
        self._credentials[credential_id] = replace(
            self._credentials[credential_id], last_used_step=step
        )

    async def revoke(self, credential_id: uuid.UUID, *, at: datetime) -> None:
        self._credentials[credential_id] = replace(self._credentials[credential_id], revoked_at=at)

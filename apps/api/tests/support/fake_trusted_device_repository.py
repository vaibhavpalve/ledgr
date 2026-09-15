"""In-memory TrustedDeviceRepository double for testing
api.auth.trusted_devices without a database - see fake_totp_repository.py
for the same precedent this mirrors.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from api.auth.trusted_devices import TrustedDevice


class InMemoryTrustedDeviceRepository:
    def __init__(self) -> None:
        self._devices: dict[uuid.UUID, TrustedDevice] = {}
        # TrustedDevice itself carries no token_hash (the real schema keeps
        # it out of every SELECT this repository's callers use it for -
        # see trusted_devices_repository.py's _SELECT_COLUMNS), so the hash
        # is tracked here, the same way the real table's WHERE clause finds
        # a row without ever handing the hash back out.
        self._hash_by_id: dict[uuid.UUID, str] = {}

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        name: str | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> TrustedDevice:
        device = TrustedDevice(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            created_at=created_at,
            last_used_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        self._devices[device.id] = device
        self._hash_by_id[device.id] = token_hash
        return device

    async def get_by_token_hash(self, token_hash: str) -> TrustedDevice | None:
        for device_id, stored_hash in self._hash_by_id.items():
            if stored_hash == token_hash:
                return self._devices[device_id]
        return None

    async def list_for_user(self, user_id: uuid.UUID) -> list[TrustedDevice]:
        return sorted(
            (d for d in self._devices.values() if d.user_id == user_id),
            key=lambda d: d.created_at,
            reverse=True,
        )

    async def touch_last_used(self, device_id: uuid.UUID, *, at: datetime) -> None:
        self._devices[device_id] = replace(self._devices[device_id], last_used_at=at)

    async def revoke(self, device_id: uuid.UUID, *, at: datetime) -> None:
        self._devices[device_id] = replace(self._devices[device_id], revoked_at=at)

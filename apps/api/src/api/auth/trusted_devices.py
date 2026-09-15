"""Trusted-device remembering (IAM-011 scoping, ADR-061): a device that has
already proven MFA once may skip re-proving it for a bounded, revocable
window, rather than an open-ended opt-out of "MFA is mandatory, no
opt-out." See docs/decisions/ADR-061-trusted-devices.md for the full
reasoning; the short version is that a trusted_device row is issued only
AFTER mfa_totp_verify or mfa_passkey_verify_finish succeeds - it is proof
this browser already cleared the gate once, never a way to avoid clearing
it at all.

Deliberately the same shape as api.auth.sessions: an opaque bearer token
whose SHA-256 hash alone is persisted (the raw value exists only at
issuance, in the response body, and cannot be recovered from storage
afterward), a revoked_at column rather than a DELETE, and one Protocol so
the SQL-backed repository and an in-memory test double satisfy the exact
same interface (tests/support/fake_trusted_device_repository.py).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

_TOKEN_BYTES = 32  # 256 bits of entropy, matching api.auth.sessions


@dataclass(frozen=True, slots=True)
class TrustedDevice:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class TrustedDeviceRepository(Protocol):
    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        name: str | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> TrustedDevice: ...

    async def get_by_token_hash(self, token_hash: str) -> TrustedDevice | None: ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[TrustedDevice]: ...

    async def touch_last_used(self, device_id: uuid.UUID, *, at: datetime) -> None: ...

    async def revoke(self, device_id: uuid.UUID, *, at: datetime) -> None: ...


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TrustedDeviceService:
    def __init__(
        self,
        repository: TrustedDeviceRepository,
        *,
        lifetime_days: int = 7,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._lifetime = timedelta(days=lifetime_days)
        self._clock = clock

    async def issue(self, *, user_id: uuid.UUID, name: str | None) -> tuple[TrustedDevice, str]:
        """Returns the device record AND the raw bearer token - the only
        moment the raw token is ever available, exactly like
        api.auth.sessions.SessionService.issue_session. Called only from
        an MFA-verification handler that just succeeded (mfa_totp_verify,
        mfa_passkey_verify_finish) - this module has no opinion on when
        that is appropriate, only on how the resulting grant is stored.
        """
        raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = self._clock()
        device = await self._repository.create(
            user_id=user_id,
            token_hash=_hash_token(raw_token),
            name=name,
            created_at=now,
            expires_at=now + self._lifetime,
        )
        return device, raw_token

    async def check(self, *, user_id: uuid.UUID, raw_token: str) -> bool:
        """True only for a token that is this user's own, unrevoked, and
        unexpired. Every other outcome - not found, wrong user, revoked,
        expired - is silently False: an invalid trusted-device token must
        fall back to an ordinary MFA prompt, never raise or distinguish
        WHY it failed. Distinguishing reasons would turn this into an
        oracle a caller could use to enumerate valid-looking tokens or
        confirm a guess about another user's device.
        """
        device = await self._repository.get_by_token_hash(_hash_token(raw_token))
        if device is None or device.user_id != user_id:
            return False
        now = self._clock()
        if device.is_revoked or now >= device.expires_at:
            return False
        await self._repository.touch_last_used(device.id, at=now)
        return True

    async def revoke(self, device_id: uuid.UUID) -> None:
        await self._repository.revoke(device_id, at=self._clock())

    async def list_for_user(self, user_id: uuid.UUID) -> list[TrustedDevice]:
        return await self._repository.list_for_user(user_id)

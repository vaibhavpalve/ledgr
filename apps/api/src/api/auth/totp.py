"""TOTP (Time-based One-Time Password) enrollment and verification
(IAM-012). One of exactly two MFA factor mechanisms this system supports
- see api.auth.mfa.MfaFactorKind for the closed set SMS is deliberately
excluded from.

The TOTP secret is field-level encrypted (SEC-024), distinct from the
per-administration document encryption in api.crypto.envelope (SEC-022):
a fresh DEK is generated per credential, wrapped by the SAME
KeyManagementService used for documents (api.crypto.kms), and only the
wrapped form is ever persisted - the raw secret exists only transiently,
during enrollment and during a verify_code call.

Enrollment is two-step and stateless between the steps, matching
api.auth.google_oidc and api.auth.passkeys: begin_enrollment generates a
secret and returns it to the caller (never persisted at this point);
confirm_enrollment requires the caller to submit BOTH the secret AND a
valid code generated from it, proving the user's authenticator app
actually captured it correctly, before anything is encrypted and stored.

Step arithmetic uses timezone-AWARE UTC datetimes throughout, deliberately
- pyotp.TOTP.timecode() falls back to interpreting a naive datetime in the
server's LOCAL timezone (via time.mktime) rather than UTC, which would
make verification silently depend on server timezone configuration. This
was confirmed by reading pyotp's source before relying on it, not assumed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.crypto.kms import KeyManagementService, WrappedKey

_NONCE_LENGTH = 12
_DEK_LENGTH = 32
_INTERVAL_SECONDS = 30
_VERIFICATION_WINDOW_STEPS = 1  # +/- 1 step (~30s) of clock drift tolerance


class TotpError(Exception):
    """Base class for anything wrong with a TOTP enrollment or
    verification attempt.
    """


class InvalidTotpCodeError(TotpError):
    """The submitted code does not match any step in the accepted window."""


class TotpCodeAlreadyUsedError(InvalidTotpCodeError):
    """The code is valid for a time step, but that step (or an earlier
    one) has already been used - replay protection. A code is usable
    exactly once, even within its ~30-90s window of validity, closing the
    gap an attacker who intercepts a valid code in transit would
    otherwise have.
    """


class TotpNotEnrolledError(TotpError):
    """No confirmed, non-revoked TOTP credential exists for this user."""


class TotpAlreadyEnrolledError(TotpError):
    """A confirmed, non-revoked TOTP credential already exists; revoke it
    before enrolling a new one.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TotpEnrollment:
    # base32 - caller must hold this and pass it to confirm_enrollment;
    # never persisted here.
    secret: str
    provisioning_uri: str  # otpauth:// URI, e.g. for rendering a QR code


@dataclass(frozen=True, slots=True)
class TotpCredential:
    id: uuid.UUID
    user_id: uuid.UUID
    wrapped_secret: bytes
    secret_nonce: bytes
    wrapped_dek: bytes
    wrap_algorithm: str
    kek_key_id: str
    last_used_step: int | None
    confirmed_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_active(self) -> bool:
        return self.is_confirmed and not self.is_revoked


class TotpRepository(Protocol):
    async def get_for_user(self, user_id: uuid.UUID) -> TotpCredential | None: ...

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
    ) -> TotpCredential: ...

    async def update_last_used_step(self, credential_id: uuid.UUID, *, step: int) -> None: ...

    async def revoke(self, credential_id: uuid.UUID, *, at: datetime) -> None: ...


def _step_at(secret: str, step: int) -> str:
    totp = pyotp.TOTP(secret, interval=_INTERVAL_SECONDS)
    step_start = datetime.fromtimestamp(step * _INTERVAL_SECONDS, tz=UTC)
    return totp.at(step_start)


def _current_step(now: datetime) -> int:
    return int(now.timestamp() // _INTERVAL_SECONDS)


def _find_matching_step(
    secret: str, code: str, *, now: datetime, after_step: int | None
) -> int | None:
    current = _current_step(now)
    for offset in range(-_VERIFICATION_WINDOW_STEPS, _VERIFICATION_WINDOW_STEPS + 1):
        step = current + offset
        if after_step is not None and step <= after_step:
            continue
        if _step_at(secret, step) == code:
            return step
    return None


def _aad(user_id: uuid.UUID) -> bytes:
    # Binds ciphertext to the specific user it belongs to, the same
    # tenant-binding principle as api.crypto.envelope's AAD - moving this
    # ciphertext to a different user's row would fail authentication
    # rather than silently decrypting as someone else's secret.
    return f"totp-secret:{user_id}".encode()


class TotpService:
    def __init__(
        self,
        repository: TotpRepository,
        kms: KeyManagementService,
        *,
        issuer: str = "LEDGR",
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._kms = kms
        self._issuer = issuer
        self._clock = clock

    def begin_enrollment(self, *, account_name: str) -> TotpEnrollment:
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret, interval=_INTERVAL_SECONDS)
        uri = totp.provisioning_uri(name=account_name, issuer_name=self._issuer)
        return TotpEnrollment(secret=secret, provisioning_uri=uri)

    async def confirm_enrollment(
        self, *, user_id: uuid.UUID, secret: str, code: str
    ) -> TotpCredential:
        existing = await self._repository.get_for_user(user_id)
        if existing is not None and existing.is_active:
            raise TotpAlreadyEnrolledError

        now = self._clock()
        matched_step = _find_matching_step(secret, code, now=now, after_step=None)
        if matched_step is None:
            raise InvalidTotpCodeError

        dek = os.urandom(_DEK_LENGTH)
        wrapped_dek = await self._kms.wrap_key(dek)
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = AESGCM(dek).encrypt(nonce, secret.encode("ascii"), _aad(user_id))

        return await self._repository.create(
            user_id=user_id,
            wrapped_secret=ciphertext,
            secret_nonce=nonce,
            wrapped_dek=wrapped_dek.ciphertext,
            wrap_algorithm=wrapped_dek.algorithm,
            kek_key_id=wrapped_dek.kek_key_id,
            confirmed_at=now,
            last_used_step=matched_step,
        )

    async def verify_code(self, *, user_id: uuid.UUID, code: str) -> TotpCredential:
        credential = await self._repository.get_for_user(user_id)
        if credential is None or not credential.is_active:
            raise TotpNotEnrolledError

        now = self._clock()
        secret = await self._decrypt_secret(credential, user_id=user_id)

        matched_step = _find_matching_step(
            secret, code, now=now, after_step=credential.last_used_step
        )
        if matched_step is None:
            # Distinguish "wrong code" from "correct code, already used"
            # so replay attempts are identifiable separately from typos.
            if _find_matching_step(secret, code, now=now, after_step=None) is not None:
                raise TotpCodeAlreadyUsedError
            raise InvalidTotpCodeError

        await self._repository.update_last_used_step(credential.id, step=matched_step)
        return credential

    async def revoke(self, user_id: uuid.UUID) -> None:
        credential = await self._repository.get_for_user(user_id)
        if credential is None:
            raise TotpNotEnrolledError
        await self._repository.revoke(credential.id, at=self._clock())

    async def _decrypt_secret(self, credential: TotpCredential, *, user_id: uuid.UUID) -> str:
        dek = await self._kms.unwrap_key(
            WrappedKey(
                ciphertext=credential.wrapped_dek,
                algorithm=credential.wrap_algorithm,
                kek_key_id=credential.kek_key_id,
            )
        )
        plaintext = AESGCM(dek).decrypt(
            credential.secret_nonce, credential.wrapped_secret, _aad(user_id)
        )
        return plaintext.decode("ascii")

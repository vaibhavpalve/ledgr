"""Envelope encryption for document/attachment storage (SEC-022, IAM-004).

Key hierarchy (see docs/decisions/ADR-004-envelope-encryption.md):

    KMS-held KEK (never leaves the vault)
      -- wraps -->  one DEK per administration (administration_encryption_key)
           -- encrypts -->  individual document/attachment bytes (AES-256-GCM)

The DEK's raw bytes exist only transiently in process memory, for the
duration of a single encrypt or decrypt call, and are never logged, cached
across requests, or persisted anywhere - only the KMS-wrapped ciphertext is
ever written to the database (see migrations/0002_encryption_keys.sql).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from api.crypto.kms import KeyManagementService, WrappedKey

_NONCE_LENGTH = 12  # 96-bit nonce, standard for AES-GCM
_DEK_LENGTH = 32  # AES-256


@dataclass(frozen=True, slots=True)
class EncryptionKeyRecord:
    """Mirrors one row of administration_encryption_key. wrapped_dek is
    None only when status == 'revoked' - the database tombstones revoked
    key material (see the migration's transition-guard trigger).
    """

    id: uuid.UUID
    administration_id: uuid.UUID
    key_version: int
    wrapped_dek: bytes | None
    wrap_algorithm: str
    kek_key_id: str
    status: str


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    administration_id: uuid.UUID
    key_version: int
    nonce: bytes
    ciphertext: bytes  # includes the GCM authentication tag


class KeyRevokedError(Exception):
    """Raised when an operation needs a tenant's key and it has been
    revoked. IAM-004 requires revocation to actually and permanently deny
    access, not merely discourage it - this exception is that guarantee
    surfacing at the point of use.
    """


class AdministrationKeyRepository(Protocol):
    """Persistence boundary for administration_encryption_key. Kept
    separate from EnvelopeEncryptionService so the rotation/encryption
    logic can be unit-tested against an in-memory fake
    (tests/support/fake_key_repository.py) independent of the database;
    the real SQLAlchemy-backed implementation (repository.py) is exercised
    by tests/integration/test_encryption_key_isolation.py, which proves the
    RLS wiring holds.
    """

    async def get_active(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None: ...

    async def get_latest(self, administration_id: uuid.UUID) -> EncryptionKeyRecord | None:
        """The highest-key_version row for this administration, regardless
        of status - unlike get_active, this also returns a revoked row.
        Needed to tell "never provisioned" apart from "revoked" (both look
        like "no active key" to get_active), and to keep version numbering
        monotonic across a revoke-then-reprovision cycle.
        """
        ...

    async def get_by_version(
        self, administration_id: uuid.UUID, key_version: int
    ) -> EncryptionKeyRecord | None: ...

    async def insert_active(
        self,
        *,
        administration_id: uuid.UUID,
        key_version: int,
        wrapped: WrappedKey,
        rotation_reason: str,
    ) -> EncryptionKeyRecord: ...

    async def retire(self, key_id: uuid.UUID) -> None: ...

    async def revoke(self, key_id: uuid.UUID, *, revoked_by_user_id: uuid.UUID | None) -> None: ...

    async def list_active_wrapped_under(self, kek_key_id: str) -> list[EncryptionKeyRecord]: ...

    async def rewrap_in_place(self, key_id: uuid.UUID, wrapped: WrappedKey) -> None: ...


async def generate_wrapped_dek(kms: KeyManagementService) -> WrappedKey:
    """A fresh DEK, wrapped under the current KEK, for a caller that persists
    the row ITSELF rather than through this service.

    There is exactly one such caller, and it is not a shortcut around
    `EnvelopeEncryptionService.provision_key` below: migration 0002's
    `app.create_firm_client_administration` inserts the client organization,
    the administration, the engagement AND the encryption key in one
    SECURITY DEFINER call, precisely so no window exists in which a client
    administration holds documents it has no key for. That function needs the
    wrapped material as arguments, so the generation has to happen before it
    runs — and it belongs here, beside `_DEK_LENGTH`, rather than as a second
    place that decides how long a DEK is.
    """
    return await kms.wrap_key(os.urandom(_DEK_LENGTH))


class EnvelopeEncryptionService:
    def __init__(self, kms: KeyManagementService, repository: AdministrationKeyRepository) -> None:
        self._kms = kms
        self._repository = repository

    async def provision_key(
        self, administration_id: uuid.UUID, *, rotation_reason: str = "initial_provisioning"
    ) -> EncryptionKeyRecord:
        """Generates a fresh DEK, wraps it under the current KEK, and
        persists it as the administration's active key. Used both for a
        brand-new administration and as the second half of rotate_key
        below.
        """
        wrapped = await generate_wrapped_dek(self._kms)
        # get_latest, not get_active: a revoked row must still count for
        # version numbering, or re-provisioning after a revoke would try to
        # reuse key_version=1 and collide with the (administration_id,
        # key_version) uniqueness constraint on the already-revoked row.
        existing = await self._repository.get_latest(administration_id)
        next_version = 1 if existing is None else existing.key_version + 1
        return await self._repository.insert_active(
            administration_id=administration_id,
            key_version=next_version,
            wrapped=wrapped,
            rotation_reason=rotation_reason,
        )

    async def rotate_key(
        self, administration_id: uuid.UUID, *, reason: str = "scheduled_annual"
    ) -> EncryptionKeyRecord:
        """SEC-023: rotation with no downtime. The old DEK is retired, not
        destroyed - documents already encrypted under it stay decryptable
        via their recorded key_version. New writes use the new version.
        No document content is re-encrypted by this call; that's the whole
        point of envelope encryption's cheap rotation - see ADR-004.

        Retires the current key BEFORE provisioning the new one, not after:
        at most one row may be 'active' for a given administration at a
        time (enforced by administration_encryption_key_one_active_idx in
        migrations/0002_encryption_keys.sql), so inserting the new active
        row first would collide with the still-active old one.
        """
        current = await self._repository.get_active(administration_id)
        if current is not None:
            await self._repository.retire(current.id)
        return await self.provision_key(administration_id, rotation_reason=reason)

    async def revoke_key(
        self, administration_id: uuid.UUID, *, revoked_by_user_id: uuid.UUID | None = None
    ) -> None:
        """IAM-004: after this call, every document ever encrypted for this
        administration is permanently unreadable, including by LEDGR
        itself - the wrapped DEK material is deleted (the migration's
        tombstone trigger nulls wrapped_dek on the revoked row), and the
        raw DEK was never stored anywhere to begin with. No other
        administration's key is touched.
        """
        current = await self._repository.get_active(administration_id)
        if current is not None:
            await self._repository.revoke(current.id, revoked_by_user_id=revoked_by_user_id)

    async def encrypt(self, administration_id: uuid.UUID, plaintext: bytes) -> EncryptedPayload:
        # get_latest, not get_active: a revoked administration has no
        # 'active' row at all, and get_active alone can't distinguish that
        # from "never provisioned" - both would otherwise raise the same
        # generic ValueError instead of the KeyRevokedError callers need to
        # tell "this tenant was cut off" apart from "this tenant was never
        # set up."
        record = await self._repository.get_latest(administration_id)
        if record is None:
            raise ValueError(f"administration {administration_id} has no active encryption key")
        if record.status == "revoked":
            raise KeyRevokedError(f"administration {administration_id}'s key has been revoked")
        if record.status != "active" or record.wrapped_dek is None:
            # Shouldn't happen in correct usage: retire() only ever runs in
            # the same operation that immediately creates a new active key
            # (see rotate_key), so the latest row is always 'active' or
            # 'revoked'. Fail loudly rather than silently encrypt under a
            # stale key if that invariant is ever violated.
            raise ValueError(
                f"administration {administration_id}'s latest key is unexpectedly "
                f"{record.status!r}, not 'active'"
            )

        dek = await self._unwrap(record)
        aesgcm = AESGCM(dek)
        nonce = os.urandom(_NONCE_LENGTH)
        aad = _associated_data(administration_id, record.key_version)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
        return EncryptedPayload(
            administration_id=administration_id,
            key_version=record.key_version,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    async def decrypt(self, payload: EncryptedPayload) -> bytes:
        record = await self._repository.get_by_version(
            payload.administration_id, payload.key_version
        )
        if record is None:
            raise ValueError(
                f"administration {payload.administration_id} has no key version "
                f"{payload.key_version}"
            )
        if record.wrapped_dek is None:
            raise KeyRevokedError(
                f"administration {payload.administration_id}'s key version "
                f"{payload.key_version} has been revoked"
            )

        dek = await self._unwrap(record)
        aesgcm = AESGCM(dek)
        aad = _associated_data(payload.administration_id, payload.key_version)
        return aesgcm.decrypt(payload.nonce, payload.ciphertext, aad)

    async def rewrap_after_kek_rotation(self, *, old_kek_key_id: str) -> int:
        """SEC-023's "re-wrapping" half of KEK rotation: unwraps every
        active DEK still wrapped under old_kek_key_id and re-wraps it under
        the KMS's current KEK version, in place. No DEK value changes and
        no document is touched - only the wrapper. Returns the count of
        keys re-wrapped, so callers (see
        scripts/rewrap_after_kek_rotation.py) can confirm nothing was
        missed before the old KEK version is disabled.
        """
        stale = await self._repository.list_active_wrapped_under(old_kek_key_id)
        count = 0
        for record in stale:
            assert record.wrapped_dek is not None  # active keys always have material
            dek = await self._unwrap(record)
            rewrapped = await self._kms.wrap_key(dek)
            await self._repository.rewrap_in_place(record.id, rewrapped)
            count += 1
        return count

    async def _unwrap(self, record: EncryptionKeyRecord) -> bytes:
        assert record.wrapped_dek is not None
        return await self._kms.unwrap_key(
            WrappedKey(
                ciphertext=record.wrapped_dek,
                algorithm=record.wrap_algorithm,
                kek_key_id=record.kek_key_id,
            )
        )


def _associated_data(administration_id: uuid.UUID, key_version: int) -> bytes:
    """Binds ciphertext to the tenant and key version it was encrypted
    under. AES-GCM's authentication tag covers this AAD as well as the
    ciphertext, so decrypting a document under the wrong administration_id
    or key_version - e.g. because a storage path or database row got mixed
    up - fails authentication rather than silently succeeding. This is the
    concrete mechanism behind IAM-004's "a key revocation isolates one
    tenant": even if ciphertext bytes were somehow copied between tenants,
    they cannot be decrypted as anyone else's.
    """
    return f"administration:{administration_id}:v{key_version}".encode()

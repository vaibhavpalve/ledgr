"""GcpKmsKeyManagementService, exercised against a fake client - production
code with zero test coverage before this file, which is exactly how the bug
below reached a real deployment before a human hit it.

The bug: `current_kek_id` used to read `get_crypto_key(...).primary`, which
does not exist for an asymmetric key - Cloud KMS only populates `primary`
for symmetric keys. Confirmed against a real key in the wild: ENABLED,
2048-bit RSA, correctly configured, `primary` still unset. The fix lists the
key's versions and picks the newest ENABLED one instead - these tests pin
that behaviour so it cannot regress silently again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.cloud import kms_v1

from api.crypto.kms import GcpKmsKeyManagementService

_KEY_RESOURCE_NAME = "projects/p/locations/europe-west4/keyRings/ledgr/cryptoKeys/tenant-dek-kek"


class _AsyncIterableList:
    """Wraps a plain list so `async for` works on it, the same shape
    `list_crypto_key_versions`'s real pager has after `await`.
    """

    def __init__(self, items: list[kms_v1.CryptoKeyVersion]) -> None:
        self._items = items

    def __aiter__(self):
        async def _generator():
            for item in self._items:
                yield item

        return _generator()


class _FakeKmsClient:
    """Satisfies the three methods GcpKmsKeyManagementService calls, with no
    network access to real Cloud KMS.
    """

    def __init__(
        self,
        *,
        versions: list[kms_v1.CryptoKeyVersion],
        public_key_pem: bytes | None = None,
        plaintext: bytes | None = None,
    ) -> None:
        self._versions = versions
        self._public_key_pem = public_key_pem
        self._plaintext = plaintext
        self.asymmetric_decrypt_calls: list[str] = []

    async def list_crypto_key_versions(self, *, parent: str) -> _AsyncIterableList:
        assert parent == _KEY_RESOURCE_NAME
        return _AsyncIterableList(self._versions)

    async def get_public_key(self, *, name: str) -> kms_v1.PublicKey:
        assert self._public_key_pem is not None
        return kms_v1.PublicKey(pem=self._public_key_pem.decode("ascii"))

    async def asymmetric_decrypt(
        self, *, name: str, ciphertext: bytes
    ) -> kms_v1.AsymmetricDecryptResponse:
        self.asymmetric_decrypt_calls.append(name)
        assert self._plaintext is not None
        return kms_v1.AsymmetricDecryptResponse(plaintext=self._plaintext)


def _version(number: int, *, state: int, created_minutes_ago: int) -> kms_v1.CryptoKeyVersion:
    return kms_v1.CryptoKeyVersion(
        name=f"{_KEY_RESOURCE_NAME}/cryptoKeyVersions/{number}",
        state=state,
        create_time=datetime.now(UTC) - timedelta(minutes=created_minutes_ago),
    )


_ENABLED = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.ENABLED
_DISABLED = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.DISABLED
_DESTROYED = kms_v1.CryptoKeyVersion.CryptoKeyVersionState.DESTROYED


async def test_current_kek_id_picks_the_newest_enabled_version() -> None:
    """The exact bug this file exists for: a lone ENABLED version with no
    'primary' field set is still a valid, usable current version.
    """
    client = _FakeKmsClient(versions=[_version(1, state=_ENABLED, created_minutes_ago=5)])
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    assert await kms.current_kek_id() == f"{_KEY_RESOURCE_NAME}/cryptoKeyVersions/1"


async def test_current_kek_id_prefers_the_newer_of_two_enabled_versions() -> None:
    """SEC-023 rotation: the old version stays ENABLED (so it can still
    decrypt DEKs wrapped under it) while a newer one exists - new wraps must
    use the newer one, not whichever happens to sort first.
    """
    older = _version(1, state=_ENABLED, created_minutes_ago=999)
    newer = _version(2, state=_ENABLED, created_minutes_ago=1)
    client = _FakeKmsClient(versions=[older, newer])
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    assert await kms.current_kek_id() == f"{_KEY_RESOURCE_NAME}/cryptoKeyVersions/2"


async def test_current_kek_id_ignores_disabled_and_destroyed_versions() -> None:
    disabled = _version(2, state=_DISABLED, created_minutes_ago=1)
    destroyed = _version(3, state=_DESTROYED, created_minutes_ago=0)
    enabled = _version(1, state=_ENABLED, created_minutes_ago=100)
    client = _FakeKmsClient(versions=[enabled, disabled, destroyed])
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    assert await kms.current_kek_id() == f"{_KEY_RESOURCE_NAME}/cryptoKeyVersions/1"


async def test_current_kek_id_raises_when_nothing_is_enabled() -> None:
    client = _FakeKmsClient(versions=[_version(1, state=_DISABLED, created_minutes_ago=1)])
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="no ENABLED key version"):
        await kms.current_kek_id()


async def test_wrap_and_unwrap_round_trip_through_the_real_rsa_math() -> None:
    """wrap_key/unwrap_key do real OAEP encryption/decryption with the
    `cryptography` library - only the KMS RPCs (list versions, fetch the
    public key, and the final private-key decrypt) are faked.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    dek = b"0" * 32

    client = _FakeKmsClient(
        versions=[_version(1, state=_ENABLED, created_minutes_ago=1)],
        public_key_pem=public_pem,
    )
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    wrapped = await kms.wrap_key(dek)
    assert wrapped.kek_key_id == f"{_KEY_RESOURCE_NAME}/cryptoKeyVersions/1"
    assert wrapped.algorithm == "RSA-OAEP-256"
    assert dek not in wrapped.ciphertext

    # unwrap_key only calls out to KMS (asymmetric_decrypt) - it never has
    # the private key locally - so the fake supplies the plaintext directly,
    # the same way the real Cloud KMS RPC would.
    client._plaintext = dek
    unwrapped = await kms.unwrap_key(wrapped)
    assert unwrapped == dek
    assert client.asymmetric_decrypt_calls == [wrapped.kek_key_id]


async def test_public_keys_are_cached_per_version() -> None:
    """The class docstring's claim: a second wrap under the same version
    does not re-fetch the public key.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    client = _FakeKmsClient(
        versions=[_version(1, state=_ENABLED, created_minutes_ago=1)],
        public_key_pem=public_pem,
    )
    calls: list[str] = []
    original = client.get_public_key

    async def counting_get_public_key(*, name: str) -> kms_v1.PublicKey:
        calls.append(name)
        return await original(name=name)

    client.get_public_key = counting_get_public_key  # type: ignore[method-assign]
    kms = GcpKmsKeyManagementService(key_resource_name=_KEY_RESOURCE_NAME, client=client)  # type: ignore[arg-type]

    await kms.wrap_key(b"0" * 32)
    await kms.wrap_key(b"1" * 32)

    assert len(calls) == 1

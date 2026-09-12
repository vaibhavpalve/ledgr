"""Key-management adapters: the KEK boundary (SEC-022).

A KeyManagementService wraps and unwraps per-tenant data encryption keys
(DEKs) against a key-encryption key (KEK) that never leaves the KMS/HSM.
Two implementations exist so the rest of the system depends only on this
Protocol - CLAUDE.md's fourth architectural non-negotiable: integrations
sit behind adapters, so a provider can be replaced without touching domain
logic.

- AzureKeyVaultKeyManagementService: production. The KEK lives in Azure Key
  Vault as an HSM-backed RSA key; wrap/unwrap are Key Vault API calls. The
  raw KEK material never leaves the vault boundary - it is never fetched,
  held, or logged here, only referenced by its versioned key id.
- LocalDevKeyManagementService: local development and tests only. Holds KEK
  material directly (derived from settings.local_dev_kek) and performs
  RFC 3394 AES key wrap. There is no official local emulator for Key Vault
  the way Azurite emulates Blob Storage, so this hand-rolled stand-in fills
  that role. Selecting kms_provider="local" outside local dev or CI is a
  deployment misconfiguration, not a supported mode - see
  docs/decisions/ADR-004-envelope-encryption.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.keys.aio import KeyClient
from azure.keyvault.keys.crypto import KeyWrapAlgorithm
from azure.keyvault.keys.crypto.aio import CryptographyClient
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap

from api.config import settings


@dataclass(frozen=True, slots=True)
class WrappedKey:
    ciphertext: bytes
    algorithm: str  # 'A256KW' or 'RSA-OAEP-256'
    kek_key_id: str  # KMS key identifier, including version


class KeyManagementService(Protocol):
    async def current_kek_id(self) -> str:
        """The identifier (including version) of the KEK new wraps use."""
        ...

    async def wrap_key(self, key_material: bytes) -> WrappedKey: ...

    async def unwrap_key(self, wrapped: WrappedKey) -> bytes: ...


class LocalDevKeyManagementService:
    """Dev/test-only stand-in. Not a KMS: the "KEK" is process memory
    derived from a config value, with no HSM boundary, no access-policy
    enforcement, and no audit log beyond whatever the application logs
    itself. Holds every KEK version it's given, exactly like a real KMS
    retains old versions for unwrap/decrypt - see rotate() and
    disable_kek_version(), which simulate that behavior for tests.
    """

    _ALGORITHM = "A256KW"

    def __init__(self, *, current_kek_id: str, keks: dict[str, bytes]) -> None:
        for kek_id, material in keks.items():
            if len(material) != 32:
                raise ValueError(f"KEK {kek_id!r} must be exactly 32 bytes (AES-256)")
        if current_kek_id not in keks:
            raise ValueError(f"current_kek_id {current_kek_id!r} not present in keks")
        self._current_kek_id = current_kek_id
        self._keks = dict(keks)

    async def current_kek_id(self) -> str:
        return self._current_kek_id

    async def wrap_key(self, key_material: bytes) -> WrappedKey:
        kek_material = self._keks[self._current_kek_id]
        ciphertext = aes_key_wrap(kek_material, key_material)
        return WrappedKey(
            ciphertext=ciphertext, algorithm=self._ALGORITHM, kek_key_id=self._current_kek_id
        )

    async def unwrap_key(self, wrapped: WrappedKey) -> bytes:
        try:
            kek_material = self._keks[wrapped.kek_key_id]
        except KeyError:
            raise ValueError(
                f"KEK version {wrapped.kek_key_id!r} is not available (disabled/purged, "
                "or never configured) - this is the emergency-rotation end state, or a "
                "sign a re-wrap sweep is overdue"
            ) from None
        return aes_key_unwrap(kek_material, wrapped.ciphertext)

    def rotate(self, *, new_kek_id: str, new_kek_material: bytes) -> None:
        """Test/dev convenience simulating a KMS-side KEK rotation: a new
        current version is added; old versions remain available for
        unwrap unless explicitly disabled below - matching how Key Vault
        retains prior key versions.
        """
        if len(new_kek_material) != 32:
            raise ValueError("KEK material must be exactly 32 bytes (AES-256)")
        self._keks[new_kek_id] = new_kek_material
        self._current_kek_id = new_kek_id

    def disable_kek_version(self, kek_id: str) -> None:
        """Test/dev convenience simulating permanently disabling/purging an
        old KEK version - the emergency-rotation end state where the
        suspected-compromised version can no longer unwrap anything.
        """
        if kek_id == self._current_kek_id:
            raise ValueError("cannot disable the current KEK version")
        self._keks.pop(kek_id, None)


class AzureKeyVaultKeyManagementService:
    """Production KMS adapter. The KEK is an HSM-backed RSA key in Azure Key
    Vault; wrap/unwrap are Key Vault REST calls via the SDK, authenticated
    with the process's managed identity (DefaultAzureCredential - SEC-033,
    no long-lived static credentials). This class only ever sends or
    receives DEK bytes and ciphertext; the KEK's private key material never
    leaves Key Vault's HSM boundary.
    """

    _ALGORITHM = "RSA-OAEP-256"

    def __init__(self, *, vault_url: str, key_name: str) -> None:
        self._key_name = key_name
        self._credential = DefaultAzureCredential()
        self._key_client = KeyClient(vault_url=vault_url, credential=self._credential)
        self._crypto_clients: dict[str, CryptographyClient] = {}

    async def current_kek_id(self) -> str:
        key = await self._key_client.get_key(self._key_name)
        assert key.id is not None
        return key.id

    async def _crypto_client_for(self, kek_key_id: str) -> CryptographyClient:
        if kek_key_id not in self._crypto_clients:
            self._crypto_clients[kek_key_id] = CryptographyClient(
                kek_key_id, credential=self._credential
            )
        return self._crypto_clients[kek_key_id]

    async def wrap_key(self, key_material: bytes) -> WrappedKey:
        kek_key_id = await self.current_kek_id()
        client = await self._crypto_client_for(kek_key_id)
        result = await client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, key_material)
        return WrappedKey(
            ciphertext=result.encrypted_key, algorithm=self._ALGORITHM, kek_key_id=kek_key_id
        )

    async def unwrap_key(self, wrapped: WrappedKey) -> bytes:
        client = await self._crypto_client_for(wrapped.kek_key_id)
        result = await client.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, wrapped.ciphertext)
        return bytes(result.key)


def build_kms() -> KeyManagementService:
    """Selects the KMS adapter from settings.kms_provider. This is the one
    place that decision gets made - everything downstream depends only on
    the KeyManagementService Protocol.
    """
    if settings.kms_provider == "local":
        material = hashlib.sha256(settings.local_dev_kek.encode("utf-8")).digest()
        return LocalDevKeyManagementService(
            current_kek_id="local-dev-kek-v1", keks={"local-dev-kek-v1": material}
        )
    if settings.kms_provider == "azure-key-vault":
        if not settings.azure_key_vault_url:
            raise RuntimeError(
                "kms_provider is 'azure-key-vault' but AZURE_KEY_VAULT_URL is not set"
            )
        return AzureKeyVaultKeyManagementService(
            vault_url=settings.azure_key_vault_url, key_name=settings.azure_kek_name
        )
    raise ValueError(f"unknown kms_provider: {settings.kms_provider!r}")

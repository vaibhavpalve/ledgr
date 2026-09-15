"""Key-management adapters: the KEK boundary (SEC-022).

A KeyManagementService wraps and unwraps per-tenant data encryption keys
(DEKs) against a key-encryption key (KEK) that never leaves the KMS/HSM.
Three implementations exist so the rest of the system depends only on this
Protocol - CLAUDE.md's fourth architectural non-negotiable: integrations
sit behind adapters, so a provider can be replaced without touching domain
logic.

- GcpKmsKeyManagementService: production (ADR-062). The KEK lives in Google
  Cloud KMS as an HSM-backed RSA key; unwrap is a KMS asymmetric_decrypt RPC,
  wrap encrypts locally against the key's PUBLIC portion (not secret - see
  the class docstring for why that is still "never leaves the HSM boundary").
- AzureKeyVaultKeyManagementService: the PRE-ADR-062 production adapter,
  kept in-tree until GcpKmsKeyManagementService is actually selected in
  every non-local environment (ADR-062's own Consequences say so
  explicitly) rather than deleted the moment its replacement is written.
  The KEK lives in Azure Key Vault as an HSM-backed RSA key; wrap/unwrap are
  Key Vault API calls. The raw KEK material never leaves the vault boundary
  - it is never fetched, held, or logged here, only referenced by its
  versioned key id.
- LocalDevKeyManagementService: local development and tests only. Holds KEK
  material directly (derived from settings.local_dev_kek) and performs
  RFC 3394 AES key wrap. There is no official local emulator for either
  cloud KMS the way Azurite emulates Blob Storage, so this hand-rolled
  stand-in fills that role. Selecting kms_provider="local" outside local
  dev or CI is a deployment misconfiguration, not a supported mode - see
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
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
from google.cloud import kms_v1

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


class GcpKmsKeyManagementService:
    """Production KMS adapter (ADR-062): the KEK is an HSM-backed asymmetric
    RSA key in Google Cloud KMS, purpose RSA_DECRYPT_OAEP with a 2048+ bit
    key and SHA-256. Deliberately uses the SAME 'RSA-OAEP-256' algorithm tag
    AzureKeyVaultKeyManagementService does, so this adapter needed no schema
    change - administration_encryption_key.wrap_algorithm's CHECK constraint
    (migrations/0002_encryption_keys.sql:60) already permits it.

    Cloud KMS has no server-side "wrap" RPC for an asymmetric key the way Key
    Vault does; only asymmetric_decrypt is a KMS operation. wrap_key instead
    fetches the key's PUBLIC portion (via get_public_key) and encrypts with
    it locally, using this process's own `cryptography` library. That is
    still "the private key never leaves the HSM boundary": a public key is,
    by definition, not secret - revealing it lets anyone encrypt FOR the key,
    never decrypt anything wrapped under it. Only unwrap_key - the operation
    that needs the private half - ever calls out to KMS.

    Public keys are cached per key-VERSION id (the same string current_kek_id
    returns), not per crypto key, so a KEK rotation that changes the current
    version transparently fetches and caches the new version's public key on
    its first use, while an already-wrapped DEK under an older version still
    resolves (for decrypt) against that older version's identity - the cache
    key never needs invalidating on rotation, only growing.
    """

    _ALGORITHM = "RSA-OAEP-256"

    def __init__(self, *, key_resource_name: str) -> None:
        # The CRYPTO KEY's resource name, with no version segment - e.g.
        # "projects/<p>/locations/europe-west4/keyRings/ledgr/cryptoKeys/
        # tenant-dek-kek". current_kek_id() resolves this to its current
        # PRIMARY VERSION's own (longer) resource name, which is what
        # actually gets stored as kek_key_id and passed to KMS RPCs.
        self._key_resource_name = key_resource_name
        self._client = kms_v1.KeyManagementServiceAsyncClient()
        self._public_keys: dict[str, rsa.RSAPublicKey] = {}

    async def current_kek_id(self) -> str:
        key = await self._client.get_crypto_key(name=self._key_resource_name)
        assert key.primary is not None and key.primary.name
        return key.primary.name

    async def _public_key_for(self, kek_key_id: str) -> rsa.RSAPublicKey:
        if kek_key_id not in self._public_keys:
            response = await self._client.get_public_key(name=kek_key_id)
            public_key = serialization.load_pem_public_key(response.pem.encode("ascii"))
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise ValueError(f"KEK {kek_key_id!r} is not an RSA key")
            self._public_keys[kek_key_id] = public_key
        return self._public_keys[kek_key_id]

    async def wrap_key(self, key_material: bytes) -> WrappedKey:
        kek_key_id = await self.current_kek_id()
        public_key = await self._public_key_for(kek_key_id)
        ciphertext = public_key.encrypt(
            key_material,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return WrappedKey(ciphertext=ciphertext, algorithm=self._ALGORITHM, kek_key_id=kek_key_id)

    async def unwrap_key(self, wrapped: WrappedKey) -> bytes:
        response = await self._client.asymmetric_decrypt(
            name=wrapped.kek_key_id, ciphertext=wrapped.ciphertext
        )
        return bytes(response.plaintext)


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
    if settings.kms_provider == "gcp-kms":
        if not settings.gcp_kms_key_resource_name:
            raise RuntimeError("kms_provider is 'gcp-kms' but GCP_KMS_KEY_RESOURCE_NAME is not set")
        return GcpKmsKeyManagementService(key_resource_name=settings.gcp_kms_key_resource_name)
    raise ValueError(f"unknown kms_provider: {settings.kms_provider!r}")

"""Where the bytes actually live - SEC-005, FR-DOC-001, IAM-004.

    SEC-005  ... stored outside the web root, served from a separate origin
             with `Content-Disposition: attachment`.

--- "Outside the web root" is a property of the design, not a path ---

There is no web root to be outside of: originals go to Cloudflare R2 (ADR-062,
superseding PRD §13's Azure Blob choice), which is not a filesystem the API
serves from and has no path an HTTP request can reach. That is a stronger form
of the control than a directory outside `static/`, and the way to keep it
strong is to make it structural - so this module's Protocol has no operation
that returns a filesystem path or a URL the application would serve directly.
A caller can put bytes in and get bytes out, and that is all.

The other half of SEC-005 - what a browser is told to do with the bytes -
belongs to whatever turns stored bytes into a response, which is
`api.security.stored_files.stored_file_response` for every such route. ADR-063
records why the attachment disposition and a CSP sandbox carry that on their
own, with no separate origin behind them.

--- Encrypted under the administration's own key ---

CLAUDE.md's non-negotiable #4 puts integrations behind adapters so a provider
can be replaced without touching domain logic, and `BlobStore` is that seam
for R2. But the encryption is NOT the provider's: `EncryptedBlobStore` wraps
any store and encrypts with `EnvelopeEncryptionService`, so the bytes are
already ciphertext before the provider sees them.

That ordering is deliberate. Provider-side encryption would make IAM-004's
per-tenant key isolation a property of the provider's own configuration - true
until somebody changes a setting, and not true at all for a different
provider. Encrypting here makes it a property of this code path, and a stored
blob is useless to anyone who obtains it without also holding the
administration's DEK. It's also what made ADR-062's provider swap (Azure Blob
-> R2) a one-class change instead of a re-encryption project: `R2BlobStore`
only ever sees ciphertext, so it did not need to inherit or replicate
anything about how the previous, never-built Azure adapter would have handled
encryption.

--- Keys are opaque and tenant-scoped ---

`storage_key` embeds the administration id, which is not a security control -
RLS and the DEK are - but it makes a stray blob attributable, and it means a
per-tenant deletion sweep at the end of a retention period is a prefix scan
rather than a join against a database that may no longer have the row.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Protocol

import aioboto3

from api.config import settings
from api.crypto.envelope import EncryptedPayload, EnvelopeEncryptionService


class StorageError(RuntimeError):
    """The blob could not be written, read or removed."""


class BlobNotFoundError(StorageError):
    """No blob under that key.

    Distinct from a read failure: a missing blob for a document row that exists
    is an FR-DOC-001 violation - the original is gone - and is reported by the
    integrity job rather than retried.
    """


def new_storage_key(administration_id: uuid.UUID) -> str:
    """An opaque, unguessable key under a per-administration prefix.

    Random rather than derived from the document id, and the reason is the
    order of operations: the bytes are stored BEFORE the row exists, so that a
    failure leaves an orphan blob (which the sweep collects) rather than a row
    pointing at nothing (which is a lost original). There is no document id to
    derive from at that moment.
    """
    return f"{administration_id}/{secrets.token_urlsafe(24)}"


class BlobStore(Protocol):
    """The provider seam. Cloudflare R2 in production (ADR-062), in-memory
    in tests and local dev.

    Deliberately has no `list`, no `url_for` and no `path_for`. A store that
    could hand out a URL would invite something to serve it directly, and
    SEC-005's response headers would then depend on every caller remembering
    to route through the download endpoint. That matters more since ADR-063,
    not less: those headers are now the only control on what a browser does
    with these bytes.
    """

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None:
        """Only ever called after FR-DOC-005's guard has been satisfied - by
        the retention sweep, or by an approved deletion. The database is what
        enforces that; this just removes bytes.
        """
        ...


class InMemoryBlobStore:
    """For tests and for local development, before real R2 credentials
    (or an S3-compatible local stand-in) are configured.

    Not a fake in the sense of being simplified: it has the same failure modes
    the Protocol declares, so a test can exercise the missing-blob path without
    a network.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self._blobs[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self._blobs[key]
        except KeyError as exc:
            raise BlobNotFoundError(f"no blob at {key!r}") from exc

    async def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    def __len__(self) -> int:
        return len(self._blobs)


class R2BlobStore:
    """Production adapter (ADR-062): Cloudflare R2, spoken through its
    S3-compatible API via `aioboto3` - R2 has no native Python SDK, only
    S3-API compatibility, so this is written as an S3 client pointed at R2's
    endpoint rather than a Cloudflare-specific library.

    A fresh client is opened per call rather than held across the store's
    lifetime: `aioboto3`'s client is an async context manager tied to one
    underlying `aiohttp` session, and this store (like `EncryptedBlobStore`
    it's wrapped by) is built once per process and reused across many
    requests - holding one open session for the process's whole lifetime
    risks it going stale under a store that outlives it. The cost is a
    connection-setup round trip per call; revisit with a pooled/shared
    client if that becomes a measurable bottleneck.
    """

    def __init__(
        self, *, account_id: str, bucket: str, access_key_id: str, secret_access_key: str
    ) -> None:
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        # R2's S3-compatible endpoint is account-scoped, not region-scoped -
        # "auto" is what R2 documents as the region_name for its API calls,
        # since R2 itself has no AWS-style regions to select between.
        self._endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        self._session = aioboto3.Session()

    def _client(self) -> Any:
        # aioboto3/aiobotocore build the S3 client dynamically from botocore's
        # service model at runtime, so its concrete type isn't a class this
        # code can name - Any is the honest annotation here, not a stand-in
        # for one this project forgot to write (unlike types-aiobotocore's
        # generated stubs, which this project's dependencies deliberately
        # skip pulling in just for one adapter).
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            region_name="auto",
        )

    async def put(self, key: str, data: bytes) -> None:
        async with self._client() as client:
            await client.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise BlobNotFoundError(f"no blob at {key!r}") from exc
            async with response["Body"] as body:
                return bytes(await body.read())

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)


def build_blob_store() -> BlobStore:
    """Selects the blob-store adapter from settings.blob_provider - the same
    "one place this decision gets made" pattern api.crypto.kms.build_kms
    uses for the KMS provider.
    """
    if settings.blob_provider == "in-memory":
        return InMemoryBlobStore()
    if settings.blob_provider == "r2":
        missing = [
            name
            for name, value in (
                ("R2_ACCOUNT_ID", settings.r2_account_id),
                ("R2_BUCKET", settings.r2_bucket),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"blob_provider is 'r2' but {', '.join(missing)} is not set")
        assert settings.r2_account_id and settings.r2_bucket
        assert settings.r2_access_key_id and settings.r2_secret_access_key
        return R2BlobStore(
            account_id=settings.r2_account_id,
            bucket=settings.r2_bucket,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
        )
    raise ValueError(f"unknown blob_provider: {settings.blob_provider!r}")


class EncryptedBlobStore:
    """A `BlobStore` that encrypts under the administration's own DEK.

    Composed rather than inherited, so the encryption applies to whatever store
    is underneath - Azure in production, in-memory in a test - and a test of the
    encryption is not also a test of Azure.

    The administration id is a constructor argument rather than a per-call one
    because a store instance is built per request, already scoped to the
    administration the request is about. A `put` that took the tenant as an
    argument would be one typo away from encrypting under the wrong key, and
    the resulting blob would be unreadable rather than obviously wrong.
    """

    def __init__(
        self,
        inner: BlobStore,
        encryption: EnvelopeEncryptionService,
        administration_id: uuid.UUID,
    ) -> None:
        self._inner = inner
        self._encryption = encryption
        self._administration_id = administration_id

    async def put(self, key: str, data: bytes) -> None:
        payload = await self._encryption.encrypt(self._administration_id, data)
        await self._inner.put(key, _pack(payload))

    async def get(self, key: str) -> bytes:
        blob = await self._inner.get(key)
        return await self._encryption.decrypt(_unpack(blob, self._administration_id))

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)


#: The wire form of an EncryptedPayload inside a blob. A blob has to carry the
#: key version it was encrypted under, or a rotated key (SEC-023) leaves every
#: earlier document undecryptable - rotation exists precisely so that old
#: ciphertext stays readable under the old version.
_HEADER = b"LEDGRDOC\x01"


def _pack(payload: EncryptedPayload) -> bytes:
    version = payload.key_version.to_bytes(4, "big")
    nonce_length = len(payload.nonce).to_bytes(2, "big")
    return _HEADER + version + nonce_length + payload.nonce + payload.ciphertext


def _unpack(blob: bytes, administration_id: uuid.UUID) -> EncryptedPayload:
    """The administration id comes from the CALLER, not from the blob.

    That is what makes it a tenancy control rather than a label. The envelope
    service binds `(administration_id, key_version)` into AES-GCM's associated
    data, so a blob read while scoped to the wrong administration fails
    authentication outright - it does not decrypt to something wrong, it does
    not decrypt at all. Had the id been packed into the blob and read back out,
    that check would be comparing the blob against itself.
    """
    if not blob.startswith(_HEADER):
        raise StorageError(
            "stored blob does not carry the LEDGR document header; it was "
            "written by something else, or it has been altered (FR-DOC-001)"
        )
    cursor = len(_HEADER)
    key_version = int.from_bytes(blob[cursor : cursor + 4], "big")
    cursor += 4
    nonce_length = int.from_bytes(blob[cursor : cursor + 2], "big")
    cursor += 2
    nonce = blob[cursor : cursor + nonce_length]
    cursor += nonce_length
    return EncryptedPayload(
        administration_id=administration_id,
        key_version=key_version,
        nonce=nonce,
        ciphertext=blob[cursor:],
    )

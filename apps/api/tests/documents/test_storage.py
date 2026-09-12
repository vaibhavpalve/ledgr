"""Where the bytes live - SEC-005, IAM-004, FR-DOC-001.

The properties worth pinning here are the ones that would fail silently: an
original that comes back altered, and a blob that one tenant can read from
another's key.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import LocalDevKeyManagementService
from api.documents.storage import (
    BlobNotFoundError,
    BlobStore,
    EncryptedBlobStore,
    InMemoryBlobStore,
    StorageError,
    new_storage_key,
)
from tests.support.fake_key_repository import InMemoryAdministrationKeyRepository

PDF = b"%PDF-1.7\nthe original, unaltered\n" + bytes(range(256)) * 8


async def _encrypted(
    administration_id: uuid.UUID, inner: InMemoryBlobStore | None = None
) -> tuple[EncryptedBlobStore, InMemoryBlobStore, EnvelopeEncryptionService]:
    kms = LocalDevKeyManagementService(
        current_kek_id="local-dev-kek-v1", keks={"local-dev-kek-v1": b"0" * 32}
    )
    encryption = EnvelopeEncryptionService(kms, InMemoryAdministrationKeyRepository())
    await encryption.provision_key(administration_id)
    store = inner if inner is not None else InMemoryBlobStore()
    return EncryptedBlobStore(store, encryption, administration_id), store, encryption


async def test_an_original_comes_back_byte_identical() -> None:
    """FR-DOC-001's whole claim, at the layer that could break it."""
    administration = uuid.uuid4()
    store, _, _ = await _encrypted(administration)
    key = new_storage_key(administration)

    await store.put(key, PDF)

    assert await store.get(key) == PDF


async def test_the_stored_blob_is_not_the_plaintext() -> None:
    """IAM-004/SEC-022: a blob obtained without the administration's key is
    useless. Asserted because "we encrypt" is the kind of claim that survives
    the removal of the code that does it.
    """
    administration = uuid.uuid4()
    store, inner, _ = await _encrypted(administration)
    key = new_storage_key(administration)

    await store.put(key, PDF)

    stored = await inner.get(key)
    assert PDF not in stored
    assert b"the original, unaltered" not in stored


async def test_one_tenants_blob_cannot_be_read_under_anothers_key() -> None:
    """The tenancy control, and the reason the administration id is a
    constructor argument rather than packed into the blob.

    AES-GCM binds (administration_id, key_version) into its associated data, so
    a read scoped to the wrong administration does not decrypt to something
    wrong - it fails outright. Had the id been read back out of the blob, the
    check would be comparing the blob against itself.
    """
    shared = InMemoryBlobStore()
    alice, bob = uuid.uuid4(), uuid.uuid4()

    alice_store, _, _ = await _encrypted(alice, shared)
    key = new_storage_key(alice)
    await alice_store.put(key, PDF)

    # Bob's store, over the same underlying blobs, with Bob's own key.
    bob_store, _, _ = await _encrypted(bob, shared)

    with pytest.raises(Exception) as raised:
        await bob_store.get(key)
    assert not isinstance(raised.value, AssertionError)


async def test_a_blob_written_by_something_else_is_refused() -> None:
    """FR-DOC-001: an original that does not carry the header was not written
    by this code path, so it is not the original this archive stored.
    """
    administration = uuid.uuid4()
    store, inner, _ = await _encrypted(administration)
    await inner.put("stray", b"not a LEDGR document")

    with pytest.raises(StorageError, match="header"):
        await store.get("stray")


async def test_a_missing_blob_is_distinguishable_from_a_read_failure() -> None:
    """A missing blob for a row that exists is an FR-DOC-001 violation - the
    original is gone - and is reported rather than retried.
    """
    store = InMemoryBlobStore()
    with pytest.raises(BlobNotFoundError):
        await store.get("nothing here")


async def test_keys_are_unguessable_and_tenant_prefixed() -> None:
    administration = uuid.uuid4()
    keys = {new_storage_key(administration) for _ in range(200)}

    assert len(keys) == 200, "a collision would silently overwrite an original"
    assert all(key.startswith(f"{administration}/") for key in keys)
    # Long enough that guessing one is not a strategy, even though RLS and the
    # DEK are what actually protect it.
    assert all(len(key.split("/")[1]) >= 24 for key in keys)


def test_the_store_offers_no_way_to_hand_out_a_url() -> None:
    """SEC-005's "served from a separate origin with Content-Disposition:
    attachment" is enforceable only if every read goes through the download
    endpoint. A store that could produce a URL or a path would invite
    something to serve it directly, bypassing the scan gate, the audit entry
    and every response header.
    """
    operations = {
        name
        for name, _ in inspect.getmembers(BlobStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert operations == {"put", "get", "delete"}

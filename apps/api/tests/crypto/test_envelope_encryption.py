"""Pure-logic tests for envelope encryption: no database, no network - just
LocalDevKeyManagementService plus an in-memory repository. RLS/schema
wiring for the real table is proven separately by
tests/integration/test_encryption_key_isolation.py.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag

from api.crypto.envelope import EnvelopeEncryptionService, KeyRevokedError
from api.crypto.kms import LocalDevKeyManagementService
from tests.support.fake_key_repository import InMemoryAdministrationKeyRepository


def _service_with_kms() -> tuple[EnvelopeEncryptionService, LocalDevKeyManagementService]:
    kms = LocalDevKeyManagementService(
        current_kek_id="local-dev-kek-v1", keks={"local-dev-kek-v1": b"0" * 32}
    )
    repository = InMemoryAdministrationKeyRepository()
    return EnvelopeEncryptionService(kms, repository), kms


def _service() -> EnvelopeEncryptionService:
    return _service_with_kms()[0]


async def test_provisioning_and_round_trip_encrypt_decrypt() -> None:
    service = _service()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)

    payload = await service.encrypt(administration_id, b"hello ledger")
    plaintext = await service.decrypt(payload)

    assert plaintext == b"hello ledger"


async def test_encrypting_without_a_provisioned_key_fails() -> None:
    service = _service()
    with pytest.raises(ValueError, match="no active encryption key"):
        await service.encrypt(uuid.uuid4(), b"anything")


async def test_ciphertext_cannot_be_decrypted_under_a_different_tenants_aad() -> None:
    """The AAD tenant-binding property: even with a validly provisioned key
    for the target administration, ciphertext produced for a DIFFERENT
    administration must fail authentication rather than silently decrypt -
    this is what makes ciphertext un-movable between tenants.
    """
    service = _service()
    admin_a = uuid.uuid4()
    admin_b = uuid.uuid4()
    await service.provision_key(admin_a)
    await service.provision_key(admin_b)

    payload = await service.encrypt(admin_a, b"tenant A's secret")
    forged = replace(payload, administration_id=admin_b)

    with pytest.raises(InvalidTag):
        await service.decrypt(forged)


async def test_rotation_retires_old_key_but_keeps_it_decryptable() -> None:
    service = _service()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)

    old_payload = await service.encrypt(administration_id, b"encrypted under v1")
    await service.rotate_key(administration_id, reason="scheduled_annual")
    new_payload = await service.encrypt(administration_id, b"encrypted under v2")

    assert old_payload.key_version == 1
    assert new_payload.key_version == 2
    assert await service.decrypt(old_payload) == b"encrypted under v1"
    assert await service.decrypt(new_payload) == b"encrypted under v2"


async def test_revocation_permanently_blocks_further_use() -> None:
    service = _service()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)
    payload = await service.encrypt(administration_id, b"about to be revoked")

    await service.revoke_key(administration_id)

    with pytest.raises(KeyRevokedError):
        await service.decrypt(payload)
    with pytest.raises(KeyRevokedError):
        await service.encrypt(administration_id, b"should never be written")


async def test_revoking_one_tenant_does_not_affect_another() -> None:
    """IAM-004's central claim, exercised directly: revocation isolates."""
    service = _service()
    admin_a = uuid.uuid4()
    admin_b = uuid.uuid4()
    await service.provision_key(admin_a)
    await service.provision_key(admin_b)

    await service.revoke_key(admin_a)

    payload_b = await service.encrypt(admin_b, b"tenant B is unaffected")
    assert await service.decrypt(payload_b) == b"tenant B is unaffected"


async def test_reprovisioning_after_revocation_does_not_collide_with_the_revoked_version() -> None:
    """Regression test: encrypt()/provision_key() must consult the latest
    key row regardless of status, not only 'active' ones - otherwise
    re-provisioning after a revoke tries to reuse key_version=1, which
    collides with the (administration_id, key_version) uniqueness the
    schema enforces on the already-revoked row.
    """
    service = _service()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)
    await service.revoke_key(administration_id)

    new_key = await service.provision_key(administration_id, rotation_reason="scheduled_annual")
    assert new_key.key_version == 2  # continues the sequence, does not restart at 1

    payload = await service.encrypt(administration_id, b"written after re-provisioning")
    assert await service.decrypt(payload) == b"written after re-provisioning"


async def test_rewrap_after_kek_rotation_survives_old_version_being_disabled() -> None:
    service, kms = _service_with_kms()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)
    payload = await service.encrypt(administration_id, b"stable across rewrap")

    kms.rotate(new_kek_id="local-dev-kek-v2", new_kek_material=b"1" * 32)
    count = await service.rewrap_after_kek_rotation(old_kek_key_id="local-dev-kek-v1")
    assert count == 1

    kms.disable_kek_version("local-dev-kek-v1")

    # Now wrapped under v2, which is current - still decrypts fine even
    # though v1 (what it was ORIGINALLY wrapped under) no longer exists.
    assert await service.decrypt(payload) == b"stable across rewrap"


async def test_skipping_rewrap_before_disabling_old_kek_loses_the_key() -> None:
    """What NOT re-wrapping in time looks like: if an old KEK version is
    disabled while a DEK is still wrapped under it, that DEK - and every
    document it protects - becomes unreadable. This is exactly why SEC-023
    requires re-wrapping to happen, not just KEK rotation alone.
    """
    service, kms = _service_with_kms()
    administration_id = uuid.uuid4()
    await service.provision_key(administration_id)
    payload = await service.encrypt(administration_id, b"never re-wrapped")

    kms.rotate(new_kek_id="local-dev-kek-v2", new_kek_material=b"1" * 32)
    kms.disable_kek_version("local-dev-kek-v1")  # no rewrap happened first

    with pytest.raises(ValueError, match="not available"):
        await service.decrypt(payload)

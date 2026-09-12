"""Pure-logic tests for WebAuthn passkey registration and authentication
(IAM-010) - no database, using InMemoryPasskeyRepository and real
cryptographic ceremonies built by tests/auth/webauthn_helpers.py against
the actual `webauthn` library.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from webauthn.helpers import bytes_to_base64url

from api.auth.passkeys import (
    PasskeyAlreadyRegisteredError,
    PasskeyAuthenticationError,
    PasskeyNotFoundError,
    PasskeyRegistrationError,
    PasskeyRevokedError,
    UnknownPasskeyError,
    WebAuthnService,
)
from tests.auth.webauthn_helpers import FakeAuthenticator, another_authenticators_signature_over
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository

_RP_ID = "ledgr.test"
_ORIGIN = "https://ledgr.test"


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _service(clock: _FakeClock | None = None) -> WebAuthnService:
    kwargs = {"clock": clock} if clock is not None else {}
    return WebAuthnService(
        InMemoryPasskeyRepository(),
        rp_id=_RP_ID,
        rp_name="LEDGR Test",
        expected_origin=_ORIGIN,
        **kwargs,
    )


async def _register(
    service: WebAuthnService,
    *,
    user_id: uuid.UUID,
    name: str = "My Device",
    authenticator: FakeAuthenticator | None = None,
    existing_passkeys: list | None = None,
    user_email: str = "user@example.com",
    user_verified: bool = True,
):
    authenticator = authenticator or FakeAuthenticator()
    challenge_response = service.begin_registration(
        user_id=user_id, user_email=user_email, existing_passkeys=existing_passkeys or []
    )
    credential = authenticator.build_registration_credential(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=challenge_response.challenge,
        user_verified=user_verified,
    )
    passkey = await service.complete_registration(
        user_id=user_id, name=name, challenge=challenge_response.challenge, credential=credential
    )
    return passkey, authenticator


# --- Registration and authentication round trip ---------------------------


async def test_register_then_authenticate_round_trip() -> None:
    service = _service()
    user_id = uuid.uuid4()

    passkey, authenticator = await _register(service, user_id=user_id, name="MacBook Touch ID")
    assert passkey.name == "MacBook Touch ID"
    assert passkey.user_id == user_id
    assert not passkey.is_revoked

    auth_challenge = service.begin_authentication()
    credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=auth_challenge.challenge
    )
    authenticated = await service.complete_authentication(
        challenge=auth_challenge.challenge, credential=credential
    )

    assert authenticated.id == passkey.id


async def test_a_blank_name_is_rejected() -> None:
    service = _service()
    with pytest.raises(PasskeyRegistrationError, match="blank"):
        await _register(service, user_id=uuid.uuid4(), name="   ")


# --- Multiple passkeys per user --------------------------------------------


async def test_a_user_can_register_multiple_named_passkeys() -> None:
    service = _service()
    user_id = uuid.uuid4()

    laptop, _ = await _register(service, user_id=user_id, name="Laptop")
    phone, _ = await _register(service, user_id=user_id, name="Phone")

    passkeys = await service.list_passkeys(user_id)
    assert {p.id for p in passkeys} == {laptop.id, phone.id}
    assert {p.name for p in passkeys} == {"Laptop", "Phone"}


async def test_each_registered_passkey_authenticates_independently() -> None:
    service = _service()
    user_id = uuid.uuid4()
    _, laptop_auth = await _register(service, user_id=user_id, name="Laptop")
    _, phone_auth = await _register(service, user_id=user_id, name="Phone")

    for authenticator in (laptop_auth, phone_auth):
        challenge = service.begin_authentication()
        credential = authenticator.build_authentication_credential(
            rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
        )
        await service.complete_authentication(challenge=challenge.challenge, credential=credential)


async def test_registration_excludes_the_users_existing_non_revoked_credentials() -> None:
    service = _service()
    user_id = uuid.uuid4()
    existing, _ = await _register(service, user_id=user_id, name="Laptop")

    challenge_response = service.begin_registration(
        user_id=user_id, user_email="user@example.com", existing_passkeys=[existing]
    )

    options = json.loads(challenge_response.options_json)
    excluded_ids = {c["id"] for c in options["excludeCredentials"]}
    assert bytes_to_base64url(existing.credential_id) in excluded_ids


async def test_registering_the_same_credential_twice_fails() -> None:
    service = _service()
    user_id = uuid.uuid4()
    _, authenticator = await _register(service, user_id=user_id, name="Laptop")

    challenge_response = service.begin_registration(
        user_id=user_id, user_email="user@example.com", existing_passkeys=[]
    )
    duplicate_credential = authenticator.build_registration_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge_response.challenge
    )

    with pytest.raises(PasskeyAlreadyRegisteredError):
        await service.complete_registration(
            user_id=user_id,
            name="Laptop Again",
            challenge=challenge_response.challenge,
            credential=duplicate_credential,
        )


# --- Individual revocability ------------------------------------------------


async def test_revoking_one_passkey_blocks_authentication_with_it() -> None:
    service = _service()
    user_id = uuid.uuid4()
    passkey, authenticator = await _register(service, user_id=user_id)

    await service.revoke_passkey(user_id, passkey.id)

    challenge = service.begin_authentication()
    credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
    )
    with pytest.raises(PasskeyRevokedError):
        await service.complete_authentication(challenge=challenge.challenge, credential=credential)


async def test_revoking_one_passkey_does_not_affect_another() -> None:
    service = _service()
    user_id = uuid.uuid4()
    laptop, laptop_auth = await _register(service, user_id=user_id, name="Laptop")
    phone, phone_auth = await _register(service, user_id=user_id, name="Phone")

    await service.revoke_passkey(user_id, laptop.id)

    challenge = service.begin_authentication()
    credential = phone_auth.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
    )
    authenticated = await service.complete_authentication(
        challenge=challenge.challenge, credential=credential
    )
    assert authenticated.id == phone.id

    remaining = await service.list_passkeys(user_id)
    assert {p.id for p in remaining} == {laptop.id, phone.id}  # revoked, but still listed
    assert next(p for p in remaining if p.id == laptop.id).is_revoked
    assert not next(p for p in remaining if p.id == phone.id).is_revoked


async def test_revoking_a_passkey_that_belongs_to_a_different_user_is_rejected() -> None:
    service = _service()
    owner_id = uuid.uuid4()
    attacker_id = uuid.uuid4()
    passkey, _ = await _register(service, user_id=owner_id)

    with pytest.raises(PasskeyNotFoundError):
        await service.revoke_passkey(attacker_id, passkey.id)

    # And critically: the real owner's passkey is untouched by the attempt.
    remaining = await service.list_passkeys(owner_id)
    assert not remaining[0].is_revoked


async def test_revoking_a_nonexistent_passkey_raises_the_same_error_as_wrong_owner() -> None:
    """Deliberately indistinguishable, same reasoning as SEC-008: a caller
    should not be able to tell "no such passkey" apart from "that passkey
    belongs to someone else" from the error alone.
    """
    service = _service()
    with pytest.raises(PasskeyNotFoundError):
        await service.revoke_passkey(uuid.uuid4(), uuid.uuid4())


# --- Authentication failure modes ------------------------------------------


async def test_an_unregistered_credential_id_is_rejected() -> None:
    service = _service()
    stray_authenticator = FakeAuthenticator()
    challenge = service.begin_authentication()
    credential = stray_authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
    )

    with pytest.raises(UnknownPasskeyError):
        await service.complete_authentication(challenge=challenge.challenge, credential=credential)


async def test_a_forged_signature_from_a_different_key_is_rejected() -> None:
    service = _service()
    user_id = uuid.uuid4()
    _, authenticator = await _register(service, user_id=user_id)

    challenge = service.begin_authentication()
    credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
    )
    forged = credential.__class__(
        id=credential.id,
        raw_id=credential.raw_id,
        response=credential.response.__class__(
            client_data_json=credential.response.client_data_json,
            authenticator_data=credential.response.authenticator_data,
            signature=another_authenticators_signature_over(credential),
        ),
    )

    with pytest.raises(PasskeyAuthenticationError):
        await service.complete_authentication(challenge=challenge.challenge, credential=forged)


async def test_registration_without_user_verification_is_rejected() -> None:
    """IAM-010: a passkey stands in for a password, so user verification
    (not just presence) is required, not merely preferred.
    """
    service = _service()
    with pytest.raises(PasskeyRegistrationError):
        await _register(service, user_id=uuid.uuid4(), user_verified=False)


async def test_authentication_without_user_verification_is_rejected() -> None:
    service = _service()
    user_id = uuid.uuid4()
    _, authenticator = await _register(service, user_id=user_id)

    challenge = service.begin_authentication()
    credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge, user_verified=False
    )
    with pytest.raises(PasskeyAuthenticationError):
        await service.complete_authentication(challenge=challenge.challenge, credential=credential)


# --- Clone detection (sign counter) ----------------------------------------


async def test_a_sign_count_that_fails_to_increase_is_rejected_as_a_possible_clone() -> None:
    service = _service()
    user_id = uuid.uuid4()
    _, authenticator = await _register(service, user_id=user_id)

    first_challenge = service.begin_authentication()
    first_credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=first_challenge.challenge
    )
    await service.complete_authentication(
        challenge=first_challenge.challenge, credential=first_credential
    )

    # Replay the SAME (or a non-incrementing) sign count on a second
    # attempt - simulating a cloned authenticator whose counter has fallen
    # out of sync with the genuine one's.
    second_challenge = service.begin_authentication()
    replayed_credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=second_challenge.challenge,
        sign_count_override=1,  # same as the first attempt's count
    )
    with pytest.raises(PasskeyAuthenticationError):
        await service.complete_authentication(
            challenge=second_challenge.challenge, credential=replayed_credential
        )


async def test_a_sign_count_permanently_at_zero_is_not_treated_as_a_clone() -> None:
    """Many platform/synced authenticators (Face ID/Touch ID with iCloud
    Keychain, etc.) never implement a signature counter and always report
    0 - that is normal, expected behavior, not a clone signal.
    """
    service = _service()
    user_id = uuid.uuid4()
    _, authenticator = await _register(
        service, user_id=user_id, authenticator=FakeAuthenticator(sign_count_increments=False)
    )

    for _ in range(3):
        challenge = service.begin_authentication()
        credential = authenticator.build_authentication_credential(
            rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
        )
        await service.complete_authentication(challenge=challenge.challenge, credential=credential)


# --- last_used_at bookkeeping -----------------------------------------------


async def test_successful_authentication_updates_last_used_at() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    passkey, authenticator = await _register(service, user_id=user_id)
    assert passkey.last_used_at is None

    clock.advance(minutes=5)
    challenge = service.begin_authentication()
    credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge.challenge
    )
    await service.complete_authentication(challenge=challenge.challenge, credential=credential)

    updated = await service.list_passkeys(user_id)
    assert updated[0].last_used_at == clock.now

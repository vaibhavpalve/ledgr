"""WebAuthn passkey registration and authentication (IAM-010): a passkey
is a first-class sign-in method here, on equal footing with Google and
email+password - not merely a second factor. A user may register multiple
passkeys (one per device/authenticator), each named for their own
reference, and revoke any one individually without affecting the others
or requiring re-registration of the rest.

Uses the `webauthn` library (py_webauthn) for the actual FIDO2 protocol
work - CBOR/COSE parsing, attestation and assertion verification,
signature checking - rather than hand-rolling it. This is exactly the kind
of low-level cryptographic protocol implementation where a maintained,
widely-used library is the right call over bespoke code: the same
reasoning already applied to Argon2 (passwords), PyJWT (Google ID
tokens), and `cryptography` (envelope encryption) elsewhere in this
package.

Like api.auth.google_oidc, ceremony-in-progress state (the challenge
issued by begin_registration/begin_authentication) is NOT persisted by
this module - it is returned to the caller, who is responsible for
holding it (a server-side flow record, not a client-readable cookie)
until the matching complete_* call. There is no login HTTP endpoint yet
to own that decision.

Registration requires a discoverable (resident) credential and user
verification - not merely user presence - because a passkey is meant to
replace a password entirely (IAM-010 lists it side by side with
email+password, not behind it), so the "just tap it, no email typed
first" usernameless flow, and the assurance that the person present is
who they claim to be, are both load-bearing, not optional conveniences.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import webauthn
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


class PasskeyError(Exception):
    """Base class for anything wrong with a passkey registration or
    authentication attempt.
    """


class PasskeyRegistrationError(PasskeyError):
    """The registration ceremony failed verification - bad signature,
    wrong challenge/origin/relying party, an unsupported algorithm, or the
    authenticator did not perform user verification.
    """


class PasskeyAlreadyRegisteredError(PasskeyRegistrationError):
    """This exact credential is already registered. Structurally very
    unlikely by accident (credential IDs are high-entropy), but the
    database's UNIQUE constraint on credential_id is the real backstop -
    this exception is what the service raises when that constraint would
    otherwise be hit.
    """


class PasskeyAuthenticationError(PasskeyError):
    """The authentication ceremony failed verification - bad signature,
    wrong challenge/origin/relying party, the authenticator did not
    perform user verification, or its signature counter did not increase
    as expected (see the `webauthn` library's own replay/clone check,
    which credential_current_sign_count feeds into).
    """


class UnknownPasskeyError(PasskeyAuthenticationError):
    """The credential_id in the response is not registered to any
    account.
    """


class PasskeyRevokedError(PasskeyAuthenticationError):
    """IAM-010: the credential exists but has been individually revoked.
    The cryptographic signature may be perfectly valid - revocation still
    rejects it, checked BEFORE cryptographic verification runs at all, so
    a revoked passkey costs nothing extra to reject and reveals nothing
    about whether the signature would have been valid.
    """


class PasskeyNotFoundError(PasskeyError):
    """Raised by revoke_passkey when the passkey either does not exist or
    does not belong to the calling user - deliberately the same exception
    for both cases, so a caller cannot distinguish "no such passkey" from
    "that passkey belongs to someone else" by the error alone.
    """


@dataclass(frozen=True, slots=True)
class Passkey:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: list[str]
    aaguid: str | None
    backup_eligible: bool
    backed_up: bool
    # 'platform' (device-bound, e.g. Touch ID/Windows Hello) or
    # 'cross-platform' (roaming, e.g. a USB key or a synced passkey) - or
    # None if the browser didn't report it. IAM-012 names both "passkey"
    # and "platform biometrics bound to a device key" as acceptable MFA
    # factors; recorded for audit, but both count identically for
    # MFA-satisfaction purposes today - see api.auth.mfa.
    authenticator_attachment: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class PasskeyRepository(Protocol):
    async def create(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: list[str],
        aaguid: str | None,
        backup_eligible: bool,
        backed_up: bool,
        authenticator_attachment: str | None,
    ) -> Passkey: ...

    async def get_by_credential_id(self, credential_id: bytes) -> Passkey | None: ...

    async def get_by_id(self, passkey_id: uuid.UUID) -> Passkey | None: ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[Passkey]: ...

    async def update_after_authentication(
        self, passkey_id: uuid.UUID, *, sign_count: int, backed_up: bool, at: datetime
    ) -> None: ...

    async def revoke(self, passkey_id: uuid.UUID, *, at: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class RegistrationChallenge:
    options_json: str  # ready to send to the client as the create() options
    challenge: bytes  # caller must retain and pass back to complete_registration


@dataclass(frozen=True, slots=True)
class AuthenticationChallenge:
    options_json: str  # ready to send to the client as the get() options
    challenge: bytes  # caller must retain and pass back to complete_authentication


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WebAuthnService:
    def __init__(
        self,
        repository: PasskeyRepository,
        *,
        rp_id: str,
        rp_name: str,
        expected_origin: str,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._rp_id = rp_id
        self._rp_name = rp_name
        self._expected_origin = expected_origin
        self._clock = clock

    def begin_registration(
        self, *, user_id: uuid.UUID, user_email: str, existing_passkeys: list[Passkey]
    ) -> RegistrationChallenge:
        """exclude_credentials lists the user's own already-registered,
        non-revoked passkeys so the browser/authenticator won't offer to
        re-register one of them redundantly.
        """
        options = webauthn.generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=user_id.bytes,
            user_name=user_email,
            user_display_name=user_email,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=passkey.credential_id)
                for passkey in existing_passkeys
                if not passkey.is_revoked
            ],
            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
        )
        return RegistrationChallenge(
            options_json=webauthn.options_to_json(options), challenge=options.challenge
        )

    async def complete_registration(
        self, *, user_id: uuid.UUID, name: str, challenge: bytes, credential: RegistrationCredential
    ) -> Passkey:
        if not name.strip():
            raise PasskeyRegistrationError("passkey name must not be blank")

        try:
            verified = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._expected_origin,
                require_user_verification=True,
                supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
            )
        except WebAuthnException as exc:
            raise PasskeyRegistrationError(str(exc)) from exc

        # require_user_verification=True above already enforces this via
        # the library; checked again explicitly rather than trusted,
        # since IAM-010 depends on it and a library behavior change should
        # fail loudly here, not silently stop enforcing it.
        if not verified.user_verified:
            raise PasskeyRegistrationError("authenticator did not perform user verification")

        if await self._repository.get_by_credential_id(verified.credential_id) is not None:
            raise PasskeyAlreadyRegisteredError

        return await self._repository.create(
            user_id=user_id,
            name=name.strip(),
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=[t.value for t in (credential.response.transports or [])],
            aaguid=verified.aaguid,
            backup_eligible=verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE,
            backed_up=verified.credential_backed_up,
            authenticator_attachment=(
                credential.authenticator_attachment.value
                if credential.authenticator_attachment is not None
                else None
            ),
        )

    def begin_authentication(
        self, *, allow_passkeys: list[Passkey] | None = None
    ) -> AuthenticationChallenge:
        """With allow_passkeys omitted (the IAM-010 "click passkey, no
        email typed first" case): a fully discoverable/usernameless
        challenge - the browser prompts with whichever passkeys for this
        relying party the device already knows about, and
        complete_authentication resolves the user from the credential_id
        in the response. With allow_passkeys given (e.g. a specific known
        user, for a future MFA/step-up use): the browser is scoped to only
        that user's non-revoked credentials.
        """
        allow_credentials = None
        if allow_passkeys is not None:
            allow_credentials = [
                PublicKeyCredentialDescriptor(id=passkey.credential_id)
                for passkey in allow_passkeys
                if not passkey.is_revoked
            ]

        options = webauthn.generate_authentication_options(
            rp_id=self._rp_id,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return AuthenticationChallenge(
            options_json=webauthn.options_to_json(options), challenge=options.challenge
        )

    async def complete_authentication(
        self, *, challenge: bytes, credential: AuthenticationCredential
    ) -> Passkey:
        passkey = await self._repository.get_by_credential_id(credential.raw_id)
        if passkey is None:
            raise UnknownPasskeyError

        # Checked BEFORE cryptographic verification: a revoked passkey is
        # rejected unconditionally, regardless of whether its signature
        # would otherwise have verified. This is IAM-010's "individually
        # revocable" guarantee actually enforced, not just recorded.
        if passkey.is_revoked:
            raise PasskeyRevokedError

        try:
            verified = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._expected_origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            # The library itself performs sign-count/replay ("cloned
            # authenticator") detection here - see its
            # verify_authentication_response source: it raises exactly
            # this exception type when the new count fails to exceed the
            # stored one (except when both are 0, which many platform/
            # synced authenticators report permanently and which is NOT
            # itself a clone signal). Wrapped into PasskeyAuthenticationError
            # for a library-independent exception surface; the underlying
            # message is preserved via exception chaining for anyone who
            # needs to distinguish the reason.
            raise PasskeyAuthenticationError(str(exc)) from exc

        if not verified.user_verified:
            raise PasskeyAuthenticationError("authenticator did not perform user verification")

        await self._repository.update_after_authentication(
            passkey.id,
            sign_count=verified.new_sign_count,
            backed_up=verified.credential_backed_up,
            at=self._clock(),
        )
        return passkey

    async def list_passkeys(self, user_id: uuid.UUID) -> list[Passkey]:
        return await self._repository.list_for_user(user_id)

    async def revoke_passkey(self, user_id: uuid.UUID, passkey_id: uuid.UUID) -> None:
        """Individually revocable: only this one passkey is affected. The
        ownership check (passkey.user_id == user_id) is what stops one
        user from revoking another user's passkey by guessing or
        otherwise obtaining its id - raising the SAME PasskeyNotFoundError
        whether the id doesn't exist at all or belongs to someone else, so
        neither case discloses more than the other.
        """
        passkey = await self._repository.get_by_id(passkey_id)
        if passkey is None or passkey.user_id != user_id:
            raise PasskeyNotFoundError

        await self._repository.revoke(passkey_id, at=self._clock())

"""Constructs real, cryptographically valid WebAuthn registration and
authentication ceremonies for tests - no browser or physical authenticator
involved, but every byte is genuine: a real EC keypair, a real CBOR-encoded
COSE public key, real authenticator data, and a real ECDSA signature over
the actual signed bytes a browser would produce. This exercises the real
`webauthn` library verification path (webauthn.verify_registration_response
/ verify_authentication_response), not a mock of it - the same principle
already applied to the hand-signed RS256 JWTs in test_google_oidc.py.

A FakeAuthenticator models one WebAuthn authenticator: it holds its own
keypair and its own signature counter, and can register itself, then sign
authentication assertions, and simulate the two realistic sign-count
behaviors - counting up like a hardware security key, or staying at zero
forever like most platform/synced authenticators.
"""

from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url, encode_cbor, generate_challenge
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    RegistrationCredential,
)

# WebAuthn authenticator data flag bits (§6.1 of the spec).
_FLAG_USER_PRESENT = 0x01
_FLAG_USER_VERIFIED = 0x04
_FLAG_BACKUP_ELIGIBLE = 0x08
_FLAG_ATTESTED_CREDENTIAL_DATA = 0x40


def _build_cose_ec2_public_key(public_key: ec.EllipticCurvePublicKey) -> bytes:
    numbers = public_key.public_numbers()
    cose_key = {
        1: 2,  # kty: EC2
        3: -7,  # alg: ES256
        -1: 1,  # crv: P-256
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    }
    return encode_cbor(cose_key)


def _build_authenticator_data(
    *, rp_id: str, flags: int, sign_count: int, attested_credential_data: bytes = b""
) -> bytes:
    rp_id_hash = hashlib.sha256(rp_id.encode()).digest()
    return (
        rp_id_hash
        + flags.to_bytes(1, "big")
        + sign_count.to_bytes(4, "big")
        + attested_credential_data
    )


def _build_client_data_json(*, type_: str, challenge: bytes, origin: str) -> bytes:
    data = {
        "type": type_,
        "challenge": bytes_to_base64url(challenge),
        "origin": origin,
        "crossOrigin": False,
    }
    return json.dumps(data).encode("utf-8")


class FakeAuthenticator:
    """One simulated WebAuthn authenticator (a "device"). Verified end to
    end against the real `webauthn` library in test_passkeys.py's basic
    round-trip test before being relied on for the security-property tests.
    """

    def __init__(self, *, sign_count_increments: bool = True) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.aaguid = bytes(16)
        self._sign_count_increments = sign_count_increments
        self._sign_count = 0

    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self.private_key.public_key()

    def build_registration_credential(
        self, *, rp_id: str, origin: str, challenge: bytes, user_verified: bool = True
    ) -> RegistrationCredential:
        cose_key = _build_cose_ec2_public_key(self.public_key())
        attested_credential_data = (
            self.aaguid + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose_key
        )
        flags = _FLAG_USER_PRESENT | _FLAG_BACKUP_ELIGIBLE | _FLAG_ATTESTED_CREDENTIAL_DATA
        if user_verified:
            flags |= _FLAG_USER_VERIFIED
        auth_data = _build_authenticator_data(
            rp_id=rp_id,
            flags=flags,
            sign_count=0,
            attested_credential_data=attested_credential_data,
        )
        attestation_object = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data_json = _build_client_data_json(
            type_="webauthn.create", challenge=challenge, origin=origin
        )
        return RegistrationCredential(
            id=bytes_to_base64url(self.credential_id),
            raw_id=self.credential_id,
            response=AuthenticatorAttestationResponse(
                client_data_json=client_data_json, attestation_object=attestation_object
            ),
        )

    def build_authentication_credential(
        self,
        *,
        rp_id: str,
        origin: str,
        challenge: bytes,
        user_verified: bool = True,
        sign_count_override: int | None = None,
    ) -> AuthenticationCredential:
        if self._sign_count_increments:
            self._sign_count += 1
        sign_count = sign_count_override if sign_count_override is not None else self._sign_count

        flags = _FLAG_USER_PRESENT
        if user_verified:
            flags |= _FLAG_USER_VERIFIED
        auth_data = _build_authenticator_data(rp_id=rp_id, flags=flags, sign_count=sign_count)
        client_data_json = _build_client_data_json(
            type_="webauthn.get", challenge=challenge, origin=origin
        )
        signed_data = auth_data + hashlib.sha256(client_data_json).digest()
        signature = self.private_key.sign(signed_data, ec.ECDSA(hashes.SHA256()))

        return AuthenticationCredential(
            id=bytes_to_base64url(self.credential_id),
            raw_id=self.credential_id,
            response=AuthenticatorAssertionResponse(
                client_data_json=client_data_json,
                authenticator_data=auth_data,
                signature=signature,
            ),
        )


def registration_credential_json(credential: RegistrationCredential) -> dict:
    """The wire shape a browser's navigator.credentials.create() promise
    resolves to and a client JSON-serializes for the server - what
    api.auth.routes' passkey enrolment endpoints actually receive as
    `body.credential`, as opposed to the already-parsed
    RegistrationCredential FakeAuthenticator builds for the pure-logic tests
    in test_passkeys.py. See webauthn.helpers.parse_registration_credential_json
    for the exact fields this has to carry.
    """
    return {
        "id": credential.id,
        "rawId": bytes_to_base64url(credential.raw_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(credential.response.client_data_json),
            "attestationObject": bytes_to_base64url(credential.response.attestation_object),
        },
    }


def authentication_credential_json(credential: AuthenticationCredential) -> dict:
    """The HTTP-layer counterpart of registration_credential_json, for the
    sign-in and step-up-verification endpoints.
    """
    return {
        "id": credential.id,
        "rawId": bytes_to_base64url(credential.raw_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(credential.response.client_data_json),
            "authenticatorData": bytes_to_base64url(credential.response.authenticator_data),
            "signature": bytes_to_base64url(credential.response.signature),
        },
    }


def another_authenticators_signature_over(credential: AuthenticationCredential) -> bytes:
    """Signs the SAME authenticator_data/clientDataJSON with a freshly
    generated, unrelated private key - simulating a forged assertion from
    an attacker who does not hold the real authenticator's key. Used to
    prove signature verification actually checks the key, not just that
    *some* well-formed signature is present.
    """
    forged_key = ec.generate_private_key(ec.SECP256R1())
    signed_data = (
        credential.response.authenticator_data
        + hashlib.sha256(credential.response.client_data_json).digest()
    )
    return forged_key.sign(signed_data, ec.ECDSA(hashes.SHA256()))


__all__ = [
    "FakeAuthenticator",
    "another_authenticators_signature_over",
    "authentication_credential_json",
    "generate_challenge",
    "registration_credential_json",
]

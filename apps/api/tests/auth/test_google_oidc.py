"""Pure-logic tests for Google sign-in (IAM-010a, IAM-010b): PKCE
mechanics, scope minimality, and ID token validation - all against a
locally generated RSA keypair via an injected SigningKeyResolver, so
nothing here ever calls Google's real endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.auth.google_oidc import (
    GoogleOidcClient,
    InvalidGoogleIdentityError,
    UnverifiedGoogleEmailError,
    generate_pkce_challenge,
)

_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
_REDIRECT_URI = "https://ledgr.test/auth/google/callback"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()


class _StaticSigningKeyResolver:
    def __init__(self, key: object) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> object:
        return SimpleNamespace(key=self._key)


def _make_id_token(
    *,
    extra_claims: dict[str, object] | None = None,
    omit: list[str] | None = None,
    private_key: object = _PRIVATE_KEY,
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": _CLIENT_ID,
        "sub": "1234567890",
        "email": "user@example.com",
        "email_verified": True,
        "name": "Test User",
        "picture": "https://example.com/pic.jpg",
        "nonce": "the-expected-nonce",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(extra_claims or {})
    for key in omit or []:
        claims.pop(key, None)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _client(
    id_token: str | None = None,
    *,
    token_status: int = 200,
    signing_key: object = _PUBLIC_KEY,
) -> GoogleOidcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if token_status != 200:
            return httpx.Response(token_status, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"id_token": id_token, "token_type": "Bearer"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleOidcClient(
        client_id=_CLIENT_ID,
        client_secret="test-client-secret",
        redirect_uri=_REDIRECT_URI,
        http_client=http_client,
        signing_key_resolver=_StaticSigningKeyResolver(signing_key),
    )


# --- PKCE -------------------------------------------------------------


def test_pkce_challenge_matches_rfc_7636() -> None:
    challenge = generate_pkce_challenge()

    assert 43 <= len(challenge.code_verifier) <= 128
    assert challenge.code_challenge_method == "S256"
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(challenge.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge.code_challenge == expected_challenge


def test_each_pkce_challenge_is_unique() -> None:
    first = generate_pkce_challenge()
    second = generate_pkce_challenge()
    assert first.code_verifier != second.code_verifier


# --- Authorization request: scope minimality (IAM-010a) ----------------


def test_start_sign_in_requests_exactly_openid_email_profile() -> None:
    client = _client()
    request = client.start_sign_in()

    query = parse_qs(urlparse(request.authorization_url).query)
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]


@pytest.mark.parametrize("forbidden", ["gmail", "drive", "calendar", "contacts"])
def test_start_sign_in_never_requests_gmail_drive_calendar_or_contacts(forbidden: str) -> None:
    client = _client()
    request = client.start_sign_in()
    assert forbidden not in request.authorization_url.lower()


def test_start_sign_in_produces_unique_state_nonce_and_verifier_each_call() -> None:
    client = _client()
    first = client.start_sign_in()
    second = client.start_sign_in()

    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier


# --- ID token validation -------------------------------------------------


async def test_complete_sign_in_returns_the_identity_for_a_valid_verified_token() -> None:
    id_token = _make_id_token()
    client = _client(id_token)

    identity = await client.complete_sign_in(
        code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
    )

    assert identity.subject == "1234567890"
    assert identity.email == "user@example.com"
    assert identity.name == "Test User"


async def test_complete_sign_in_rejects_email_verified_false() -> None:
    """IAM-010b, the direct case."""
    id_token = _make_id_token(extra_claims={"email_verified": False})
    client = _client(id_token)

    with pytest.raises(UnverifiedGoogleEmailError):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_rejects_a_missing_email_verified_claim() -> None:
    """IAM-010b again, but for absence rather than an explicit false - a
    missing claim must not be treated as trusted-by-default.
    """
    id_token = _make_id_token(omit=["email_verified"])
    client = _client(id_token)

    with pytest.raises(UnverifiedGoogleEmailError):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_rejects_nonce_mismatch() -> None:
    id_token = _make_id_token(extra_claims={"nonce": "the-real-nonce"})
    client = _client(id_token)

    with pytest.raises(InvalidGoogleIdentityError, match="nonce"):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="a-different-nonce"
        )


async def test_complete_sign_in_rejects_wrong_audience() -> None:
    id_token = _make_id_token(extra_claims={"aud": "someone-elses-client-id"})
    client = _client(id_token)

    with pytest.raises(InvalidGoogleIdentityError):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_rejects_wrong_issuer() -> None:
    id_token = _make_id_token(extra_claims={"iss": "https://evil.example.com"})
    client = _client(id_token)

    with pytest.raises(InvalidGoogleIdentityError, match="issuer"):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_rejects_an_expired_token() -> None:
    now = int(time.time())
    id_token = _make_id_token(extra_claims={"iat": now - 7200, "exp": now - 3600})
    client = _client(id_token)

    with pytest.raises(InvalidGoogleIdentityError):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_rejects_a_token_signed_with_the_wrong_key() -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    id_token = _make_id_token(private_key=other_key)
    client = _client(id_token)  # resolver still hands back the ORIGINAL public key

    with pytest.raises(InvalidGoogleIdentityError):
        await client.complete_sign_in(
            code="auth-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )


async def test_complete_sign_in_raises_when_the_token_endpoint_fails() -> None:
    client = _client(id_token=None, token_status=400)

    with pytest.raises(InvalidGoogleIdentityError):
        await client.complete_sign_in(
            code="a-bad-code", code_verifier="verifier", expected_nonce="the-expected-nonce"
        )

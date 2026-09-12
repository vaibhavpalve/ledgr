"""Google Sign-In (IAM-010a, IAM-010b): OpenID Connect authorization code
flow with PKCE.

--- Why the scope list must stay exactly openid, email, profile ---

Every scope Google grants becomes a capability the resulting access token
carries - Gmail, Drive, Calendar and Contacts scopes each let that token
read or write the corresponding Google API, not just prove who the user
is. LEDGR has no legitimate use for any of that on a login path: we are
verifying identity, not requesting delegated access to someone's personal
email, files, calendar or address book. Requesting those scopes anyway
would mean:

  1. Every Dutch SMB owner signing in would see a consent screen asking for
     permissions wildly disproportionate to "log into your bookkeeping
     software" - a trust-destroying mismatch between what the button says
     ("Continue with Google") and what it actually asks for.
  2. The stored access token (even if LEDGR never calls those APIs) becomes
     a far more valuable target: a compromise of LEDGR's token storage
     would hand an attacker read/write access to users' email and files,
     not just their bookkeeping data - a blast radius LEDGR has no business
     creating.
  3. It would directly violate IAM-010a, which prohibits this outright:
     "No Gmail, Drive, Calendar or Contacts scopes are requested at any
     point."

`openid` is what makes this an OpenID Connect request at all - Google only
issues an ID token (the signed identity assertion this module verifies)
when it is present; without it this would be a plain OAuth grant with no
built-in identity proof. `email` (plus the `email_verified` claim it
carries) is the one piece of identity IAM-010b's rejection check depends
on. `profile` provides a display name and picture for the UI - the
absolute maximum footprint that still lets LEDGR show "Signed in as
<name>". Nothing else belongs in _SCOPES, ever - not "just to check," not
temporarily, not behind a feature flag. If a future feature genuinely needs
Google API access (there is no such feature today), that is a separate,
explicit, additional OAuth grant with its own consent and its own stored
token - never folded into this login scope.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt

from api.config import settings

_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
# Google's ID tokens have used both forms of `iss` historically; both are
# accepted, and nothing else is.
_VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

# IAM-010a. See module docstring for why this list is exactly these three
# values and must never grow.
_SCOPES = ("openid", "email", "profile")


class GoogleSignInError(Exception):
    """Base class for anything wrong with a Google sign-in attempt."""


class InvalidGoogleIdentityError(GoogleSignInError):
    """Token exchange or ID token validation failed: bad signature, wrong
    audience or issuer, expired token, or a nonce that doesn't match what
    this server issued (replay protection).
    """


class UnverifiedGoogleEmailError(GoogleSignInError):
    """IAM-010b: the `email_verified` claim was not `true`. No account is
    created or matched for this identity - this exception is raised before
    any such action is possible, from inside _extract_identity, so there is
    no code path in this module that hands back an identity for an
    unverified email.
    """

    def __init__(self, email: str | None) -> None:
        self.email = email
        super().__init__(f"Google identity for {email!r} has an unverified email")


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    # `subject` is the `sub` claim - Google's stable, unique account id.
    # Never use email as a join key; see migrations/0004_google_identity.sql.
    subject: str
    email: str
    name: str | None
    picture: str | None
    # RFC 8176 Authentication Method Reference values from the ID token's
    # `amr` claim, if present. IAM-010e: interpreted by
    # api.auth.mfa.google_asserts_second_factor, not here - this class
    # only carries the verified claim data through; empty tuple when the
    # claim is absent (Google's real-world token issuance often omits it).
    amr: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PkceChallenge:
    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"


def generate_pkce_challenge() -> PkceChallenge:
    """RFC 7636 §4.1: code_verifier is 43-128 characters from
    [A-Za-z0-9-._~]. 32 random bytes, base64url-encoded without padding,
    produces exactly 43.
    """
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkceChallenge(code_verifier=code_verifier, code_challenge=code_challenge)


@dataclass(frozen=True, slots=True)
class GoogleSignInRequest:
    """Everything issuing the authorization redirect requires. `state` and
    `code_verifier` must be retained server-side (e.g. a short-lived,
    server-held flow record - not a client-readable cookie) and supplied
    back to complete_sign_in for the matching callback; there is no login
    HTTP endpoint in this change to own that storage decision yet.
    """

    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


class SigningKeyResolver(Protocol):
    """Matches jwt.PyJWKClient's interface - injectable so tests can supply
    a locally generated key instead of fetching Google's real JWKS over the
    network.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class GoogleOidcClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_client: httpx.AsyncClient | None = None,
        signing_key_resolver: SigningKeyResolver | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http_client or httpx.AsyncClient(timeout=5.0)
        self._signing_key_resolver = signing_key_resolver or jwt.PyJWKClient(_JWKS_URI)

    def start_sign_in(self) -> GoogleSignInRequest:
        pkce = generate_pkce_challenge()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(_SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": pkce.code_challenge_method,
        }
        return GoogleSignInRequest(
            authorization_url=f"{_AUTHORIZATION_ENDPOINT}?{urlencode(params)}",
            state=state,
            nonce=nonce,
            code_verifier=pkce.code_verifier,
        )

    async def complete_sign_in(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> GoogleIdentity:
        """Exchanges the authorization code for tokens and validates the ID
        token. Raises InvalidGoogleIdentityError for anything structurally
        wrong (bad signature, wrong audience/issuer, nonce mismatch) and
        UnverifiedGoogleEmailError specifically for IAM-010b - callers that
        need to tell the two apart (e.g. to show a different message) can
        catch each separately; both are GoogleSignInError.
        """
        id_token = await self._exchange_code(code=code, code_verifier=code_verifier)
        claims = self._verify_id_token(id_token, expected_nonce=expected_nonce)
        return _extract_identity(claims)

    async def _exchange_code(self, *, code: str, code_verifier: str) -> str:
        response = await self._http.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
                # PKCE (IAM-010a): proves this token-exchange request comes
                # from whoever started the flow that produced `code`, even
                # though the authorization step happened in the user's
                # browser where the code could in principle be intercepted.
                # client_secret (above) is a separate, complementary
                # protection - it authenticates THIS confidential backend
                # client to Google; PKCE authenticates the flow itself.
                # Neither replaces the other.
                "code_verifier": code_verifier,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise InvalidGoogleIdentityError(
                f"token exchange failed: {response.status_code} {response.text}"
            ) from exc

        body = response.json()
        id_token = body.get("id_token")
        if not id_token:
            raise InvalidGoogleIdentityError("token response contained no id_token")
        return str(id_token)

    def _verify_id_token(self, id_token: str, *, expected_nonce: str) -> dict[str, Any]:
        try:
            signing_key = self._signing_key_resolver.get_signing_key_from_jwt(id_token)
            claims: dict[str, Any] = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._client_id,
                options={"require": ["exp", "iat", "sub", "aud", "iss", "email"]},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidGoogleIdentityError(f"invalid ID token: {exc}") from exc

        # PyJWT validates `aud` (via audience=) and `exp`/`iat` on its own;
        # `iss` and `nonce` are OIDC-specific and checked explicitly here.
        if claims.get("iss") not in _VALID_ISSUERS:
            raise InvalidGoogleIdentityError(f"unexpected issuer: {claims.get('iss')!r}")
        if claims.get("nonce") != expected_nonce:
            raise InvalidGoogleIdentityError("nonce mismatch - possible replay")

        return claims


def _extract_identity(claims: dict[str, Any]) -> GoogleIdentity:
    # IAM-010b: this check runs here, inside the OIDC client itself, before
    # a GoogleIdentity can be constructed at all - there is no lower-level
    # function downstream callers could reach instead that would skip it.
    # `is not True` deliberately rejects `False`, a missing claim (`None`),
    # and any non-boolean value alike - only an explicit `true` passes.
    if claims.get("email_verified") is not True:
        raise UnverifiedGoogleEmailError(claims.get("email"))

    raw_amr = claims.get("amr") or []
    return GoogleIdentity(
        subject=str(claims["sub"]),
        email=str(claims["email"]),
        name=claims.get("name"),
        picture=claims.get("picture"),
        amr=tuple(str(value) for value in raw_amr),
    )


def build_google_oidc_client() -> GoogleOidcClient:
    missing = [
        name
        for name, value in (
            ("GOOGLE_CLIENT_ID", settings.google_client_id),
            ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
            ("GOOGLE_REDIRECT_URI", settings.google_redirect_uri),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Google sign-in is not configured - missing: {', '.join(missing)}")

    assert settings.google_client_id is not None
    assert settings.google_client_secret is not None
    assert settings.google_redirect_uri is not None
    return GoogleOidcClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
    )

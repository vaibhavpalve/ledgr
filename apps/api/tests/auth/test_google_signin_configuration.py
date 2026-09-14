"""The Google sign-in wiring review (docs/founder-review-2026-09-14.md §6):
no OAuth client exists on this machine, so what CAN be proven without one
is proven here - that the three settings, once present, produce a client
whose authorization redirect carries exactly the registered redirect URI
and IAM-010a's three scopes, that their absence is the clean "unavailable"
answer and not a placeholder, and that the redirect-URI contract the
founder has to register is written down where they will look for it.

The full start -> callback -> signup_required -> POST /v1/auth/signup/google
round trip against a faked OIDC client is
tests/integration/test_google_initiated_signup.py (real RS256 ID token, real
ceremony rows); it needs Postgres, and it is what actually exercises the
wiring. This file is the part that runs everywhere.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from api.auth import google_oidc
from api.auth.google_oidc import build_google_oidc_client
from api.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_unconfigured_google_sign_in_is_unavailable_not_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_client_secret", None)
    monkeypatch.setattr(settings, "google_redirect_uri", None)

    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        build_google_oidc_client()


def test_a_configured_client_redirects_to_exactly_the_registered_uri_with_minimal_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What Google compares byte for byte at both legs of the flow. A
    trailing slash dropped here is a redirect_uri_mismatch in front of the
    founder the first time the button is pressed for real.
    """
    monkeypatch.setattr(settings, "google_client_id", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:5173/")

    flow = build_google_oidc_client().start_sign_in()

    parsed = urlparse(flow.authorization_url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == google_oidc._AUTHORIZATION_ENDPOINT
    assert query["redirect_uri"] == ["http://localhost:5173/"]
    assert query["client_id"] == ["client-id.apps.googleusercontent.com"]
    assert query["response_type"] == ["code"]
    assert set(query["scope"][0].split()) == {"openid", "email", "profile"}  # IAM-010a
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [flow.state]
    assert query["nonce"] == [flow.nonce]


def test_the_redirect_uri_contract_is_documented_for_the_founder() -> None:
    """§6 of the founder review: the one thing only the founder can do is
    register the OAuth client, and the URI they must register is the web
    app's origin with a trailing slash - not an API path. .env.example is
    where they will look.
    """
    example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "GOOGLE_REDIRECT_URI=" in example
    assert "http://localhost:5173/" in example
    assert "readGoogleCallbackParams" in example
    assert "/v1/auth/login/google/callback" in example

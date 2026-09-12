"""SEC-004's anti-CSRF check: only ever engages for a request carrying the
session cookie (see api.security.csrf's module docstring for why a
bearer-token request is not in CSRF's threat model at all), and even then
only for state-changing methods.

No route in this codebase issues the session cookie yet, so these tests
build the cookie pair directly (as a future login endpoint would, via
api.security.cookies) rather than going through a login flow that does not
exist.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.security.cookies import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from api.security.csrf import CSRF_HEADER_NAME, CsrfProtectionMiddleware


def _probe_app() -> tuple[FastAPI, list[bool]]:
    handler_calls: list[bool] = []
    app = FastAPI()
    app.add_middleware(CsrfProtectionMiddleware)

    @app.post("/v1/things")
    def create_thing() -> dict[str, str]:
        handler_calls.append(True)
        return {"status": "created"}

    @app.get("/v1/things")
    def list_things() -> dict[str, str]:
        handler_calls.append(True)
        return {"status": "ok"}

    return app, handler_calls


def test_a_bearer_only_request_is_never_subject_to_the_check() -> None:
    """No session cookie at all — today's actual auth mechanism for every
    real route — passes straight through, proving this middleware cannot
    break the app as it exists now.
    """
    app, handler_calls = _probe_app()
    client = TestClient(app)

    response = client.post(
        "/v1/things", headers={"Authorization": "Bearer some-jwt-does-not-matter"}
    )

    assert response.status_code == 200
    assert handler_calls == [True]


def test_a_session_cookie_with_no_csrf_token_is_rejected() -> None:
    app, handler_calls = _probe_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "session-value")

    response = client.post("/v1/things")

    assert response.status_code == 403
    assert handler_calls == []


def test_a_mismatched_csrf_token_is_rejected() -> None:
    app, handler_calls = _probe_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "session-value")
    client.cookies.set(CSRF_COOKIE_NAME, "the-real-token")

    response = client.post("/v1/things", headers={CSRF_HEADER_NAME: "a-forged-token"})

    assert response.status_code == 403
    assert handler_calls == []


def test_a_matching_csrf_token_is_accepted() -> None:
    app, handler_calls = _probe_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "session-value")
    client.cookies.set(CSRF_COOKIE_NAME, "the-real-token")

    response = client.post("/v1/things", headers={CSRF_HEADER_NAME: "the-real-token"})

    assert response.status_code == 200
    assert handler_calls == [True]


def test_safe_methods_are_never_checked_even_with_a_session_cookie() -> None:
    app, handler_calls = _probe_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "session-value")

    response = client.get("/v1/things")

    assert response.status_code == 200
    assert handler_calls == [True]


def test_the_real_app_is_wired_with_csrf_protection() -> None:
    """The regression guard: proves CsrfProtectionMiddleware is actually
    added in api.main, not just implemented and left unwired (the fate
    api.rate_limit_middleware.RateLimitingMiddleware documents for itself).

    A forged request carrying a session cookie but no CSRF token must be
    rejected by THIS middleware, before TenantContextMiddleware ever gets to
    reject it for the (also true, but different) reason that it carries no
    bearer token. If api.main stopped adding CsrfProtectionMiddleware, this
    request would 401 instead of 403, and the test would fail.
    """
    from api.main import app

    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "whatever")

    response = client.put("/v1/switcher/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 403
    assert response.json()["reason"] == "csrf_token_missing_or_mismatched"

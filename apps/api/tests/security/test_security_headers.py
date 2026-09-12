"""SEC-003 / SEC-004 at the HTTP edge: proves the actual header content
(nonce-based CSP with no unsafe-inline/unsafe-eval, HSTS with preload) AND
that api.main.app is actually wired with SecurityHeadersMiddleware — the
second half is what makes removing the middleware from api.main a failing
test rather than a silent regression, the same reason
tests/test_authz_middleware.py sits beside tests/test_authz_coverage.py.
"""

from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.security.headers import SecurityHeadersMiddleware

_NONCE_PATTERN = re.compile(r"'nonce-([A-Za-z0-9_-]+)'")


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise HTTPException(status_code=403, detail="nope")

    return app


def test_csp_carries_a_nonce_and_no_unsafe_directives() -> None:
    client = TestClient(_probe_app())

    response = client.get("/ok")

    csp = response.headers["content-security-policy"]
    assert _NONCE_PATTERN.search(csp) is not None
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "default-src 'none'" in csp


def test_the_nonce_is_different_on_every_request() -> None:
    client = TestClient(_probe_app())

    first = _NONCE_PATTERN.search(client.get("/ok").headers["content-security-policy"])
    second = _NONCE_PATTERN.search(client.get("/ok").headers["content-security-policy"])

    assert first is not None and second is not None
    assert first.group(1) != second.group(1)


def test_hsts_meets_preload_list_requirements() -> None:
    client = TestClient(_probe_app())

    hsts = client.get("/ok").headers["strict-transport-security"]

    match = re.search(r"max-age=(\d+)", hsts)
    assert match is not None
    # hstspreload.org's own minimum is one year; this asserts at least that,
    # not the exact value, so tightening the policy later cannot break it.
    assert int(match.group(1)) >= 31536000
    assert "includeSubDomains" in hsts
    assert "preload" in hsts


def test_headers_survive_a_handled_error_response() -> None:
    """Proves the middleware wraps an error response, not just the happy
    path — a route's HTTPException still passes back out through this
    middleware's `dispatch`, because it is added outermost (see api.main).
    """
    client = TestClient(_probe_app())

    response = client.get("/boom")

    assert response.status_code == 403
    assert "content-security-policy" in response.headers
    assert "strict-transport-security" in response.headers


def test_the_real_app_is_wired_with_security_headers() -> None:
    """The regression guard: if api.main stops adding
    SecurityHeadersMiddleware, this fails — proving SEC-003/SEC-004 headers
    on the actual application, not just a probe app that happens to add the
    same middleware.
    """
    from api.main import app

    client = TestClient(app)

    response = client.get("/health")

    assert "content-security-policy" in response.headers
    assert "strict-transport-security" in response.headers

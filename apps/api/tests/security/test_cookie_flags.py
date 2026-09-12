"""SEC-004: `Secure`, `HttpOnly`, `SameSite=Strict` on the session cookie —
proved on the helper functions AND enforced structurally so nothing else in
the codebase can set a cookie without them.

No route calls these functions yet (see api.security.cookies' module
docstring) — this proves the PRIMITIVE is correct, ready for the login
endpoint that will call it, the same posture
tests/auth/test_sessions.py takes toward
SessionService.record_reauthentication before any sensitive-action route
exists.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.responses import Response

from api.security.cookies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    set_csrf_cookie,
    set_session_cookie,
)

_API_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "api"
_SANCTIONED_FILE = _API_SOURCE_ROOT / "security" / "cookies.py"
_COOKIE_CALL = re.compile(r"\.(set_cookie|delete_cookie)\(")


def test_set_session_cookie_carries_every_sec_004_flag() -> None:
    response = Response()

    set_session_cookie(response, "opaque-token-value", max_age_seconds=3600)

    cookie_header = response.headers["set-cookie"]
    assert cookie_header.startswith(f"{SESSION_COOKIE_NAME}=opaque-token-value")
    assert "Secure" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "samesite=strict" in cookie_header.lower()
    assert "path=/" in cookie_header.lower()


def test_csrf_cookie_is_not_httponly_but_is_still_secure_and_strict() -> None:
    """Deliberately readable by JavaScript — see CSRF_COOKIE_NAME's
    docstring for why that is the point of the double-submit pattern, not a
    gap in this test's assertions.
    """
    response = Response()

    set_csrf_cookie(response, "csrf-token-value", max_age_seconds=3600)

    cookie_header = response.headers["set-cookie"]
    assert cookie_header.startswith(f"{CSRF_COOKIE_NAME}=csrf-token-value")
    assert "Secure" in cookie_header
    assert "HttpOnly" not in cookie_header
    assert "samesite=strict" in cookie_header.lower()


def test_clear_session_cookie_expires_both_cookies_with_matching_flags() -> None:
    response = Response()

    clear_session_cookie(response)

    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    session_header = next(h for h in headers if h.startswith(f"{SESSION_COOKIE_NAME}="))
    csrf_header = next(h for h in headers if h.startswith(f"{CSRF_COOKIE_NAME}="))
    assert "Secure" in session_header and "HttpOnly" in session_header
    assert "Secure" in csrf_header and "HttpOnly" not in csrf_header
    assert "max-age=0" in session_header.lower()
    assert "max-age=0" in csrf_header.lower()


def test_no_other_file_sets_a_cookie_directly() -> None:
    """The structural half: api.security.cookies is the ONLY sanctioned way
    to set or clear a cookie anywhere in this codebase. A future login route
    that wrote `response.set_cookie(...)` inline instead of calling
    set_session_cookie would bypass every SEC-004 flag this module enforces
    — this fails CI the moment that happens, the same guard
    tests/test_isolation_coverage.py gives IAM-005.
    """
    offenders: list[str] = []
    for path in _API_SOURCE_ROOT.rglob("*.py"):
        if path == _SANCTIONED_FILE or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _COOKIE_CALL.search(text):
            offenders.append(f"apps/api/src/api/{path.relative_to(_API_SOURCE_ROOT).as_posix()}")

    assert not offenders, (
        "These files call .set_cookie(/.delete_cookie( directly instead of "
        "going through api.security.cookies (SEC-004's Secure/HttpOnly/"
        "SameSite=Strict flags live only there):\n" + "\n".join(offenders)
    )

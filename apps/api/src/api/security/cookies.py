"""SEC-004's cookie flags (`Secure`, `HttpOnly`, `SameSite=Strict`) — as the
only sanctioned way to set or clear the session cookie anywhere in this
codebase.

No route sets this cookie yet: authentication today is a bearer token read
from the `Authorization` header (api.tenancy), and
docs/decisions/ADR-005-authentication-foundation.md is explicit that "no
login UI or HTTP endpoint was requested" is still true. This module is the
primitive a future cookie-based web session will need, built now so that
when it exists it has no way to set a session cookie EXCEPT through a
function that already enforces every SEC-004 flag — the same
primitive-before-the-caller posture as
api.auth.sessions.SessionService.record_reauthentication.

tests/security/test_cookie_flags.py enforces this structurally: it fails CI
if any other file under api/ calls `.set_cookie(`/`.delete_cookie(` directly,
so a future login route cannot bypass this module by construction.
"""

from __future__ import annotations

from starlette.responses import Response

#: A name of the app's own, not a framework default (`session`, `sessionid`)
#: — a generic name is one a same-site sibling application on the same
#: parent domain could collide with or make assumptions about.
SESSION_COOKIE_NAME = "ledgr_session"

#: The companion cookie for double-submit CSRF verification
#: (api.security.csrf) — readable by JavaScript (NOT HttpOnly) by design: the
#: web client reads it and echoes it back as the X-CSRF-Token header. It is
#: not itself a credential; knowing its value alongside the HttpOnly,
#: script-unreadable session cookie proves nothing an attacker's cross-site
#: request could not already do without CSRF protection at all. What it
#: proves is that the request was assembled by code that can read same-site
#: cookies — i.e. not a cross-site form or image tag.
CSRF_COOKIE_NAME = "ledgr_csrf"


def set_session_cookie(response: Response, value: str, *, max_age_seconds: int) -> None:
    """SEC-004: `Secure`, `HttpOnly`, `SameSite=Strict`, every time. There is
    no parameter to relax any of the three — a caller that needs a different
    posture needs a different, separately reviewed function, not an optional
    flag on this one that would quietly weaken every existing caller's
    assumption about what it does.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=value,
        max_age=max_age_seconds,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def set_csrf_cookie(response: Response, token: str, *, max_age_seconds: int) -> None:
    """Issued alongside the session cookie — api.security.csrf verifies the
    pairing on every unsafe-method request. Not HttpOnly: see
    CSRF_COOKIE_NAME's docstring for why that is deliberate, not an
    oversight.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Logout / session revocation. Flags must match how each cookie was
    set, or some browsers will refuse to delete it.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )

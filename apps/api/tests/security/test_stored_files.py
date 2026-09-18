"""SEC-005's response headers, proved on the helper AND enforced structurally
so no other file can write its own drifting copy of them.

ADR-063 removed the separate document origin, which makes these headers the
only control on what a browser does with bytes somebody uploaded. The
structural test below is the half that matters most: before ADR-063 a route
that forgot a header still landed on a cookie-less origin, and now there is
nothing behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.security.stored_files import stored_file_response

_API_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "api"
_SANCTIONED_FILE = _API_SOURCE_ROOT / "security" / "stored_files.py"
#: The sandbox policy rather than every header name: `Content-Security-Policy`
#: alone would match api.security.headers' nonce-based SEC-003 policy, which is
#: a different control on ordinary responses. This exact value is only ever
#: correct for stored bytes.
_SANDBOX_POLICY = re.compile(r"sandbox;\s*default-src")


def test_every_sec_005_header_is_present() -> None:
    response = stored_file_response(content=b"%PDF-1.7", content_type="application/pdf")

    assert response.headers["Content-Disposition"] == "attachment"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_the_verified_content_type_is_sent() -> None:
    """`nosniff` is only meaningful next to a type worth not sniffing past, so
    the type sniffed from the bytes at upload still travels (ADR-063).
    """
    response = stored_file_response(content=b"\x89PNG\r\n\x1a\n", content_type="image/png")

    assert response.headers["Content-Type"].startswith("image/png")
    assert response.body == b"\x89PNG\r\n\x1a\n"


def test_extra_headers_travel_alongside() -> None:
    response = stored_file_response(
        content=b"bytes", content_type="application/pdf", extra_headers={"X-Content-SHA256": "ab12"}
    )

    assert response.headers["X-Content-SHA256"] == "ab12"
    assert response.headers["Content-Disposition"] == "attachment"


def test_a_caller_cannot_weaken_a_security_header() -> None:
    """Silently ignoring the override would be worse than refusing it: the call
    site would read as though it had taken effect.
    """
    with pytest.raises(ValueError, match="Content-Disposition"):
        stored_file_response(
            content=b"bytes",
            content_type="text/html",
            extra_headers={"Content-Disposition": "inline"},
        )


def test_the_override_check_is_case_insensitive() -> None:
    """HTTP header names are case-insensitive, so a lowercase spelling would
    otherwise slip past the check and be emitted alongside ours.
    """
    with pytest.raises(ValueError, match="content-security-policy"):
        stored_file_response(
            content=b"bytes",
            content_type="image/svg+xml",
            extra_headers={"content-security-policy": "default-src *"},
        )


def test_no_other_file_writes_the_sandbox_policy_itself() -> None:
    """The structural half: api.security.stored_files is the ONLY place the
    stored-file sandbox policy is written. A new byte-serving route that built
    its own Response with a hand-copied set of headers would drift from this
    one silently - this fails CI the moment that happens, the same guard
    tests/security/test_cookie_flags.py gives SEC-004.
    """
    offenders: list[str] = []
    for path in _API_SOURCE_ROOT.rglob("*.py"):
        if path == _SANCTIONED_FILE or "__pycache__" in path.parts:
            continue
        if _SANDBOX_POLICY.search(path.read_text(encoding="utf-8")):
            offenders.append(f"apps/api/src/api/{path.relative_to(_API_SOURCE_ROOT).as_posix()}")

    assert not offenders, (
        "These files write the stored-file sandbox policy themselves instead of "
        "returning stored_file_response (SEC-005's headers live only there, and "
        "since ADR-063 nothing stands behind them):\n" + "\n".join(offenders)
    )

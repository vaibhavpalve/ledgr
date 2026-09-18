"""The single way to return bytes somebody uploaded - SEC-005, ADR-063.

Every response carrying stored file content goes through
`stored_file_response`, and that is the whole reason this module exists.
Until ADR-063 these headers were the first of two layers: a separate
document origin stood behind them, so a route that forgot one still had
somewhere safe for the bytes to land. ADR-063 removed that origin, which
makes these headers the only control there is - and a control written out
by hand at each call site is one someone adds a route without.

Callers cannot override any of them. `extra_headers` carries things that
are not security controls (a content hash, say); a key that collides with
one of these raises rather than quietly winning or quietly losing.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi.responses import Response

#: Every one is a control rather than a convention:
#:
#:   Content-Disposition: attachment
#:       The browser saves rather than renders. A PDF rendered inline runs
#:       its own scripts in the origin that served it.
#:   X-Content-Type-Options: nosniff
#:       Stops a browser second-guessing the type we verified from the bytes
#:       and rendering it as something else, which would undo the first
#:       header at the last possible moment.
#:   Content-Security-Policy: sandbox; default-src 'none'
#:       For anything that renders anyway: an opaque origin, where nothing
#:       loads and nothing executes. This is what stands in for the separate
#:       origin ADR-063 removed.
#:   Cache-Control: private, no-store
#:       A source document is financial evidence under a 7-year retention
#:       policy; it does not belong in a shared cache, and `no-store` keeps
#:       it off disk on the way through.
_SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Disposition": "attachment",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, no-store",
}

_PROTECTED_NAMES = frozenset(name.lower() for name in _SECURITY_HEADERS)


def stored_file_response(
    *,
    content: bytes,
    content_type: str,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    """Stored bytes, under the headers SEC-005 requires.

    `content_type` is the type verified from the bytes at upload, not
    anything a client asserted - the value is still sent, because `nosniff`
    is only meaningful next to a type worth not sniffing past.
    """
    headers = dict(extra_headers or {})
    # Matched case-insensitively: HTTP header names are, so a lowercase
    # "content-disposition" would otherwise slip past and end up emitted
    # alongside ours rather than rejected.
    collisions = sorted(name for name in headers if name.lower() in _PROTECTED_NAMES)
    if collisions:
        raise ValueError(
            f"{collisions} are applied to every stored-file response and cannot be set "
            f"per call site (SEC-005, ADR-063)"
        )
    headers.update(_SECURITY_HEADERS)
    return Response(content=content, media_type=content_type, headers=headers)

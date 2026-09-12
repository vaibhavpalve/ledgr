"""Localised error and validation bodies - FR-UX-007, FR-LOC-001.

    FR-UX-007  Error and validation messages are written in the user's
               language by a person, reviewed as content, and pass the D5
               test. Untranslated or developer-facing strings never reach a
               user.

--- The shape, and why it has two halves ---

Every error body this produces looks like:

    {"message": "U heeft geen rechten voor deze actie.",
     "reason": "no_matching_grant",
     ...}

`message` is for a person and is localised. `reason` is for a machine - a
client branching on it, an operator grepping a log, a test asserting on it -
and is a stable English token that is never translated.

Separating them is what lets FR-UX-007 be satisfied without making the API
harder to operate. The tempting alternative is one localised sentence carrying
the machine detail inside it ("Not permitted to post journal_entry"), and it
fails both ways at once: `post` and `journal_entry` are developer-facing
strings inside a user-facing sentence, and a client that wanted to branch on
the failure would have to parse a translated string to do it.

--- Which language ---

`Accept-Language` on the request, which the LEDGR clients set to the language
the UI is currently in. See api.i18n.language for why that rather than the
stored `users.language` column: this is the answer that cannot be stale, and
FR-LOC-001a's "taking effect immediately" has to hold on the server too.

Note what this does NOT need: an authenticated user. A validation error on the
signup form is answered in the visitor's language, before any account exists
(IAM-010g), because the header is there either way.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from api.i18n.catalogue import translate
from api.i18n.language import Language, negotiate_language


def request_language(request: Request) -> Language:
    """The language to answer THIS request in.

    Also usable as a FastAPI dependency (`Depends(request_language)`), which
    is how a handler that renders text into a success response gets it.
    """
    return negotiate_language(request.headers.get("accept-language"))


def problem(
    request: Request,
    status_code: int,
    key: str,
    *,
    reason: str,
    **extra: object,
) -> HTTPException:
    """An HTTPException whose `message` a person can read and whose `reason` a
    machine can branch on.

    Returned rather than raised, so the call site reads `raise problem(...)`
    and a reader sees where control leaves - the same reason
    api.authz.dependencies raises its own HTTPExceptions inline rather than
    delegating the raise.
    """
    detail: dict[str, object] = {
        "message": translate(key, request_language(request), **extra),
        "reason": reason,
    }
    # Machine-readable context (ids, the supported set) travels beside the
    # sentence rather than inside it, so a client never parses prose.
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def message(request: Request, key: str, **params: object) -> str:
    """Just the sentence, for a body that is not an error."""
    return translate(key, request_language(request), **params)

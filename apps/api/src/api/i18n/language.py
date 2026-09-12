"""Which language the API answers a request in - FR-LOC-001, FR-LOC-001b,
FR-UX-007.

    FR-LOC-001b  Language is a per-user setting, not per-organization.
    FR-UX-007    Error and validation messages are written in the user's
                 language by a person. Untranslated or developer-facing
                 strings never reach a user.

--- Two sources, and which one applies when ---

The API produces user-facing text in two very different situations, and they
need different answers:

    REQUEST-SCOPED      an error, a validation message, a response body. The
                        language comes from the request's `Accept-Language`
                        header, which the LEDGR clients set to the language
                        the UI is CURRENTLY in.
    SERVER-INITIATED    an e-mail, a push notification, a scheduled PDF.
                        There is no request, so the language comes from
                        `users.language` (migration 0030).

Deriving the request-scoped answer from the header rather than from the stored
column is deliberate, and it is what makes FR-LOC-001a's "taking effect
immediately without reload or re-authentication" true on the server as well as
in the browser. The moment someone switches language, the next request already
carries the new header - whereas a token claim would be stale until reissued,
and a database read would be a query on every error path to answer a question
the caller already knows the answer to.

The two cannot disagree in a way that matters: the client sets the header from
the same value it persists to the column, and a disagreement means a write in
flight, whose worst outcome is one message in the language the user just left.

--- Not `locale` ---

This module answers "which words", never "how are numbers written". That is
`api.i18n.formatting`, driven by the ADMINISTRATION's locale (FR-LOC-002).
The two are separate on purpose - see packages/i18n/src/language.ts, which
makes the same split on the client.
"""

from __future__ import annotations

import enum
import re


class Language(enum.Enum):
    """The languages LEDGR ships. Both first-class (FR-LOC-001); the order
    here is alphabetical by code and is not a ranking.

    Mirrored by the `users_language` CHECK in migration 0030 and by
    SUPPORTED_LANGUAGES in packages/i18n/src/language.ts.
    """

    EN = "en"
    NL = "nl"


#: The codes, in the order a picker lists them - alphabetical by endonym
#: (English, Nederlands), so neither language is presented as the default one.
#: Mirrors SUPPORTED_LANGUAGES in packages/i18n/src/language.ts.
SUPPORTED_LANGUAGE_CODES: tuple[str, ...] = tuple(language.value for language in Language)


#: Where negotiation lands when nothing else answers - a caller that sent no
#: `Accept-Language`, or one asking only for languages LEDGR does not ship.
#:
#: PRD open question Q12 ("Is Dutch or English the default UI language for a
#: firm's staff users?") is unresolved, and this is the answer the product
#: ships until it is: Dutch, matching §20's `.nl` rule and the Dutch market.
#: One constant, so answering Q12 the other way is a one-line change - the
#: same reason DEFAULT_LANGUAGE exists once on the client side.
#:
#: This is NOT a fallback for a missing translation. FR-LOC-001 makes that a
#: release blocker rather than a fallback, and `translate` raises instead.
DEFAULT_LANGUAGE = Language.NL


def parse_language(value: str | None) -> Language | None:
    """A bare language code, or None if it is not one LEDGR ships.

    Used for `users.language` values and for request payloads. Returns None
    rather than the default, so a caller can tell "not set" from "set to
    Dutch" - the difference between seeding an account from a device choice
    and overwriting one.
    """
    if value is None:
        return None
    try:
        return Language(value.strip().lower())
    except ValueError:
        return None


#: One `Accept-Language` entry: a tag, optionally with a quality weight.
#: Deliberately narrow - anything that does not match is skipped rather than
#: guessed at, because a malformed header is a reason to fall through to the
#: default, never a reason to fail a request that was otherwise fine.
_RANGE = re.compile(
    r"^\s*(?P<tag>\*|[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*)\s*"
    r"(?:;\s*q\s*=\s*(?P<q>[01](?:\.\d{0,3})?))?\s*$"
)


def negotiate_language(header: str | None) -> Language:
    """RFC 9110 `Accept-Language`, reduced to the two languages LEDGR ships.

    Matches on the primary subtag, so `nl-BE` and `en-US` both resolve - a
    Flemish accountant's browser should not fall through to the default over
    a region code. `q=0` means "not acceptable" and is honoured: a client that
    says `nl;q=0` is asking not to be given Dutch, and giving it to them
    anyway would be worse than falling through to the default.

    Ties keep header order, which is what makes `nl,en` prefer Dutch without
    either entry carrying an explicit weight.
    """
    if not header:
        return DEFAULT_LANGUAGE

    candidates: list[tuple[float, int, Language]] = []
    for position, part in enumerate(header.split(",")):
        match = _RANGE.match(part)
        if match is None:
            continue

        quality = 1.0 if match["q"] is None else float(match["q"])
        if quality <= 0:
            continue

        tag = match["tag"]
        if tag == "*":
            # "anything you have". Not a preference between our two, so it
            # contributes the default rather than an arbitrary first entry.
            candidates.append((quality, position, DEFAULT_LANGUAGE))
            continue

        language = parse_language(tag.split("-")[0])
        if language is not None:
            candidates.append((quality, position, language))

    if not candidates:
        return DEFAULT_LANGUAGE

    # Highest quality first; header order breaks ties. `position` ascending
    # inside a descending sort is why the key negates the quality rather than
    # reversing the sort.
    candidates.sort(key=lambda entry: (-entry[0], entry[1]))
    return candidates[0][2]


def plural_category(language: Language, count: int | float) -> str:
    """CLDR plural category: `one` or `other`.

    Dutch and English share one rule, so this is a single branch today and is
    still written per-language, for the reason
    packages/i18n/src/language.ts gives: FR-LOC-005 says adding a
    jurisdiction must not fork the codebase, and a language with a `few`
    should add a case here rather than force every counted message to be
    rewritten.

    CLDR's rule for both is `i = 1 and v = 0` - integer part 1, and no VISIBLE
    decimals, so "1,0 regel" is `other`. The second half is not evaluable from
    a number in either runtime: `1.0 == 1` here and `1.0 === 1` in JavaScript.
    So what is implemented is the half that survives - the value is one - and
    it is implemented identically on both sides, which is the property that
    matters when the same counted message is rendered into a screen by one and
    into a PDF by the other.
    """
    if language in (Language.NL, Language.EN):
        return "one" if count == 1 else "other"
    raise ValueError(f"no plural rule for {language}")  # pragma: no cover

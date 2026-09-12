"""Reading the shared message catalogue - FR-LOC-001, FR-LOC-001c, FR-UX-007.

    FR-LOC-001  Every screen, error message, email, push notification, PDF
                template, help article and validation message exists in both.
                A missing translation is a release blocker, not a fallback to
                English.

--- One catalogue, read by two runtimes ---

The files under packages/i18n/catalogue/ are the source of every user-facing
string in the product, for the API and for the clients alike. This module
reads exactly the same directory packages/i18n/src/catalogue.ts imports, and
does not keep a copy.

That is worth the small awkwardness of reaching outside apps/api for data. The
alternative - the API owning its error messages and the web owning its labels
- means the same sentence exists twice, is translated twice, and drifts. An
error a user reads on screen and the same error rendered into a PDF have to be
the same sentence in the same words, and the cheapest way to guarantee that is
for there to be only one of it.

The directory is resolved at import, not at first use, and a missing or
unreadable catalogue is a startup failure. A product whose every user-facing
string is missing should refuse to start rather than serve raw message keys.

--- No fallback between languages ---

`translate` RAISES on a key it cannot resolve, and there is no path from a
missing Dutch string to the English one. FR-LOC-001 rules that out explicitly,
and the reason is not tidiness: a fallback makes a missing translation
invisible everywhere somebody might notice it, so the Dutch product ships with
English sentences scattered through it and nobody has a list of them.

Raising is only safe because scripts/check_translations.py (FR-LOC-001d) fails
the build first. The check is what makes the runtime posture defensible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from api.config import settings
from api.i18n.language import Language, plural_category

#: Namespace files, in load order. Named rather than globbed: a catalogue file
#: that is present on disk and absent from this list would be silently
#: unavailable to the API while the web app served it happily, and a glob would
#: make that failure depend on what happened to be in the directory.
#: scripts/check_translations.py asserts this list matches the directory.
CATALOGUE_FILES = (
    "common.json",
    "auth.json",
    "capture.json",
    "client.json",
    "errors.json",
    "invoice.json",
    "mobile.json",
    "roles.json",
)

GLOSSARY_FILE = "glossary.json"


def _candidate_directories() -> list[Path]:
    """Where the catalogue might be, most specific first.

    Two real locations, and they are not redundant:

      * the packaged copy, `api/i18n/catalogue`, which `uv build` places in
        the wheel via the `force-include` in pyproject.toml. This is what a
        deployed API reads.
      * the checkout, `packages/i18n/catalogue`, five directories up. This is
        what a developer, the test suite and CI read, and it is the one the
        web app reads too - so the two runtimes are demonstrably on the same
        bytes during development.
    """
    here = Path(__file__).resolve()
    candidates = []
    if settings.message_catalogue_dir:
        candidates.append(Path(settings.message_catalogue_dir))
    candidates.append(here.parent / "catalogue")
    candidates.append(here.parents[5] / "packages" / "i18n" / "catalogue")
    return candidates


def catalogue_directory() -> Path:
    for candidate in _candidate_directories():
        if (candidate / GLOSSARY_FILE).is_file():
            return candidate
    raise RuntimeError(
        "no message catalogue found. Looked in: "
        + ", ".join(str(path) for path in _candidate_directories())
        + ". Every user-facing string comes from it (FR-LOC-001), so the API "
        "does not start without one. Set MESSAGE_CATALOGUE_DIR if it lives "
        "somewhere else."
    )


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    """FR-LOC-001c. A statutory term that keeps its Dutch form in the English
    UI, with the hover or tap definition the requirement asks for.
    """

    term: str
    keep_dutch: bool
    definition: dict[str, Any]


class MissingMessageError(KeyError):
    """A key the catalogue does not have, a counted message with no count, or
    a placeholder with no value. All three would otherwise reach a user as a
    raw key, the wrong plural form, or a literal `{query}` on screen - and
    FR-UX-007 is explicit that developer-facing strings never reach a user.
    """


def _load() -> tuple[dict[str, dict[str, Any]], dict[str, GlossaryTerm]]:
    directory = catalogue_directory()

    messages: dict[str, dict[str, Any]] = {}
    for filename in CATALOGUE_FILES:
        document = json.loads((directory / filename).read_text(encoding="utf-8"))
        for key, record in document["messages"].items():
            if key in messages:
                # Two files claiming one key means one of them silently loses,
                # and which one would depend on load order. The namespace rule
                # (every key in client.json starts with `client.`) makes this
                # unreachable; it is asserted rather than assumed.
                raise RuntimeError(f"duplicate message key across catalogue files: {key}")
            messages[key] = record

    glossary_document = json.loads((directory / GLOSSARY_FILE).read_text(encoding="utf-8"))
    glossary = {
        term_id: GlossaryTerm(
            term=entry["term"],
            keep_dutch=bool(entry.get("keepDutch", False)),
            definition=entry["definition"],
        )
        for term_id, entry in glossary_document["terms"].items()
    }

    return messages, glossary


MESSAGES, GLOSSARY = _load()


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def has_message(key: str) -> bool:
    """Whether a key exists, for the rare caller that builds one at runtime."""
    return key in MESSAGES


def translate(key: str, language: Language, /, **params: object) -> str:
    """The message for `key`, in `language`, with `{placeholders}` filled in.

    `language` is positional-only alongside `key` so that a message parameter
    happening to be called `language` cannot shadow it - which is exactly the
    kind of collision a `**params` signature invites.
    """
    record = MESSAGES.get(key)
    if record is None:
        raise MissingMessageError(
            f"no message {key!r} in the catalogue. Add it to "
            f"packages/i18n/catalogue/ in BOTH languages (FR-LOC-001)."
        )

    text = _select(record[language.value], key, language, params)
    return _interpolate(text, key, params)


def _select(text: Any, key: str, language: Language, params: dict[str, object]) -> str:
    if isinstance(text, str):
        return text

    count = params.get("count")
    if not isinstance(count, int | float) or isinstance(count, bool):
        raise MissingMessageError(
            f"{key} is a counted message and needs a numeric `count` parameter; got {count!r}."
        )
    category = plural_category(language, count)
    return str(text[category])


def _interpolate(text: str, key: str, params: dict[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match[1]
        if name not in params:
            raise MissingMessageError(
                f"{key} expects a {{{name}}} parameter and was given none. The "
                f"catalogue keeps the same placeholders in both languages, so this "
                f"is missing in both."
            )
        return str(params[name])

    return _PLACEHOLDER.sub(replace, text)


@cache
def glossary_definition(term_id: str, language: Language) -> str | None:
    """FR-LOC-001c's hover or tap definition, or None for an unknown term.

    None rather than a raise: a glossary lookup is decoration around a term
    that is already legible on its own, so a missing entry should cost the
    tooltip and nothing else.
    """
    entry = GLOSSARY.get(term_id)
    if entry is None:
        return None
    text = entry.definition[language.value]
    return text if isinstance(text, str) else str(text["other"])

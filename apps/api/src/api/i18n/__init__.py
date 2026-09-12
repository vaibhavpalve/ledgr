"""The API's half of LEDGR's bilingual surface - FR-LOC-001 through
FR-LOC-005, FR-UX-007.

Three separate concerns, and keeping them separate is the point:

    language.py    WHICH WORDS a reader gets. Per user (FR-LOC-001b),
                   negotiated per request from `Accept-Language`.
    formatting.py  HOW FIGURES ARE WRITTEN. Per administration (FR-LOC-002),
                   so an amount reads identically to a Dutch bookkeeper and an
                   English-speaking owner looking at the same books.
    catalogue.py   the strings themselves, read from the same
                   packages/i18n/catalogue/ the web and mobile apps read.

The clients mirror this in packages/i18n. Neither side is the source of
truth for the strings - the catalogue is - and neither is the source of truth
for formatting: packages/i18n/formatting-cases.json is, and both
implementations are run against it.
"""

from api.i18n.catalogue import (
    GLOSSARY,
    MESSAGES,
    GlossaryTerm,
    MissingMessageError,
    catalogue_directory,
    glossary_definition,
    has_message,
    translate,
)
from api.i18n.formatting import (
    DEFAULT_FORMATTING_LOCALE,
    LOCALES,
    MONEY_SCALE,
    DateStyle,
    FormattingError,
    LocaleSpec,
    format_date,
    format_money,
    format_number,
    locale_spec,
)
from api.i18n.language import (
    DEFAULT_LANGUAGE,
    Language,
    negotiate_language,
    parse_language,
    plural_category,
)

__all__ = [
    "DEFAULT_FORMATTING_LOCALE",
    "DEFAULT_LANGUAGE",
    "GLOSSARY",
    "LOCALES",
    "MESSAGES",
    "MONEY_SCALE",
    "DateStyle",
    "FormattingError",
    "GlossaryTerm",
    "Language",
    "LocaleSpec",
    "MissingMessageError",
    "catalogue_directory",
    "format_date",
    "format_money",
    "format_number",
    "glossary_definition",
    "has_message",
    "locale_spec",
    "negotiate_language",
    "parse_language",
    "plural_category",
    "translate",
]

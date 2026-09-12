"""Rendering money, numbers and dates into an administration's locale -
FR-LOC-002, and CLAUDE.md rule four.

    FR-LOC-002  Locale-correct number, date and currency formatting; European
                decimal comma. Formatting follows the administration's locale,
                not the user's UI language, so amounts read identically to
                every user.

--- Why this exists twice ---

"Every user" includes a user reading a PDF or an e-mail the API rendered, so
the same amount is formatted by this module and by
packages/i18n/src/format.ts. Two implementations agreeing by coincidence is
not agreement: packages/i18n/formatting-cases.json is the table both are run
against, by tests/i18n/test_formatting.py here and by format.test.ts there.

That is the same device migration 0029 uses for the fiscal period derivation,
which also exists twice - once in SQL and once in Python - and is compared
against tests/ledger/fiscal_cases.py rather than trusted.

--- Why not `locale` / `babel` ---

`locale.setlocale` is process-global and not thread-safe, so a request
formatting a Belgian administration's figures would change what a concurrent
request rendering a Dutch one produced. It also depends on locale data being
installed on the host, which makes output a property of the container image.
Neither is acceptable for a number on a filing.

`babel` would be correct and is a dependency carrying the whole CLDR
database to render one locale, whose output would still have to be pinned
against the TypeScript side case by case - which is the work below, minus the
dependency. See packages/i18n/src/locale.ts for the matching argument against
`Intl` on the client.

--- Money never becomes a float ---

The functions here take `Decimal` or an exact decimal STRING, and take it
apart with string operations. `float` is rejected explicitly rather than
coerced, the same tripwire api.ledger.model puts at its own boundary: a float
that reaches here has already lost the digits, and formatting it would print a
wrong number that every later check agrees with.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

#: The scale of a posted amount: `numeric(19,2)` in migration 0020, `SCALE` in
#: api.ledger.model. FR-GL-010's multi-currency work adds amounts in other
#: currencies beside the functional ones; it does not make the euro's scale
#: vary.
MONEY_SCALE = 2

#: U+00A0. Between the currency symbol and its digits, so that `€` and
#: `1.234,56` cannot land on two lines of an invoice - a break there reads as
#: two separate figures. Named rather than typed inline: the character is
#: invisible in a diff, and it is the difference between the shared cases
#: passing and failing.
NBSP = "\u00a0"

DateStyle = Literal["short", "long", "iso"]


class FormattingError(ValueError):
    """Input that cannot be rendered exactly.

    Every raise in this module is a refusal to round, trim or blank a figure.
    The friendlier alternatives all turn a bug into a plausible-looking number
    on a document somebody reconciles against; an exception is seen, and
    `EUR 0,00` where `€ 1.234,57` belonged is not.
    """


@dataclass(frozen=True, slots=True)
class LocaleSpec:
    """An administration's formatting rules, stated rather than derived.

    Mirrors `LocaleSpec` in packages/i18n/src/locale.ts field for field. A
    date pattern is a template over `yyyy`, `MMMM`, `MM`, `dd` and `d` rather
    than a hard-coded ordering, so a jurisdiction that writes the year first
    (FR-LOC-005) is a string here rather than a branch below.
    """

    code: str
    decimal_separator: str
    group_separator: str
    group_size: int
    currency_symbol: str
    currency_space: str
    currency_symbol_first: bool
    short_date_pattern: str
    long_date_pattern: str
    #: January first. Locale data, not UI text - which is why these are here
    #: and not in the message catalogue. A Dutch administration's dates read
    #: "2 september 2026" to an English-language user, and that is FR-LOC-002
    #: working rather than a missing translation.
    month_names: tuple[str, ...]


NL_NL = LocaleSpec(
    code="nl-NL",
    decimal_separator=",",
    group_separator=".",
    group_size=3,
    currency_symbol="€",
    currency_space=NBSP,
    currency_symbol_first=True,
    short_date_pattern="dd-MM-yyyy",
    long_date_pattern="d MMMM yyyy",
    # Lower case: Dutch month names are not capitalised, standalone or in a
    # sentence.
    month_names=(
        "januari",
        "februari",
        "maart",
        "april",
        "mei",
        "juni",
        "juli",
        "augustus",
        "september",
        "oktober",
        "november",
        "december",
    ),
)

#: The administration locales LEDGR supports. One today: LEDGR serves Dutch
#: SMBs, and a second entry would be a guess about a jurisdiction nobody has
#: specified a chart of accounts, a VAT ruleset or a filing channel for.
#: Mirrored by FORMATTING_LOCALES in packages/i18n/src/locale.ts and by the
#: `administration_formatting_locale` CHECK in migration 0030.
LOCALES: dict[str, LocaleSpec] = {NL_NL.code: NL_NL}

DEFAULT_FORMATTING_LOCALE = NL_NL.code


def locale_spec(locale: str) -> LocaleSpec:
    spec = LOCALES.get(locale)
    if spec is None:
        raise FormattingError(
            f"no formatting locale {locale!r}. Adding a jurisdiction (FR-LOC-005) "
            f"means adding a LocaleSpec here, cases to "
            f"packages/i18n/formatting-cases.json, and a value to the "
            f"administration.formatting_locale CHECK."
        )
    return spec


#: Strictly `-?digits[.digits]`. Everything this rejects is a way a wrong
#: figure reaches a document looking right, and each one is spelled out in
#: packages/i18n/formatting-cases.json: an already-formatted string arriving
#: back from a round trip (`1.234,56`), exponent notation from a float
#: (`1e3`), a padded field, a foreign `+` sign.
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _decimal_text(value: Decimal | str) -> str:
    """The value as an exact, non-exponential decimal string.

    A `Decimal` is already exact, so `format(value, "f")` is a faithful
    rendering of it and `Decimal("1E+3")` legitimately becomes `"1000"`. A
    STRING carrying `1e3` is a different matter and is refused: exponent
    notation in a string is what a float stringifies to, and accepting it
    means accepting whatever produced it (NFR-031).
    """
    if isinstance(value, float):
        # The tripwire, at the one boundary that can still catch it.
        # 4335.09 + 2964.61 is 7299.700000000001 as a float; by the time such
        # a value arrives here the digits are already gone, and rendering it
        # would print a number that every later check agrees with.
        raise FormattingError(
            "a float cannot be formatted as money: it has already lost precision "
            "(CLAUDE.md rule four, NFR-031). Pass a Decimal or an exact decimal "
            "string."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FormattingError(f"not a finite decimal: {value!r}")
        return format(value, "f")
    if not isinstance(value, str) or _DECIMAL.match(value) is None:
        raise FormattingError(
            f"not an exact decimal: {value!r}. Amounts cross the wire as decimal "
            f'strings ("1234.56"), never as JSON numbers - a JSON number is parsed '
            f"into a float before it can be seen here."
        )
    return value


def _split(text: str) -> tuple[bool, str, str]:
    """`-1234.56` -> (True, "1234", "56")."""
    negative = text.startswith("-")
    unsigned = text[1:] if negative else text
    integer_digits, _, fraction_digits = unsigned.partition(".")
    return negative, integer_digits, fraction_digits


def _group(digits: str, spec: LocaleSpec) -> str:
    """`1234567` -> `1.234.567`, from the right, so the first group may be
    short.
    """
    parts: list[str] = []
    end = len(digits)
    while end > 0:
        parts.append(digits[max(0, end - spec.group_size) : end])
        end -= spec.group_size
    return spec.group_separator.join(reversed(parts))


def _render_digits(integer_digits: str, fraction_digits: str, scale: int, spec: LocaleSpec) -> str:
    """Digits and separators, with no sign and no symbol: `1.234,56`.

    `scale` is exact, not a maximum. A value carrying more precision than the
    caller asked to display is refused rather than rounded, because rounding
    here prints a figure that differs from the stored one - and the person
    reconciling against it has no way to see that it was rounded.
    """
    if len(fraction_digits) > scale:
        raise FormattingError(
            f"{integer_digits}.{fraction_digits} carries {len(fraction_digits)} "
            f"decimal places and is being rendered at scale {scale}. Rounding it "
            f"here would display a figure that differs from the stored one; round "
            f"deliberately, upstream, or render at its own scale."
        )
    whole = _group(integer_digits, spec)
    if scale == 0:
        return whole
    return whole + spec.decimal_separator + fraction_digits.ljust(scale, "0")


def format_money(amount: Decimal | str, locale: str = DEFAULT_FORMATTING_LOCALE) -> str:
    """`Decimal("1234.56")` -> `"€ 1.234,56"`.

    The sign sits between the symbol and the digits (`€ -1.234,56`), which is
    CLDR nl-NL's currency pattern `¤ #,##0.00;¤ -#,##0.00`. Writing it the
    English way round gives `-€ 1.234,56`, which a Dutch reader notices.
    """
    spec = locale_spec(locale)
    negative, integer_digits, fraction_digits = _split(_decimal_text(amount))
    digits = _render_digits(integer_digits, fraction_digits, MONEY_SCALE, spec)

    # `-0.00` is not a figure a ledger produces, and an implementation that
    # took the sign from a comparison rather than from the digits could still
    # print one.
    is_zero = set(integer_digits + fraction_digits) <= {"0"}
    signed = ("-" if negative and not is_zero else "") + digits

    if spec.currency_symbol_first:
        return spec.currency_symbol + spec.currency_space + signed
    return signed + spec.currency_space + spec.currency_symbol


def format_number(
    value: Decimal | str, locale: str = DEFAULT_FORMATTING_LOCALE, *, scale: int
) -> str:
    """A bare number: a quantity, a BTW rate, a count of lines. The sign sits
    directly before the digits, because there is no symbol for it to follow.

    `scale` is keyword-only and has no default. A caller that has not decided
    how many decimal places its value has is a caller about to render a rate
    as `21,00` or an amount as `1.234,5`, and asking is cheaper than guessing.
    """
    spec = locale_spec(locale)
    negative, integer_digits, fraction_digits = _split(_decimal_text(value))
    digits = _render_digits(integer_digits, fraction_digits, scale, spec)
    is_zero = set(integer_digits + fraction_digits) <= {"0"}
    return ("-" if negative and not is_zero else "") + digits


def format_date(
    value: date | str, locale: str = DEFAULT_FORMATTING_LOCALE, style: DateStyle = "short"
) -> str:
    """`date(2026, 9, 2)` -> `"02-09-2026"` (short) or `"2 september 2026"`.

    Takes a `date` or an ISO date STRING, never a `datetime`. A posting date,
    an invoice date and a VAT period boundary are calendar dates in the
    administration's own reckoning and have no timezone to be moved by; a
    `datetime` rendered in the reader's zone is how a posting dated 1 January
    shows up in December. `"2026-09-02T00:00:00Z"` is refused for that reason
    and not as pedantry.

    `style="iso"` returns the ISO form, and exists so machine-facing output
    has a named style rather than being the one place a caller skips this
    function. That is `ledger.money_text`'s argument in migration 0023: a
    value some consumer parses must not depend on anybody's locale.
    """
    spec = locale_spec(locale)

    if isinstance(value, date):
        # `datetime` is a subclass of `date`, so this check is the one that
        # keeps a timestamp out - the isinstance above would happily let one
        # through.
        if type(value) is not date:
            raise FormattingError(
                f"a {type(value).__name__} is not a calendar date. A posting or "
                f"filing date carries no time and no timezone; pass a date."
            )
        year, month, day = value.year, value.month, value.day
        iso = value.isoformat()
    else:
        match = _ISO_DATE.match(value) if isinstance(value, str) else None
        if match is None:
            raise FormattingError(
                f"not an ISO 8601 calendar date: {value!r}. Expected YYYY-MM-DD - "
                f"zero-padded, no time, no offset."
            )
        year, month, day = int(match[1]), int(match[2]), int(match[3])
        iso = value

    # Rejects month 13 and 30 February. A lenient parser rolls both forward,
    # which moves a posting into the next period - silently, and by exactly
    # the amount that makes a period's totals wrong rather than obviously
    # broken.
    if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        raise FormattingError(f"no such calendar date: {iso}")

    if style == "iso":
        return iso

    pattern = spec.long_date_pattern if style == "long" else spec.short_date_pattern
    return _render_date_pattern(
        pattern,
        {
            "yyyy": f"{year:04d}",
            "MMMM": spec.month_names[month - 1],
            "MM": f"{month:02d}",
            "dd": f"{day:02d}",
            "d": str(day),
        },
    )


#: Longest token first, so `MMMM` is not consumed as `MM` followed by a
#: literal `MM`, and `dd` is not consumed as two `d`s.
_DATE_TOKENS = ("yyyy", "MMMM", "MM", "dd", "d")


def _render_date_pattern(pattern: str, values: dict[str, str]) -> str:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        for token in _DATE_TOKENS:
            if pattern.startswith(token, index):
                out.append(values[token])
                index += len(token)
                break
        else:
            out.append(pattern[index])
            index += 1
    return "".join(out)

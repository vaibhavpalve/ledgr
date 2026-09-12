"""The Python half of FR-LOC-002's cross-implementation check.

Every case here comes from packages/i18n/formatting-cases.json, which
packages/i18n/src/format.test.ts runs against the TypeScript implementation. A
case added there is a case this must satisfy, and a divergence between an
amount on a screen and the same amount on the PDF of it fails both suites
rather than neither.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from api.i18n.formatting import (
    LOCALES,
    NBSP,
    FormattingError,
    format_date,
    format_money,
    format_number,
)
from api.i18n.language import Language, plural_category
from tests.i18n import cases


def test_the_shared_table_actually_has_cases() -> None:
    """The idiom tests/test_audit_coverage.py uses for its own sweep: a
    table-driven suite that finds no rows passes completely and guarantees
    nothing, which is worse than no suite because it reads as a guarantee.
    """
    assert cases.MONEY
    assert cases.NUMBERS
    assert cases.DATES
    assert cases.PLURALS
    assert cases.REJECTED_MONEY
    assert cases.REJECTED_DATES


@pytest.mark.parametrize("case", cases.MONEY, ids=lambda c: f"{c['locale']}:{c['amount']}")
def test_money_matches_the_shared_table(case: dict[str, str]) -> None:
    assert format_money(case["amount"], case["locale"]) == case["expected"]


@pytest.mark.parametrize("case", cases.MONEY, ids=lambda c: f"{c['locale']}:{c['amount']}")
def test_money_from_a_decimal_matches_the_same_case(case: dict[str, str]) -> None:
    """`Decimal` is what actually arrives - asyncpg returns `numeric` as one.
    The table is written in strings so the TypeScript side can share it, so
    this runs every case again through the type the API really handles.
    """
    assert format_money(Decimal(case["amount"]), case["locale"]) == case["expected"]


@pytest.mark.parametrize("case", cases.REJECTED_MONEY, ids=lambda c: repr(c["value"]))
def test_money_refuses_what_the_shared_table_refuses(case: dict[str, str]) -> None:
    with pytest.raises(FormattingError):
        format_money(case["value"], "nl-NL")


@pytest.mark.parametrize(
    "case", cases.NUMBERS, ids=lambda c: f"{c['locale']}:{c['value']}@{c['scale']}"
)
def test_numbers_match_the_shared_table(case: dict[str, object]) -> None:
    assert (
        format_number(str(case["value"]), str(case["locale"]), scale=int(str(case["scale"])))
        == case["expected"]
    )


@pytest.mark.parametrize(
    "case", cases.DATES, ids=lambda c: f"{c['locale']}:{c['date']}:{c['style']}"
)
def test_dates_match_the_shared_table(case: dict[str, str]) -> None:
    assert format_date(case["date"], case["locale"], case["style"]) == case["expected"]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "case", cases.DATES, ids=lambda c: f"{c['locale']}:{c['date']}:{c['style']}"
)
def test_dates_from_a_date_object_match_the_same_case(case: dict[str, str]) -> None:
    assert (
        format_date(date.fromisoformat(case["date"]), case["locale"], case["style"])  # type: ignore[arg-type]
        == case["expected"]
    )


@pytest.mark.parametrize("case", cases.REJECTED_DATES, ids=lambda c: repr(c["value"]))
def test_dates_refuse_what_the_shared_table_refuses(case: dict[str, str]) -> None:
    with pytest.raises(FormattingError):
        format_date(case["value"], "nl-NL", "short")


@pytest.mark.parametrize("case", cases.PLURALS, ids=lambda c: f"{c['language']}:{c['count']}")
def test_plural_categories_match_the_shared_table(case: dict[str, object]) -> None:
    language = Language(str(case["language"]))
    assert plural_category(language, int(str(case["count"]))) == case["expected"]


# ---------------------------------------------------------------------------
# The properties the table demonstrates, asserted directly
# ---------------------------------------------------------------------------


def test_a_float_is_refused_rather_than_coerced() -> None:
    """CLAUDE.md rule four at the last boundary that can still catch it.

    4335.09 + 2964.61 is 7299.700000000001 as a float. Accepting it here would
    print a number that is not the number in the ledger, and every later check
    would agree with it.
    """
    lost = 4335.09 + 2964.61
    assert lost != 7299.70

    with pytest.raises(FormattingError, match="float"):
        format_money(lost, "nl-NL")  # type: ignore[arg-type]
    with pytest.raises(FormattingError, match="float"):
        format_number(1234.5, "nl-NL", scale=2)  # type: ignore[arg-type]

    # The exact sum, as a Decimal, renders as the figure a person expects.
    assert format_money(Decimal("4335.09") + Decimal("2964.61"), "nl-NL") == "€\u00a07.299,70"


def test_an_amount_beyond_a_float_survives_intact() -> None:
    """numeric(19,2) reaches 17 integer digits; a double is exact only to
    about 15. An implementation that routed through a float would round this
    four digits from the end and look entirely plausible doing it.
    """
    exact = "12345678901234567.89"
    assert str(float(exact)) != exact
    assert format_money(Decimal(exact), "nl-NL") == "€\u00a012.345.678.901.234.567,89"


def test_the_currency_space_is_non_breaking() -> None:
    """A U+0020 here lets `€` and `1.234,56` land on two lines of an invoice,
    where they read as two figures. The character is invisible in a diff, so
    it is asserted rather than trusted to the shared table's literal.
    """
    assert NBSP == "\u00a0"
    assert format_money("1234.56", "nl-NL") == "€\u00a01.234,56"


def test_rounding_is_refused_rather_than_performed() -> None:
    """The friendly alternative is to round to the requested scale. It is
    refused because 0,22 on screen where 0.215 is stored is a figure somebody
    reconciles against and cannot make agree.
    """
    with pytest.raises(FormattingError, match="decimal places"):
        format_number("0.215", "nl-NL", scale=2)
    with pytest.raises(FormattingError, match="decimal places"):
        format_money("1234.567", "nl-NL")


def test_a_datetime_is_not_a_calendar_date() -> None:
    """A posting date has no time and no timezone. `datetime` subclasses
    `date`, so an isinstance check alone would let one through and render it
    in whatever zone it happened to carry - which is how a posting dated 1
    January appears in December.
    """
    with pytest.raises(FormattingError, match="calendar date"):
        format_date(datetime(2026, 9, 2, 10, 30), "nl-NL")  # type: ignore[arg-type]
    assert format_date(date(2026, 9, 2), "nl-NL") == "02-09-2026"


def test_an_impossible_day_is_refused_rather_than_rolled_forward() -> None:
    with pytest.raises(FormattingError, match="no such calendar date"):
        format_date("2026-02-30", "nl-NL")
    assert format_date("2024-02-29", "nl-NL") == "29-02-2024"


def test_an_unknown_locale_names_what_adding_one_involves() -> None:
    with pytest.raises(FormattingError, match="FR-LOC-005"):
        format_money("1.00", "de-DE")


def test_formatting_does_not_depend_on_the_readers_language() -> None:
    """FR-LOC-002, as a property of the signature rather than of discipline.

    None of these functions takes a `Language`. A caller therefore CANNOT make
    an amount depend on who is reading it, which is what stops an
    English-speaking owner reading "1,234.56" off a screen a Dutch bookkeeper
    reads as "1.234,56" - and the two of them agreeing on the phone that the
    number matches.
    """
    import inspect

    for function in (format_money, format_number, format_date):
        annotations = {
            parameter.annotation for parameter in inspect.signature(function).parameters.values()
        }
        assert Language not in annotations
        assert "Language" not in {str(annotation) for annotation in annotations}


def test_the_locale_table_matches_the_typescript_one() -> None:
    """Both sides ship exactly the locales the other does. A locale present
    here and missing there is an administration whose figures the web app
    cannot render; the reverse is one the API cannot put on a filing.
    """
    import json

    locale_source = (cases.CASES_FILE.parent / "src" / "locale.ts").read_text(encoding="utf-8")

    # The declaration is `export const FORMATTING_LOCALES = [...] as const;`
    start = locale_source.index("FORMATTING_LOCALES = [")
    end = locale_source.index("]", start) + 1
    declared = json.loads(locale_source[locale_source.index("[", start) : end].replace("'", '"'))

    assert sorted(declared) == sorted(LOCALES)

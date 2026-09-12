"""FR-EXP-001g's matching rule, as one table run against both implementations.

`api.expenses.duplicates.normalise_supplier` and migration 0036's
`expenses.normalise_supplier` perform the same three steps in the same order,
and cannot borrow each other's answer: the form has to decide before anything
is stored, and the index has to be built on the stored form.
tests/expenses/test_duplicates.py runs these against the Python;
tests/integration/test_duplicate_detection.py runs them against the SQL.

The device is 0029's (fiscal periods), 0031's (retention) and 0033's (VAT), for
the same reason: two implementations of one rule agreeing by inspection is not
agreement.

SIMILARITY is deliberately absent from this table. It belongs to pg_trgm alone
- reimplementing it in Python to the same answer is a promise that breaks the
first time Postgres tunes it - so the SQL suite tests it on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

#: (raw, normalised). The whole normalisation contract.
NORMALISATION_CASES: tuple[tuple[str, str, str], ...] = (
    ("Albert Heijn", "albert heijn", "the ordinary case: casefolded"),
    ("ALBERT HEIJN", "albert heijn", "shouted on a receipt, typed normally"),
    ("  Albert Heijn  ", "albert heijn", "leading and trailing space"),
    ("Albert  Heijn", "albert heijn", "a double space between words"),
    ("Albert\tHeijn", "albert heijn", "a tab, which a paste can carry in"),
    ("Albert Heijn.", "albert heijn", "a trailing full stop"),
    ('"Albert Heijn"', "albert heijn", "quotes around the whole name"),
    ("(Albert Heijn)", "albert heijn", "brackets around the whole name"),
    (
        "Jan's Cafe",
        "jan's cafe",
        "punctuation INSIDE the name survives: removing it would eventually "
        "merge two real suppliers whose names differ by a hyphen",
    ),
    (
        "Café Zonneschijn",
        "café zonneschijn",
        "an accent is part of the name, not noise",
    ),
    ("NS", "ns", "a two-letter supplier still normalises"),
    ("", "", "an empty name normalises to empty rather than failing"),
    ("   ", "", "whitespace only"),
    ("...", "", "punctuation only leaves nothing"),
)


@dataclass(frozen=True, slots=True)
class MatchCase:
    left_supplier: str
    right_supplier: str
    same_date: bool
    same_amount: bool
    matches: bool
    why: str


#: FR-EXP-001g read literally: the same supplier, date AND amount.
MATCH_CASES: tuple[MatchCase, ...] = (
    MatchCase(
        "Albert Heijn",
        "albert heijn",
        True,
        True,
        True,
        "the same claim entered twice, typed differently",
    ),
    MatchCase(
        "Albert Heijn",
        "  ALBERT  HEIJN ",
        True,
        True,
        True,
        "every normalisation rule at once",
    ),
    MatchCase(
        "Albert Heijn",
        "Albert Heijn",
        False,
        True,
        False,
        "a different day is a different purchase - two coffees on two mornings",
    ),
    MatchCase(
        "Albert Heijn",
        "Albert Heijn",
        True,
        False,
        False,
        "a different amount is a different purchase",
    ),
    MatchCase(
        "Albert Heijn",
        "Jumbo",
        True,
        True,
        False,
        "same day, same amount, different shop. This is the case that would "
        "make a date-and-amount-only rule useless: two people buying lunch for "
        "the same price on the same day is unremarkable",
    ),
    MatchCase(
        "Albert Heijn 1043",
        "Albert Heijn",
        True,
        True,
        False,
        "NOT an exact match - the names differ. pg_trgm reports this as "
        "PROBABLE, which is the SQL suite's business",
    ),
    MatchCase(
        "Jan's Cafe",
        "Jans Cafe",
        True,
        True,
        False,
        "an apostrophe is not stripped, so these are different suppliers here. "
        "Similarity is what catches them, if anything does",
    ),
)

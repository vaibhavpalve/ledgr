"""FR-DOC-002's rule, as one table run against both implementations.

`api.documents.retention.retention_until` and migration 0031's
`documents.retention_until()` compute the same thing and cannot borrow each
other's answer: a screen has to show a retention date before the document
exists, and the database has to derive one inside the transaction that creates
it. tests/documents/test_retention.py runs this table against the Python;
tests/integration/test_document_archive.py runs it against the SQL.

That is the device tests/ledger/fiscal_cases.py uses for period derivation, and
for the same reason - two implementations of one rule agreeing by inspection is
not agreement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class RetentionCase:
    fiscal_year_end: date
    basis: str
    expected: date
    why: str


RETENTION_CASES: tuple[RetentionCase, ...] = (
    RetentionCase(
        date(2025, 12, 31),
        "standard",
        date(2032, 12, 31),
        "the ordinary case: a calendar year, kept seven years past its end",
    ),
    RetentionCase(
        date(2025, 12, 31),
        "immovable_property",
        date(2035, 12, 31),
        "ten years where immovable property is involved",
    ),
    RetentionCase(
        date(2026, 6, 30),
        "standard",
        date(2033, 6, 30),
        "a July-June fiscal year (FR-ONB-006): retention follows the year's own "
        "end, not the calendar's",
    ),
    RetentionCase(
        date(2026, 3, 15),
        "standard",
        date(2033, 3, 15),
        "a short first year ending mid-month",
    ),
    RetentionCase(
        date(2024, 2, 29),
        "standard",
        date(2031, 2, 28),
        "a leap day plus seven years is not a date. Postgres clamps to the last "
        "day of the month and so must Python - the one case where a naive "
        "`year + 7` raises instead of answering",
    ),
    RetentionCase(
        date(2024, 2, 29),
        "immovable_property",
        date(2034, 2, 28),
        "the same clamp on the ten-year basis, where 2034 is also not a leap year",
    ),
    RetentionCase(
        date(2020, 2, 29),
        "standard",
        date(2027, 2, 28),
        "2027 is not a leap year either",
    ),
    RetentionCase(
        date(2028, 2, 29),
        "immovable_property",
        date(2038, 2, 28),
        "ten years from a leap day",
    ),
    RetentionCase(
        date(2025, 1, 1),
        "standard",
        date(2032, 1, 1),
        "a year ending on the first of a month, so nothing about the clamp is "
        "exercised and the plain answer has to still be right",
    ),
    RetentionCase(
        date(2016, 2, 29),
        "immovable_property",
        date(2026, 2, 28),
        "a document already inside its final year, which is what makes the "
        "expiry sweep's boundary reachable in a test",
    ),
)

#: The boundary FR-DOC-002 turns on, kept separate because it is about
#: `is_expired` rather than about the date arithmetic. `retention_until` is
#: INCLUSIVE - the document is held THROUGH that date - so it is collectable
#: only the day after. Migration 0031's deletion guard reads it the same way
#: (`retention_until < current_date`), and the pair is asserted rather than
#: assumed because the disagreement would be exactly one day: the last day of
#: the seventh year, which is the day a deadline-dated inspection asks for.
EXPIRY_CASES: tuple[tuple[date, date, bool, str], ...] = (
    (date(2032, 12, 30), date(2032, 12, 31), False, "the day before expiry"),
    (date(2032, 12, 31), date(2032, 12, 31), False, "ON the retention date, still held"),
    (date(2033, 1, 1), date(2032, 12, 31), True, "the day after, collectable"),
)

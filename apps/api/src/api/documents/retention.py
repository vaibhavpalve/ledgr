"""How long a document is kept - FR-DOC-002.

    FR-DOC-002  Retention of 7 years from the end of the fiscal year (10 years
                where immovable property is involved), enforced by policy and
                not deletable by users.

--- Anchored to the fiscal year, not the upload ---

The anchor is the FISCAL YEAR's end, and that is the whole substance of the
rule. A receipt uploaded in March 2026 against fiscal year 2025 is kept until
the end of 2032, not 2033; and a receipt uploaded on the first day of a fiscal
year and one uploaded on its last are kept to the same date. Anchoring to the
upload instead would give two documents from one year two different expiries,
which is not a retention policy so much as a queue.

--- Why this exists twice ---

`documents.retention_until()` in migration 0031 computes the same thing, and
the trigger there is what makes the stored value true for every writer. This
implementation exists because a screen has to be able to say how long a
document will be kept BEFORE it is uploaded, and the database cannot answer
that about a row it does not have.

Neither can borrow the other's answer, so tests/documents/retention_cases.py
runs one table against both - the device 0029 uses for fiscal period
derivation, and for the same reason: two implementations of one rule agreeing
by inspection is not agreement.

--- The 10-year case is not implemented as a guess ---

`IMMOVABLE_PROPERTY` is applied when a caller says so, never inferred. Whether
a document relates to immovable property is an accounting judgement about the
transaction behind it - it is the reason the Dutch revision period for
onroerende zaken runs to ten years - and nothing in a file's bytes settles it.
A system that guessed would guess short, which is the direction that loses
records.
"""

from __future__ import annotations

import calendar
import enum
from datetime import date


class RetentionBasis(enum.Enum):
    """Mirrors the `retention_basis` CHECK in migration 0031."""

    STANDARD = "standard"
    IMMOVABLE_PROPERTY = "immovable_property"

    @property
    def years(self) -> int:
        return 10 if self is RetentionBasis.IMMOVABLE_PROPERTY else 7


def add_years(anchor: date, years: int) -> date:
    """`anchor` shifted by whole years, clamped to the target month's length.

    29 February is the case this exists for: a fiscal year may end on one
    (FR-ONB-006 permits non-calendar years), and 2024-02-29 plus seven years is
    not a date. Postgres clamps `date + interval 'n years'` to 2031-02-28, so
    this does too - the shared case table asserts the two agree rather than
    trusting that they do.
    """
    year = anchor.year + years
    day = min(anchor.day, calendar.monthrange(year, anchor.month)[1])
    return date(year, anchor.month, day)


def retention_until(fiscal_year_end: date, basis: RetentionBasis = RetentionBasis.STANDARD) -> date:
    """The last date a document for this fiscal year must still be held.

    INCLUSIVE: the document is retained THROUGH this date and may be removed
    after it. Migration 0031's deletion guard reads it the same way
    (`retention_until <= current_date` permits removal), and the two must agree
    or the archive expires a day early or a day late - the early direction
    losing exactly the last day of the seventh year, which is the day a
    deadline-dated inspection asks for.
    """
    if not isinstance(basis, RetentionBasis):  # pragma: no cover - typing guard
        raise TypeError(f"basis must be a RetentionBasis, got {basis!r}")
    return add_years(fiscal_year_end, basis.years)


def is_expired(fiscal_year_end: date, today: date, basis: RetentionBasis) -> bool:
    """Whether retention has run out as of `today`.

    `today` is a parameter rather than a call to `date.today()`: an expiry rule
    that reads the clock cannot be tested at a boundary, and the boundary is
    the only interesting part of it.
    """
    return today > retention_until(fiscal_year_end, basis)

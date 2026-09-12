"""FR-DOC-002 against the Python implementation.

Runs tests/documents/retention_cases.py - the same table
tests/integration/test_document_archive.py runs against migration 0031's SQL -
plus the properties that have to hold for anchors nobody wrote a case for.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.documents.retention import RetentionBasis, add_years, is_expired, retention_until
from tests.documents.retention_cases import EXPIRY_CASES, RETENTION_CASES, RetentionCase

SETTINGS = settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Wide enough to cross leap years in both directions and to include years
#: whose +7 and +10 land on non-leap years.
ANCHORS = st.dates(min_value=date(1996, 1, 1), max_value=date(2040, 12, 31))
BASES = st.sampled_from(list(RetentionBasis))


def test_the_case_table_is_not_empty() -> None:
    """A table-driven suite that finds no rows passes completely and
    guarantees nothing - the trap tests/test_audit_coverage.py names.
    """
    assert len(RETENTION_CASES) >= 10
    assert EXPIRY_CASES


@pytest.mark.parametrize("case", RETENTION_CASES, ids=lambda c: f"{c.fiscal_year_end}:{c.basis}")
def test_retention_matches_the_shared_table(case: RetentionCase) -> None:
    assert retention_until(case.fiscal_year_end, RetentionBasis(case.basis)) == case.expected, (
        case.why
    )


@pytest.mark.parametrize(("today", "until", "expired", "why"), EXPIRY_CASES, ids=lambda v: str(v))
def test_the_expiry_boundary_is_inclusive(
    today: date, until: date, expired: bool, why: str
) -> None:
    """`retention_until` is the last day the document is HELD.

    Off by one here is off by one in the direction that loses records, and the
    day it loses is the last day of the seventh year.
    """
    # The anchor that produces `until` under the standard basis.
    anchor = date(until.year - 7, until.month, until.day)
    assert is_expired(anchor, today, RetentionBasis.STANDARD) is expired, why


def test_immovable_property_is_three_years_longer() -> None:
    """FR-DOC-002's parenthesis, as the only difference between the two bases."""
    year_end = date(2025, 12, 31)
    standard = retention_until(year_end, RetentionBasis.STANDARD)
    immovable = retention_until(year_end, RetentionBasis.IMMOVABLE_PROPERTY)

    assert immovable.year - standard.year == 3
    assert (immovable.month, immovable.day) == (standard.month, standard.day)


def test_the_basis_carries_its_own_period() -> None:
    # Stated on the enum rather than in a conditional at each call site, so
    # "how long is immovable property kept" has one answer.
    assert RetentionBasis.STANDARD.years == 7
    assert RetentionBasis.IMMOVABLE_PROPERTY.years == 10


def test_a_bare_string_basis_is_refused() -> None:
    """`retention_until(end, "standard")` would silently take the `.years` of
    nothing if it were duck-typed. The refusal is explicit because the failure
    would otherwise be a retention period computed from an AttributeError's
    absence.
    """
    with pytest.raises(TypeError, match="RetentionBasis"):
        retention_until(date(2025, 12, 31), "standard")  # type: ignore[arg-type]


# ===========================================================================
# Properties, for the anchors nobody wrote a case for
# ===========================================================================


@given(anchor=ANCHORS, basis=BASES)
@SETTINGS
def test_retention_is_always_after_the_year_it_anchors_to(
    anchor: date, basis: RetentionBasis
) -> None:
    """The property FR-DOC-002 rests on: a document is never already expired
    at the moment its fiscal year closes.
    """
    assert retention_until(anchor, basis) > anchor


@given(anchor=ANCHORS, basis=BASES)
@SETTINGS
def test_retention_lands_in_the_right_year_and_month(anchor: date, basis: RetentionBasis) -> None:
    """Whole years, so only the DAY may move - and only ever backwards, by the
    leap-day clamp. A month that shifted would mean the arithmetic had drifted
    into month-counting.
    """
    result = retention_until(anchor, basis)

    assert result.year == anchor.year + basis.years
    assert result.month == anchor.month
    assert result.day <= anchor.day


@given(anchor=ANCHORS)
@SETTINGS
def test_only_a_leap_day_ever_moves(anchor: date) -> None:
    """The clamp is not a general rounding rule. 29 February is the only date
    whose day can change, because it is the only one that can fail to exist in
    the target year.
    """
    result = retention_until(anchor, RetentionBasis.STANDARD)
    if (anchor.month, anchor.day) != (2, 29):
        assert result.day == anchor.day


@given(anchor=ANCHORS)
@SETTINGS
def test_immovable_property_never_expires_before_standard(anchor: date) -> None:
    """Reclassifying a document as immovable property may only EXTEND its
    retention - which is what migration 0031's trigger enforces by refusing to
    let `retention_until` move backwards. If the arithmetic could ever produce
    a shorter ten-year answer than its seven-year one, that trigger would be
    refusing a legitimate reclassification.
    """
    assert retention_until(anchor, RetentionBasis.IMMOVABLE_PROPERTY) > retention_until(
        anchor, RetentionBasis.STANDARD
    )


@given(anchor=ANCHORS, basis=BASES)
@SETTINGS
def test_a_document_is_held_through_its_retention_date(anchor: date, basis: RetentionBasis) -> None:
    """The boundary, over every anchor rather than the three in the table."""
    until = retention_until(anchor, basis)

    assert not is_expired(anchor, until, basis), "held ON the retention date"
    assert not is_expired(anchor, until - timedelta(days=1), basis)
    assert is_expired(anchor, until + timedelta(days=1), basis), "collectable the day after"


@given(anchor=ANCHORS, years=st.integers(min_value=0, max_value=40))
@SETTINGS
def test_add_years_is_monotonic(anchor: date, years: int) -> None:
    """More years is never an earlier date. Obvious, and the leap-day clamp is
    exactly the kind of special case that breaks obvious properties.
    """
    assert add_years(anchor, years) <= add_years(anchor, years + 1)

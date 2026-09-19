"""SI-08's rules - api.invoicing.quotes (ADR-075)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.invoicing.quotes import (
    MAX_LINES,
    TRANSITIONS,
    QuoteInvalid,
    QuoteLine,
    QuoteStatus,
    can_transition,
    is_expired,
    net_total,
    validate_quote,
)

D = Decimal
TODAY = date(2026, 9, 19)


def line(**kw: object) -> QuoteLine:
    defaults: dict[str, object] = dict(
        description="Advies", quantity=D("1"), unit_price=D("100"), vat_treatment="btw_21"
    )
    defaults.update(kw)
    return QuoteLine(**defaults)  # type: ignore[arg-type]


# --- the lifecycle -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "allowed"),
    [
        (QuoteStatus.DRAFT, {"sent", "accepted", "cancelled"}),
        (QuoteStatus.SENT, {"accepted", "declined", "cancelled"}),
        (QuoteStatus.ACCEPTED, {"converted", "cancelled"}),
        (QuoteStatus.DECLINED, set()),
        (QuoteStatus.CANCELLED, set()),
        (QuoteStatus.CONVERTED, set()),
    ],
)
def test_each_status_can_move_only_where_the_lifecycle_says(
    current: QuoteStatus, allowed: set[str]
) -> None:
    assert {s.value for s in TRANSITIONS[current]} == allowed


def test_a_quote_never_moves_backwards() -> None:
    order = [QuoteStatus.DRAFT, QuoteStatus.SENT, QuoteStatus.ACCEPTED, QuoteStatus.CONVERTED]
    for later_index, later in enumerate(order):
        for earlier in order[:later_index]:
            assert not can_transition(later, earlier)


def test_a_draft_cannot_be_converted_and_an_accepted_quote_cannot_be_declined() -> None:
    assert not can_transition(QuoteStatus.DRAFT, QuoteStatus.CONVERTED)
    assert not can_transition(QuoteStatus.SENT, QuoteStatus.CONVERTED)
    assert not can_transition(QuoteStatus.ACCEPTED, QuoteStatus.DECLINED)


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (QuoteStatus.DRAFT, False),
        (QuoteStatus.SENT, False),
        (QuoteStatus.ACCEPTED, False),
        (QuoteStatus.DECLINED, True),
        (QuoteStatus.CANCELLED, True),
        (QuoteStatus.CONVERTED, True),
    ],
)
def test_terminal_statuses(status: QuoteStatus, terminal: bool) -> None:
    assert status.is_terminal is terminal


def test_every_terminal_status_has_no_way_out() -> None:
    for status in QuoteStatus:
        assert status.is_terminal == (not TRANSITIONS[status])


# --- expiry ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valid_until", "expired"),
    [
        (None, False),  # does not expire
        (date(2026, 9, 20), False),
        (TODAY, False),  # the last day is still valid
        (date(2026, 9, 18), True),
    ],
)
def test_expiry(valid_until: date | None, expired: bool) -> None:
    assert is_expired(valid_until, TODAY) is expired


# --- the net total -------------------------------------------------------------------------------


def test_the_net_total_is_the_sum_of_lines_each_rounded_as_the_invoice_will() -> None:
    # 2 x 49.95 less 10% = 89.91 ; 3 x 0.035 = 0.105 -> 0.11 (half up) ; sum 90.02
    lines = [
        line(quantity=D("2"), unit_price=D("49.95"), discount_percent=D("10")),
        line(quantity=D("3"), unit_price=D("0.035")),
    ]
    assert net_total(lines) == D("90.02")


def test_rounding_is_per_line_not_on_the_sum() -> None:
    """Three lines of 0.005 each round to 0.01 individually (0.03), not to 0.02 as
    their unrounded sum (0.015) would - the invoice prints each line rounded."""
    lines = [line(quantity=D("1"), unit_price=D("0.005")) for _ in range(3)]
    assert net_total(lines) == D("0.03")


def test_an_empty_total_is_zero_with_two_places() -> None:
    assert net_total([]) == D("0.00")


# --- validation -----------------------------------------------------------------------------------


def check(**overrides: object) -> None:
    args: dict[str, object] = dict(lines=[line()], valid_until=None, today=TODAY)
    args.update(overrides)
    validate_quote(**args)  # type: ignore[arg-type]


def test_a_sound_quote_is_accepted() -> None:
    check()
    check(valid_until=TODAY)  # valid through today
    check(lines=[line(unit_price=D("-10"))])  # a standing discount line


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"lines": []}, "at least one line"),
        ({"lines": [line() for _ in range(MAX_LINES + 1)]}, "at most"),
        ({"valid_until": date(2026, 9, 18)}, "in the past"),
        ({"lines": [line(description="  ")]}, "description"),
        ({"lines": [line(quantity=D("0"))]}, "quantity"),
        ({"lines": [line(discount_percent=D("101"))]}, "discount"),
        ({"lines": [line(discount_percent=D("-1"))]}, "discount"),
        ({"lines": [line(vat_treatment=" ")]}, "VAT treatment"),
    ],
)
def test_an_unusable_quote_is_refused(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(QuoteInvalid, match=match):
        check(**overrides)


def test_a_float_is_refused() -> None:
    with pytest.raises(QuoteInvalid, match="floats"):
        check(lines=[line(unit_price=10.0)])  # type: ignore[arg-type]

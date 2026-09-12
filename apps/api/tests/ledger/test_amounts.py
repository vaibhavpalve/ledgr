"""NFR-031 at the boundary: what an amount is allowed to be.

    NFR-031  Monetary values use decimal types with defined scale. Floating
             point is prohibited in any financial calculation path.

`amount_to_string` is the one funnel every posted amount passes through on
its way to the database, so it is where "prohibited" has to be enforced -
after this point the value is a string in a jsonb payload and a float has
already done its damage.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from api.ledger import InvalidAmount, LineInput, amount_to_string

ACCOUNT = uuid.uuid4()


def test_a_float_is_refused() -> None:
    """The requirement, literally.

    Not coerced with Decimal(str(value)), which would look helpful and be
    wrong: by the time a float exists the precision is already gone, and
    silently accepting it makes the ledger disagree with the invoice that
    produced the number.
    """
    with pytest.raises(InvalidAmount) as raised:
        amount_to_string(10.0)  # type: ignore[arg-type]

    assert "NFR-031" in str(raised.value)


def test_the_float_that_actually_goes_wrong_is_refused() -> None:
    """A concrete pair, not a symbolic one.

    4335.09 + 2964.61 is 7299.700000000001 in binary floating point. Both
    inputs look exact and the result is off by a hundred-billionth of a euro -
    small enough to survive review, large enough to make a trial balance fail
    to net to zero.
    """
    wrong = 4335.09 + 2964.61
    assert wrong != 7299.70, "the premise of this test no longer holds"

    with pytest.raises(InvalidAmount):
        amount_to_string(wrong)  # type: ignore[arg-type]

    assert amount_to_string(Decimal("4335.09") + Decimal("2964.61")) == "7299.70"


def test_an_int_is_refused() -> None:
    """Not because an int is imprecise - it is exact - but because 10 and
    Decimal('10.00') are different claims about scale, and accepting both
    means the boundary no longer has one rule.
    """
    with pytest.raises(InvalidAmount):
        amount_to_string(10)  # type: ignore[arg-type]


def test_a_bool_is_refused_as_a_bool() -> None:
    """bool subclasses int, so a naive isinstance check on int lets True
    through as 1. The error should name the actual mistake.
    """
    with pytest.raises(InvalidAmount) as raised:
        amount_to_string(True)  # type: ignore[arg-type]

    assert "boolean" in str(raised.value)


def test_a_negative_amount_is_refused() -> None:
    """Direction is carried by which side the amount is on, not by its sign.

    A negative debit is the same posting as a positive credit, and allowing
    both means every report has to handle two spellings of one fact - and that
    the one-sided CHECK constraint stops meaning what it says.
    """
    with pytest.raises(InvalidAmount) as raised:
        amount_to_string(Decimal("-1.00"))

    assert "unsigned" in str(raised.value)


def test_more_than_two_decimals_is_refused_not_rounded() -> None:
    """Rejected rather than rounded, deliberately.

    A posted amount is already settled. Rounding it here would make the ledger
    disagree with the invoice that produced it, and *which way* to round is a
    decision for the calculation that made the number - VAT has its own rule -
    not for the boundary that stores it.
    """
    with pytest.raises(InvalidAmount) as raised:
        amount_to_string(Decimal("10.005"))

    assert "scale 2" in str(raised.value)


def test_trailing_zeros_beyond_the_scale_are_accepted() -> None:
    """Decimal('10.000') has an exponent of -3 but is exactly ten.

    A check written as `exponent < -2` would reject it, which would be an
    annoying and arbitrary refusal of a legitimate amount. The check is on the
    VALUE being representable, not on how it was spelled.
    """
    assert amount_to_string(Decimal("10.000")) == "10.00"
    assert amount_to_string(Decimal("1E+2")) == "100.00"


def test_zero_is_a_valid_amount() -> None:
    """It has to be: every line carries a zero on the side it is not using."""
    assert amount_to_string(Decimal("0")) == "0.00"


def test_not_a_number_is_refused() -> None:
    with pytest.raises(InvalidAmount):
        amount_to_string(Decimal("NaN"))
    with pytest.raises(InvalidAmount):
        amount_to_string(Decimal("Infinity"))


def test_a_line_carries_an_amount_on_exactly_one_side() -> None:
    with pytest.raises(InvalidAmount) as both:
        LineInput(account_id=ACCOUNT, debit=Decimal("1.00"), credit=Decimal("1.00"))
    assert "both sides" in str(both.value)

    with pytest.raises(InvalidAmount) as neither:
        LineInput(account_id=ACCOUNT)
    assert "neither side" in str(neither.value)


def test_a_line_rejects_a_float_at_construction() -> None:
    """The check runs in __post_init__, so a bad amount cannot be held in a
    LineInput at all - there is no window where an invalid line exists and is
    caught later.
    """
    with pytest.raises(InvalidAmount):
        LineInput(account_id=ACCOUNT, debit=1.0)  # type: ignore[arg-type]


def test_the_payload_renders_amounts_as_strings() -> None:
    """ledger.post_entry rejects a JSON number outright (NFR-031). This is the
    side that has to produce a string for it, and json.dumps of a Decimal
    raises - so a regression here surfaces as a serialisation error rather
    than as a silently accepted float.
    """
    payload = LineInput(account_id=ACCOUNT, debit=Decimal("121.5")).as_payload()

    assert payload["debit"] == "121.50"
    assert payload["credit"] == "0.00"
    assert isinstance(payload["debit"], str)

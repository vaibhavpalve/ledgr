"""SI-08: quotes and order confirmations - FR-AR-007's rules, with no database.

    FR-AR-007  Quotes and order confirmations convertible to invoices.

The requirement is CONVERSION, and there was no quote to convert (see migration
0057), so this is the smallest quote conversion needs: lines, a validity date and a
lifecycle. Everything that DECIDES is here and pure: which status may follow which,
whether an offer has expired, whether a quote is valid to save, and what its net total
is. The service supplies the facts and turns a conversion into an invoice through
`InvoicingService`; this module never touches one.

--- The lifecycle ---

    draft --> sent --> accepted --> converted (an invoice now exists)
      |         |          |
      |         +--> declined
      +---------+----------+--> cancelled

`converted`, `declined` and `cancelled` are terminal. Restated in migration 0057's
triggers, so a second writer cannot sidestep it.

--- Expiry is derived, never stored ---

A quote is expired when today is after its `valid_until`. It is not a status that a
job must remember to set: a stored "expired" would be wrong the moment the date is
extended, and right only if something ran on the day. An expired quote cannot be sent
or accepted (the offer no longer stands); extending its validity revives it.

--- The net total is the invoice's net total ---

`net_total` uses `api.invoicing.vat.line_net`, which mirrors the generated column both
the quote line and the invoice line carry. So the total shown on the quote is exactly
the net of the invoice it converts into. VAT is deliberately NOT computed here: the
rate that applies is the one in force on the INVOICE date (CMP-014), which may not be
the quote's, and a VAT figure on a quote would be a promise about a rate that can
change.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from api.invoicing.vat import line_net

__all__ = [
    "MAX_LINES",
    "TRANSITIONS",
    "QuoteInvalid",
    "QuoteKind",
    "QuoteLine",
    "QuoteStatus",
    "can_transition",
    "is_expired",
    "net_total",
    "validate_quote",
]

MAX_LINES = 100


class QuoteKind(enum.Enum):
    QUOTE = "quote"
    ORDER_CONFIRMATION = "order_confirmation"


class QuoteStatus(enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    CONVERTED = "converted"

    @property
    def is_terminal(self) -> bool:
        return self in (QuoteStatus.DECLINED, QuoteStatus.CANCELLED, QuoteStatus.CONVERTED)


#: Which status may follow which. Mirrors `sales_quote_before_update` (0057).
TRANSITIONS: dict[QuoteStatus, frozenset[QuoteStatus]] = {
    QuoteStatus.DRAFT: frozenset({QuoteStatus.SENT, QuoteStatus.ACCEPTED, QuoteStatus.CANCELLED}),
    QuoteStatus.SENT: frozenset(
        {QuoteStatus.ACCEPTED, QuoteStatus.DECLINED, QuoteStatus.CANCELLED}
    ),
    QuoteStatus.ACCEPTED: frozenset({QuoteStatus.CONVERTED, QuoteStatus.CANCELLED}),
    QuoteStatus.DECLINED: frozenset(),
    QuoteStatus.CANCELLED: frozenset(),
    QuoteStatus.CONVERTED: frozenset(),
}


class QuoteInvalid(ValueError):
    """A quote that could not honestly be saved. Refused when saved."""


@dataclass(frozen=True, slots=True)
class QuoteLine:
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_treatment: str
    discount_percent: Decimal = Decimal(0)


def can_transition(current: QuoteStatus, target: QuoteStatus) -> bool:
    return target in TRANSITIONS[current]


def is_expired(valid_until: date | None, today: date) -> bool:
    """Whether the offer no longer stands. The last day is still valid: a quote valid
    until the 30th can be accepted on the 30th."""
    return valid_until is not None and today > valid_until


def net_total(lines: Sequence[QuoteLine]) -> Decimal:
    """The sum of the lines' totals, each rounded as the invoice will round it."""
    return sum(
        (
            line_net(
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_percent=line.discount_percent,
            )
            for line in lines
        ),
        Decimal("0.00"),
    )


def validate_quote(*, lines: Sequence[QuoteLine], valid_until: date | None, today: date) -> None:
    """Raise `QuoteInvalid` for anything that could not become an invoice line, or an
    offer that has expired before it was written."""
    if not lines:
        raise QuoteInvalid("a quote has at least one line")
    if len(lines) > MAX_LINES:
        raise QuoteInvalid(f"a quote has at most {MAX_LINES} lines")
    if valid_until is not None and valid_until < today:
        raise QuoteInvalid("the validity date is in the past")

    for line in lines:
        if isinstance(line.quantity, float) or isinstance(line.unit_price, float):
            raise QuoteInvalid("amounts are decimals, never floats (NFR-031)")
        if not line.description.strip():
            raise QuoteInvalid("every line has a description")
        if line.quantity == 0:
            raise QuoteInvalid("a line's quantity is not zero")
        if not Decimal(0) <= line.discount_percent <= Decimal(100):
            raise QuoteInvalid("a discount is between 0 and 100 percent")
        if not line.vat_treatment.strip():
            raise QuoteInvalid("every line has a VAT treatment")

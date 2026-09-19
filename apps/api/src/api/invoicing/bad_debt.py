"""Bad-debt write-off rules - FR-AR-013, SI-10 (ADR-076).

Pure: no I/O, no clock. The service supplies the facts and the date.

--- What is decided here ---

1. How the VAT inside an unpaid balance is worked out (`split_outstanding`).
2. When that VAT may be claimed back (`vat_reclaim_eligible_on`).

--- The VAT inside what is unpaid ---

An invoice carries one or more VAT groups (taxable amount plus VAT at a treatment). A
payment or a credit is not attributed to a group, so the unpaid balance is spread over the
groups in proportion to each group's gross, and the VAT part of each share in proportion to
that group's VAT. The split is by largest remainder in whole cents, so the shares always sum to the
outstanding balance EXACTLY - never a cent over or under - and none is negative.

    outstanding = 605.00 on a 1210.00 invoice (1000.00 + 210.00 VAT, one group)
    -> share 605.00, of which VAT 105.00

--- When VAT may be claimed back ---

Wet OB art. 29 lets a supplier reclaim the VAT on a receivable that has become uncollectable,
but not immediately: after `VAT_RECLAIM_WAIT_MONTHS` from the day the invoice fell due, or at
once when the customer is insolvent (bankruptcy or suspension of payments). The write-off
itself is not held back by this - the books can recognise the loss the day it is decided - so
the waiting period only governs the reclaim entry.

`VAT_RECLAIM_WAIT_MONTHS` is a legal figure entered from the author's understanding of the
rule, NOT verified against the current Besluit; ADR-076 flags it for review by a tax adviser,
as ADR-071 does for the dunning constants. It is a module constant so correcting it is one
line, not a hunt.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "VAT_RECLAIM_WAIT_MONTHS",
    "GroupShare",
    "VatGroupTotal",
    "WriteOffInvalid",
    "add_months",
    "split_outstanding",
    "vat_reclaim_eligible_on",
]

#: Months after the due date before the VAT may be reclaimed (unless insolvent). LEGAL DATA -
#: flagged for review in ADR-076.
VAT_RECLAIM_WAIT_MONTHS = 12

_CENT = Decimal("0.01")
ZERO = Decimal(0)


class WriteOffInvalid(ValueError):
    """The figures cannot be split: a float, a non-positive balance, or no invoice total."""


@dataclass(frozen=True, slots=True)
class VatGroupTotal:
    """One VAT group of the issued invoice, as frozen at issue (0037)."""

    treatment: str
    taxable: Decimal
    vat: Decimal

    @property
    def gross(self) -> Decimal:
        return self.taxable + self.vat


@dataclass(frozen=True, slots=True)
class GroupShare:
    """The part of the unpaid balance that belongs to one VAT group."""

    treatment: str
    #: This group's share of the unpaid balance, VAT included.
    amount: Decimal
    #: The VAT inside `amount`.
    vat: Decimal


def _cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def split_outstanding(
    groups: Sequence[VatGroupTotal], outstanding: Decimal
) -> tuple[GroupShare, ...]:
    """Spread `outstanding` over the invoice's VAT groups; see the module docstring.

    The shares' `amount`s sum to `outstanding` exactly and each `vat` is between zero and its
    share. Groups with no gross are skipped (nothing was ever owed on them).
    """
    if isinstance(outstanding, float) or not isinstance(outstanding, Decimal):
        raise WriteOffInvalid("the outstanding balance must be a Decimal, not a float (NFR-031)")
    if not outstanding.is_finite() or outstanding <= 0:
        raise WriteOffInvalid("there is nothing outstanding to write off")
    for group in groups:
        for value in (group.taxable, group.vat):
            if isinstance(value, float) or not isinstance(value, Decimal):
                raise WriteOffInvalid("VAT group amounts must be Decimal, not float (NFR-031)")

    live = [group for group in groups if group.gross > 0]
    total_gross = sum((group.gross for group in live), Decimal(0))
    if total_gross <= 0:
        raise WriteOffInvalid("the invoice has no total to write off against")

    # Largest remainder, in whole cents: every group gets the floor of its exact share, then
    # the cents left over go one each to the groups with the biggest fractional remainders
    # (ties to the earlier group). Exact by construction and never negative - rounding each
    # share and dumping the difference on the last group can overshoot on tiny balances.
    cents = int(outstanding * 100)
    gross_cents = [int(group.gross * 100) for group in live]
    total_cents = sum(gross_cents)
    floors = [cents * gross // total_cents for gross in gross_cents]
    remainders = [cents * gross % total_cents for gross in gross_cents]
    leftover = cents - sum(floors)
    by_remainder = sorted(range(len(live)), key=lambda i: (-remainders[i], i))
    for i in by_remainder[:leftover]:
        floors[i] += 1

    shares: list[GroupShare] = []
    for group, share_cents in zip(live, floors, strict=True):
        amount = Decimal(share_cents) / 100
        # The VAT part is the same fraction of the share as VAT is of the group's gross, and
        # can never exceed the share itself.
        vat = min(_cent(amount * group.vat / group.gross), amount)
        shares.append(GroupShare(treatment=group.treatment, amount=amount, vat=max(vat, ZERO)))
    return tuple(shares)


def add_months(start: date, months: int) -> date:
    """`start` plus whole months, clamped to the month's last day (31 Jan + 1 = 28/29 Feb)."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def vat_reclaim_eligible_on(
    *, due_date: date | None, invoice_date: date, customer_insolvent: bool
) -> date | None:
    """The first day the VAT may be reclaimed, or None when it may be reclaimed now.

    An insolvent customer waives the waiting period. Otherwise it runs from the due date,
    falling back to the invoice date for an invoice without one.
    """
    if customer_insolvent:
        return None
    return add_months(due_date or invoice_date, VAT_RECLAIM_WAIT_MONTHS)

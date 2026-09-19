"""SI-06: aged receivables and the customer statement - FR-AR-012's arithmetic.

    FR-AR-012  Aged receivables reporting and per-customer statement of account.

Pure, like `api.invoicing.dunning`: the facts come in as plain values and the
report goes out, with no database. The SQL (`invoicing.receivable_items`,
`invoicing.customer_movements`, migration 0054) decides WHICH items and movements
exist as of a date; this module only sorts them into buckets and adds them up.

--- Ageing is by days past the DUE date ---

The convention a Dutch bookkeeper expects: an invoice is "current" until its due
date has passed, then falls into 1-30, 31-60, 61-90 and over-90 days late. Not
by invoice date: an invoice on 60-day terms is not late at day 45.

An invoice with no due date cannot be late, and inventing one (invoice date? 30
days?) would put it in a bucket on the strength of a term nobody agreed - so it has
a bucket of its own, `NO_DUE_DATE`, that is counted in the totals and visible.

--- The totals are checked, not hoped for ---

Every bucket total, every customer total and the grand total are the same money cut
three ways, so `AgeingReport.__post_init__` refuses to construct one where they
disagree - the device `InvoiceTotals` uses. A future change that dropped an item
from one cut but not another fails here, not on a director's screen.

--- A statement is one list, and the balances are arithmetic over it ---

`build_statement` takes every movement on the account and derives the opening
balance (everything before the period), the running balance (after each line) and
the closing balance. There is no separately queried "balance brought forward" that
could disagree with the lines beneath it.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

__all__ = [
    "AgeBucket",
    "AgeingReport",
    "CustomerAgeing",
    "MovementKind",
    "OpenItem",
    "Statement",
    "StatementLine",
    "StatementMovement",
    "age_items",
    "bucket_for",
    "build_statement",
]

ZERO = Decimal("0.00")


class AgeBucket(enum.Enum):
    """Days past the due date. The order is the order they are shown in."""

    CURRENT = "current"
    DAYS_1_30 = "days_1_30"
    DAYS_31_60 = "days_31_60"
    DAYS_61_90 = "days_61_90"
    OVER_90 = "over_90"
    #: No due date, so no "late": counted, never guessed into another bucket.
    NO_DUE_DATE = "no_due_date"


#: The upper bound (inclusive) of each late bucket, in days past due.
_LATE_BOUNDS: tuple[tuple[int, AgeBucket], ...] = (
    (30, AgeBucket.DAYS_1_30),
    (60, AgeBucket.DAYS_31_60),
    (90, AgeBucket.DAYS_61_90),
)


def bucket_for(due_date: date | None, as_of: date) -> AgeBucket:
    """Which bucket an item falls in on `as_of`.

    The due date itself is CURRENT - the customer has until the end of it - and
    the first late day is 1 day past due. Consistent with
    `api.invoicing.dunning.days_overdue`, so the ageing report and the reminder
    ladder agree about what "late" means.
    """
    if due_date is None:
        return AgeBucket.NO_DUE_DATE
    days_late = (as_of - due_date).days
    if days_late <= 0:
        return AgeBucket.CURRENT
    for upper, bucket in _LATE_BOUNDS:
        if days_late <= upper:
            return bucket
    return AgeBucket.OVER_90


@dataclass(frozen=True, slots=True)
class OpenItem:
    """One invoice that still owed money on the report date."""

    invoice_id: uuid.UUID
    invoice_reference: str | None
    invoice_date: date
    due_date: date | None
    customer_id: uuid.UUID | None
    customer_name: str
    outstanding: Decimal


@dataclass(frozen=True, slots=True)
class CustomerAgeing:
    """One customer's open items, cut into buckets. `items` is the drill-down to
    the invoices themselves (FR-RPT-002)."""

    customer_id: uuid.UUID | None
    customer_name: str
    buckets: dict[AgeBucket, Decimal]
    items: tuple[tuple[OpenItem, AgeBucket, int], ...]

    @property
    def total(self) -> Decimal:
        return sum(self.buckets.values(), ZERO)


@dataclass(frozen=True, slots=True)
class AgeingReport:
    as_of: date
    bucket_totals: dict[AgeBucket, Decimal]
    customers: tuple[CustomerAgeing, ...]
    grand_total: Decimal = field(default=ZERO)

    def __post_init__(self) -> None:
        by_bucket = sum(self.bucket_totals.values(), ZERO)
        by_customer = sum((c.total for c in self.customers), ZERO)
        if not (by_bucket == by_customer == self.grand_total):
            raise ValueError(
                f"the ageing report does not add up: buckets {by_bucket}, customers "
                f"{by_customer}, grand total {self.grand_total}"
            )


def _key(item: OpenItem) -> tuple[str, str]:
    """Group by the customer master record when there is one, else by the name
    typed on the invoice. A one-off customer invoiced twice under one name is one
    debtor on the report - the ledger makes them two parties (ADR-039), and the
    report is kinder than the ledger there."""
    if item.customer_id is not None:
        return ("id", str(item.customer_id))
    return ("name", item.customer_name.strip().casefold())


def age_items(items: Sequence[OpenItem], as_of: date) -> AgeingReport:
    """Bucket every item, group by customer, total three ways.

    Customers come out largest debt first - the order somebody chasing money reads
    them in - with ties broken by name so the order is stable. Within a customer,
    items are oldest due date first.
    """
    for item in items:
        if isinstance(item.outstanding, float):
            raise TypeError("an outstanding amount must be Decimal, not float (NFR-031)")

    bucket_totals = {bucket: ZERO for bucket in AgeBucket}
    grouped: dict[tuple[str, str], list[tuple[OpenItem, AgeBucket, int]]] = {}
    for item in items:
        if item.outstanding <= 0:
            continue  # nothing is owed; the SQL already excludes these
        bucket = bucket_for(item.due_date, as_of)
        days_late = max((as_of - item.due_date).days, 0) if item.due_date else 0
        bucket_totals[bucket] += item.outstanding
        grouped.setdefault(_key(item), []).append((item, bucket, days_late))

    customers = []
    for rows in grouped.values():
        rows.sort(key=lambda r: (r[0].due_date or date.max, r[0].invoice_reference or ""))
        per_bucket = {bucket: ZERO for bucket in AgeBucket}
        for item, bucket, _ in rows:
            per_bucket[bucket] += item.outstanding
        first = rows[0][0]
        customers.append(
            CustomerAgeing(
                customer_id=first.customer_id,
                customer_name=first.customer_name,
                buckets=per_bucket,
                items=tuple(rows),
            )
        )
    customers.sort(key=lambda c: (-c.total, c.customer_name.casefold()))

    return AgeingReport(
        as_of=as_of,
        bucket_totals=bucket_totals,
        customers=tuple(customers),
        grand_total=sum(bucket_totals.values(), ZERO),
    )


# ===========================================================================
# The statement
# ===========================================================================


class MovementKind(enum.Enum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    PAYMENT = "payment"
    PAYMENT_VOID = "payment_void"


@dataclass(frozen=True, slots=True)
class StatementMovement:
    """One movement on the account, as `invoicing.customer_movements` returns it.

    Both amounts are unsigned, exactly as the ledger's own are: direction is
    which column the amount is in. A debit raises what the customer owes.
    """

    movement_date: date
    kind: MovementKind
    reference: str | None
    invoice_id: uuid.UUID | None
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class StatementLine:
    movement: StatementMovement
    #: What the customer owed after this line.
    balance: Decimal


@dataclass(frozen=True, slots=True)
class Statement:
    date_from: date
    date_to: date
    opening_balance: Decimal
    lines: tuple[StatementLine, ...]
    closing_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal

    def __post_init__(self) -> None:
        expected = self.opening_balance + self.total_debit - self.total_credit
        if self.closing_balance != expected:
            raise ValueError(
                f"the statement does not add up: opening {self.opening_balance} + debits "
                f"{self.total_debit} - credits {self.total_credit} is {expected}, "
                f"not the closing balance {self.closing_balance}"
            )


def build_statement(
    movements: Sequence[StatementMovement], date_from: date, date_to: date
) -> Statement:
    """Opening balance, lines with a running balance, and closing balance.

    Everything before `date_from` is folded into the opening balance; everything
    after `date_to` is ignored. Because all three balances are computed from the
    one list, they cannot disagree with the lines.

    Positive means the customer owes; negative means they are in credit (an
    overpayment cannot happen today - ADR-070 - but a credit note after a payment
    can leave a credit, and the statement shows it rather than hiding it).
    """
    if date_from > date_to:
        raise ValueError("a statement period must not end before it starts")

    for movement in movements:
        if isinstance(movement.debit, float) or isinstance(movement.credit, float):
            raise TypeError("statement amounts must be Decimal, not float (NFR-031)")

    # A STABLE sort by date: the SQL already orders them, but the running balance
    # must not depend on a caller having done so, and stability keeps the
    # invoice-before-credit-before-payment order within a day.
    ordered = sorted(movements, key=lambda m: m.movement_date)
    opening = sum(
        (m.debit - m.credit for m in ordered if m.movement_date < date_from),
        ZERO,
    )
    balance = opening
    lines: list[StatementLine] = []
    for movement in ordered:
        if not (date_from <= movement.movement_date <= date_to):
            continue
        balance = balance + movement.debit - movement.credit
        lines.append(StatementLine(movement=movement, balance=balance))

    return Statement(
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening,
        lines=tuple(lines),
        closing_balance=balance,
        total_debit=sum((line.movement.debit for line in lines), ZERO),
        total_credit=sum((line.movement.credit for line in lines), ZERO),
    )

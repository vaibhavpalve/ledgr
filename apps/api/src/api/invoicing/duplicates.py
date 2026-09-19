"""SI-12: warn when a draft invoice looks like one already raised.

    SI-12  Warn when creating an invoice very similar to a recent one for the
           same customer.

This extends ADR-034's pattern from expenses to sales invoices, and keeps all
of its commitments:

--- Warn, never block ---

Sending the same customer the same amount twice in a month is ordinary: a
retainer billed on the 1st and the 15th, two identical call-outs, a repeat
order. A rule that refused it would be wrong about the business and teach
people to work around it. So everything here RETURNS findings; nothing raises
and `can_be_issued` never consults them - asserted in
tests/invoicing/test_duplicates.py, because "warn" quietly becoming "block" is
one added `and not warnings` away.

--- What "very similar" means ---

Two invoices are compared only when they are for the SAME CUSTOMER and dated
within `WINDOW_DAYS` of each other. Then:

    IDENTICAL   the same lines: description, quantity, unit price and discount,
                in any order.
    SAME_TOTAL  different lines, same net total. Weaker - two different jobs can
                cost the same - but a re-typed invoice whose wording drifted
                ("Consultancy" / "Consulting") is exactly this, and a warning is
                cheap where a double bill is not.

The net total is compared rather than the gross, so the check needs no VAT rate
lookup and cannot disagree with itself across a rate change. Both totals are
`Decimal` (NFR-031).

The same customer means the same customer-master record when both invoices
carry one (`customer_id`), or else the same name once normalised. The name is
compared as well as the id because a one-off customer has no id, and because
an invoice typed out by hand for somebody who is later added to the master is
still the same customer.

--- Not compared ---

A credit note is neither a subject nor a candidate: it reverses an invoice and
matching it against that invoice is the correct behaviour, not a duplicate. An
invoice with no lines is not a subject either - two empty drafts are two
forms, not two claims. Draft candidates ARE compared: the duplicate is often
the half-finished draft somebody forgot they had started.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from api.i18n.catalogue import translate
from api.i18n.formatting import format_date, format_money
from api.i18n.language import Language

#: How far apart two invoice dates may be and still be "recent" to each other,
#: either way round - a back-dated invoice is compared with the one that
#: followed it as readily as the reverse. A month, because that is the longest
#: gap between two runs of a legitimately repeated invoice.
WINDOW_DAYS = 30

#: The most warnings one invoice carries. A customer billed the same amount
#: weekly would otherwise list every one of them, and past the first few the
#: list stops being read.
MAX_WARNINGS = 5

_WHITESPACE = re.compile(r"\s+")
_EDGE_PUNCTUATION = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


class InvoiceDuplicateStrength(enum.Enum):
    """How alike the two invoices are. Both warn; only the wording differs."""

    IDENTICAL = "identical"
    SAME_TOTAL = "same_total"


#: The order warnings are listed in: strongest first.
_STRENGTH_ORDER = {InvoiceDuplicateStrength.IDENTICAL: 0, InvoiceDuplicateStrength.SAME_TOTAL: 1}


@dataclass(frozen=True, slots=True)
class LineFingerprint:
    """One line, reduced to what makes it the same line."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceFingerprint:
    """Everything SI-12 compares, and nothing else - the analogue of ADR-034's
    `ExpenseTriple`.

    A value rather than a `SalesInvoice`, so the rule is testable without the
    rest of an invoice and callable on an invoice that is not stored yet.
    """

    customer_id: uuid.UUID | None
    customer_name: str
    invoice_date: date
    lines: tuple[LineFingerprint, ...]
    net_total: Decimal

    @property
    def normalised_customer(self) -> str:
        return normalise_name(self.customer_name)

    @property
    def is_comparable(self) -> bool:
        """Whether there is anything to compare. An invoice with no lines, or
        no customer name, is a form somebody is still filling in."""
        return bool(self.lines) and bool(self.normalised_customer)


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """An existing invoice, as SI-12 sees it."""

    invoice_id: uuid.UUID
    #: "draft" or "issued" - only the word, so this module does not import the
    #: model it is a leaf of.
    status: str
    invoice_reference: str | None
    fingerprint: InvoiceFingerprint


@dataclass(frozen=True, slots=True)
class InvoiceDuplicateWarning:
    """One existing invoice that looks like this one.

    Carries the other invoice's own identifying facts - all of which belong to
    the same administration and the same customer, so nothing here crosses a
    tenant or a person boundary.
    """

    invoice_id: uuid.UUID
    strength: InvoiceDuplicateStrength
    status: str
    invoice_reference: str | None
    invoice_date: date
    net_total: Decimal


def message_for(warning: InvoiceDuplicateWarning, language: Language) -> str:
    """The sentence a person reads - in the READER's language, like
    `statutory.message_for`. Amounts and dates follow the administration's
    formatting locale, not the language (FR-LOC-002).

    An issued invoice is named by its number; a draft has none, so it is named
    by its date - which is why there are two wordings per strength rather than
    one with a blank in it.
    """
    is_draft = warning.status == "draft"
    key = f"invoice.duplicate.{warning.strength.value}.{'draft' if is_draft else 'issued'}"
    return translate(
        key,
        language,
        reference=warning.invoice_reference or "",
        date=format_date(warning.invoice_date),
        amount=format_money(warning.net_total),
    )


def normalise_name(name: str) -> str:
    """The form a customer name is compared in - ADR-034's rule, verbatim:
    collapse whitespace, strip punctuation at the ends only, casefold.
    """
    collapsed = _WHITESPACE.sub(" ", name.strip())
    return _EDGE_PUNCTUATION.sub("", collapsed).casefold()


def _same_customer(left: InvoiceFingerprint, right: InvoiceFingerprint) -> bool:
    if left.customer_id is not None and left.customer_id == right.customer_id:
        return True
    # A name that normalises to nothing says nothing about who the customer is,
    # so it cannot establish that two invoices are for the same one - ADR-034
    # section 5, found there by a property test.
    normalised = left.normalised_customer
    return bool(normalised) and normalised == right.normalised_customer


def _canonical_lines(
    lines: Sequence[LineFingerprint],
) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    return sorted(
        (
            _WHITESPACE.sub(" ", line.description.strip()).casefold(),
            line.quantity,
            line.unit_price,
            line.discount_percent,
        )
        for line in lines
    )


def strength_of(
    subject: InvoiceFingerprint, other: InvoiceFingerprint
) -> InvoiceDuplicateStrength | None:
    """How alike two invoices are, or None when they are not alike at all.

    Pure and symmetric: `strength_of(a, b) == strength_of(b, a)`.
    """
    if not (subject.is_comparable and other.is_comparable):
        return None
    if not _same_customer(subject, other):
        return None
    if abs((subject.invoice_date - other.invoice_date).days) > WINDOW_DAYS:
        return None
    if _canonical_lines(subject.lines) == _canonical_lines(other.lines):
        return InvoiceDuplicateStrength.IDENTICAL
    if subject.net_total == other.net_total:
        return InvoiceDuplicateStrength.SAME_TOTAL
    return None


def find_duplicates(
    subject: InvoiceFingerprint,
    candidates: Sequence[DuplicateCandidate],
    *,
    subject_id: uuid.UUID | None = None,
) -> list[InvoiceDuplicateWarning]:
    """Every candidate that looks like `subject`, strongest and nearest first,
    at most `MAX_WARNINGS`.

    `subject_id` excludes the invoice from matching itself, for a caller that
    passes candidates including it.
    """
    warnings: list[InvoiceDuplicateWarning] = []
    for candidate in candidates:
        if candidate.invoice_id == subject_id:
            continue
        strength = strength_of(subject, candidate.fingerprint)
        if strength is None:
            continue
        warnings.append(
            InvoiceDuplicateWarning(
                invoice_id=candidate.invoice_id,
                strength=strength,
                status=candidate.status,
                invoice_reference=candidate.invoice_reference,
                invoice_date=candidate.fingerprint.invoice_date,
                net_total=candidate.fingerprint.net_total,
            )
        )
    warnings.sort(
        key=lambda w: (
            _STRENGTH_ORDER[w.strength],
            abs((w.invoice_date - subject.invoice_date).days),
            str(w.invoice_id),
        )
    )
    return warnings[:MAX_WARNINGS]

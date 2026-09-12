"""Duplicate detection - FR-EXP-001g.

    FR-EXP-001g  Duplicate detection WARNS when a receipt matching an existing
                 expense on supplier, date and amount is captured.

--- Warn, never block ---

Legitimate duplicates exist and are ordinary. Two identical coffees on the same
morning, two colleagues each buying a train ticket on the same route, a monthly
subscription billed twice because the first attempt failed. A system that
refused any of those would be wrong about the money AND would teach people to
work around it, which is worse than the duplicates it stopped.

So every function here RETURNS findings. Nothing raises, nothing refuses, and
`mark_ready` and the posting path both succeed with warnings outstanding -
asserted in tests/expenses/test_duplicates.py, because "warn, don't block" is
the kind of property that quietly becomes "block" the first time somebody
treats a warning as an error.

What a warning does change is the record: the submission audit entry says how
many were outstanding when the claim was released, so an approver reading it
later can see what the submitter saw.

--- "When a receipt is captured", and when that is actually possible ---

The requirement says the check happens at capture. Matching on supplier, date
and amount needs those three, and a photograph has none of them: extraction is
P1 and not built (FR-EXP-001c), so a freshly captured receipt is an empty
draft.

The check is therefore written as a function of the TRIPLE rather than of the
capture event, and it runs wherever the triple becomes known. Today that is the
expense form, as the person types. When extraction lands and fills those fields
at capture, this runs there instead with no change to any of it - which is the
whole reason it takes three values rather than a receipt.

There is already a capture-time signal that needs no extraction: the review
list flags a byte-identical FILE captured twice in one session
(`ReviewEntry.duplicate_of`). That is a different question - the same image
rather than the same claim - and the two are complementary. A re-photographed
receipt is a different image of the same purchase and only this check sees it.

--- Two strengths, and only one of them is this module's ---

    EXACT      the normalised suppliers are equal, the dates are equal, and the
               gross amounts are equal. Decidable here, with no database.
    PROBABLE   the suppliers are SIMILAR rather than equal - "ALBERT HEIJN
               1043" beside "Albert Heijn". Decided in SQL by pg_trgm, because
               reimplementing its trigram similarity in Python to the same
               answer is a promise this module would break the first time
               Postgres tuned it.

The split is deliberate and stated rather than hidden: `is_exact_match` is the
rule both implementations share and tests/expenses/duplicate_cases.py runs
against both, and similarity belongs to `expenses.duplicate_candidates` in
migration 0036 alone.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

#: Runs of whitespace, so "Albert  Heijn" and "Albert Heijn" are one supplier.
_WHITESPACE = re.compile(r"\s+")

#: Punctuation at either end of the name. Deliberately NOT punctuation in the
#: middle: "Jan's Cafe" and "Jans Cafe" are arguably the same shop, and
#: stripping the apostrophe to decide so is the kind of normalisation that
#: eventually merges two real suppliers whose names differ by a hyphen.
_EDGE_PUNCTUATION = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)

#: How alike two supplier names must be to raise a PROBABLE warning, on
#: pg_trgm's 0..1 similarity.
#:
#: 0.45 is generous, and chosen that way on purpose: this is a WARNING, so the
#: costs are asymmetric. A false positive is a line a person glances at and
#: dismisses; a false negative is a claim paid twice. "Albert Heijn 1043" and
#: "Albert Heijn" score around 0.6; two unrelated Dutch suppliers rarely reach
#: 0.45 because the trigram sets share little beyond common letter pairs.
#:
#: Named here rather than inlined in the SQL so that the number has one home
#: and this paragraph is attached to it.
SIMILARITY_THRESHOLD = 0.45


class DuplicateStrength(enum.Enum):
    """How alike the two claims are. Both warn; only the wording differs."""

    #: Normalised supplier, date and amount all equal.
    EXACT = "exact"
    #: Same date and amount, similar supplier.
    PROBABLE = "probable"


@dataclass(frozen=True, slots=True)
class ExpenseTriple:
    """The three fields FR-EXP-001g matches on, and nothing else.

    A value rather than three loose arguments, because the ORDER of three
    same-shaped parameters is exactly the kind of thing a caller gets subtly
    wrong - and a supplier compared against a date fails silently by matching
    nothing.
    """

    supplier: str | None
    on: date | None
    gross_amount: Decimal | None

    @property
    def is_complete(self) -> bool:
        """Whether there is enough to look for a duplicate at all.

        An incomplete triple is the normal state of a claim somebody is still
        filling in (FR-EXP-001c), not an error - so callers check this rather
        than being refused.
        """
        return self.supplier is not None and self.on is not None and self.gross_amount is not None

    @property
    def normalised_supplier(self) -> str | None:
        return None if self.supplier is None else normalise_supplier(self.supplier)


@dataclass(frozen=True, slots=True)
class DuplicateWarning:
    """One existing claim that looks like this one.

    Carries the matching expense's own triple, which tells the reader nothing
    they did not just type - that is the point. What it does NOT carry is who
    submitted it: "this receipt has already been claimed" is what somebody
    needs to act, and a colleague's name is not.
    """

    expense_id: uuid.UUID
    strength: DuplicateStrength
    supplier: str
    on: date
    gross_amount: Decimal
    status: str
    #: True when the same person filed the other claim. The distinction that
    #: matters: their own double entry is a mistake to fix, and somebody
    #: else's is a conversation to have.
    same_submitter: bool
    #: pg_trgm's score, for a PROBABLE match. None for an EXACT one, where
    #: there is nothing to score.
    similarity: float | None = None


def normalise_supplier(supplier: str) -> str:
    """The form a supplier name is compared in.

    Casefold rather than lower: `str.casefold` handles the cases `lower` does
    not, and a Dutch supplier list contains enough accented and Greek-lettered
    names for that to matter eventually.

    This is the ONLY normalisation, and `expenses.normalise_supplier` in
    migration 0036 performs the same three steps in the same order.
    tests/expenses/duplicate_cases.py runs one table against both.
    """
    collapsed = _WHITESPACE.sub(" ", supplier.strip())
    return _EDGE_PUNCTUATION.sub("", collapsed).casefold()


def is_exact_match(left: ExpenseTriple, right: ExpenseTriple) -> bool:
    """FR-EXP-001g's rule, read literally: the same supplier, date and amount.

    Pure, so it is callable before anything is stored - which is what a
    capture-time check will need when extraction arrives, and what lets a
    client warn about two claims it is holding in a batch before either exists.

    An incomplete triple matches nothing. Two half-filled forms that happen to
    share a supplier are not a duplicate; they are two forms.

    A supplier that normalises to NOTHING also matches nothing, and that rule
    was found by a property test rather than reasoned out. `":"` and `"..."`
    both normalise to the empty string, so under a plain equality rule every
    claim with a punctuation-only supplier would warn about every other one.
    The database's CHECK does not stop it either - `length(btrim(':')) > 0` is
    true.

    Refusing to match on an empty name is the right answer rather than a
    special case: a name carrying no letters or digits says nothing about which
    shop it was, so it cannot establish that two claims are the same shop.
    """
    if not (left.is_complete and right.is_complete):
        return False
    normalised = left.normalised_supplier
    if not normalised:
        return False
    return (
        normalised == right.normalised_supplier
        and left.on == right.on
        and left.gross_amount == right.gross_amount
    )


def find_exact_matches(
    subject: ExpenseTriple, others: dict[uuid.UUID, ExpenseTriple]
) -> list[uuid.UUID]:
    """Every id in `others` whose triple matches `subject`.

    In memory, for callers that already hold the candidates - a batch being
    reviewed before any of it is saved, most obviously. The archive-wide search
    is `expenses.duplicate_candidates`, which also scores similarity.
    """
    return [expense_id for expense_id, triple in others.items() if is_exact_match(subject, triple)]


def describe(warnings: list[DuplicateWarning]) -> str:
    """A one-line summary for a log or an audit detail.

    Not user-facing text - that is in the message catalogue (FR-LOC-001).
    """
    if not warnings:
        return "no duplicate warnings"
    exact = sum(1 for w in warnings if w.strength is DuplicateStrength.EXACT)
    probable = len(warnings) - exact
    return f"{exact} exact and {probable} probable duplicate warning(s)"

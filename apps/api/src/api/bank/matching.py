"""How sure a bank line's match to an open document is (FR-BNK-003/004, ADR-091, ADR-092).

    FR-BNK-003  Automatic matching of transactions to open AR/AP items using amount, payment
                reference, IBAN, name similarity and historical behaviour, with a confidence score.
    FR-BNK-004  ... auto-post above the high threshold, propose between thresholds, queue for
                manual handling below. Defaults are conservative.

Pure. Money IN is matched to open sales invoices, money OUT to receipts booked as paid from the
business account or card (ADR-092). A candidate's open amount always EQUALS the line's (partial
and batched payments are the person's call, FR-BNK-005). What raises confidence:

    reference       the invoice number appears in the payment's description - ours, typed by the
                    customer, or the supplier's, carried on our transfer
    name            the counterparty's name is the customer's or supplier's, give or take
                    legal-form suffixes
    only_candidate  no other open document of that kind has this amount

    high      a reference match; or the name AND the only candidate
    medium    the name, or the only candidate
    low       an amount and nothing else

Only HIGH is ever applied in bulk ("match all certain" in the web app), and only when it is the
single high candidate for that line - the conservative default FR-BNK-004 asks for.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from api.bank.model import BankTransaction, CandidateKind, MatchCandidate


class Confidence(enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}

#: Legal-form words that say nothing about WHO: "Hotel De Gouden Leeuw" pays as "HOTEL DE GOUDEN
#: LEEUW BV".
_NOISE = {"bv", "b.v.", "b.v", "nv", "n.v.", "vof", "v.o.f.", "cv", "c.v.", "holding", "de", "het"}


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: MatchCandidate
    confidence: Confidence
    reasons: tuple[str, ...]


def _alnum(value: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def _words(value: str | None) -> str:
    tokens = re.findall(r"[0-9a-zà-ÿ.]+", (value or "").lower())
    return " ".join(t for t in tokens if t not in _NOISE).strip()


def names_match(counterparty: str | None, party: str | None) -> bool:
    a, b = _words(counterparty), _words(party)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def reference_in(transaction: BankTransaction, reference: str | None) -> bool:
    wanted = _alnum(reference)
    if len(wanted) < 3:
        return False
    haystack = _alnum(transaction.description) + " " + _alnum(transaction.counterparty_name)
    return wanted in haystack


def score(
    transaction: BankTransaction, candidates: Sequence[MatchCandidate]
) -> list[ScoredCandidate]:
    """Every candidate with its confidence, most certain first (then oldest document first)."""
    only = len(candidates) == 1
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        reasons: list[str] = []
        if reference_in(transaction, candidate.reference):
            reasons.append("reference")
        if names_match(transaction.counterparty_name, candidate.party_name):
            reasons.append("name")
        if only:
            reasons.append("only_candidate")
        if "reference" in reasons or ("name" in reasons and only):
            confidence = Confidence.HIGH
        elif "name" in reasons or only:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW
        scored.append(ScoredCandidate(candidate, confidence, tuple(reasons)))
    scored.sort(key=lambda s: (_RANK[s.confidence], s.candidate.document_date))
    return scored


def certain(scored: Sequence[ScoredCandidate]) -> ScoredCandidate | None:
    """The one match safe to apply without asking: the single HIGH candidate, or nothing."""
    high = [s for s in scored if s.confidence is Confidence.HIGH]
    return high[0] if len(high) == 1 else None


def ambiguous(scored: ScoredCandidate) -> ScoredCandidate:
    """A HIGH match that competes with another - two documents both named in one payment, or one
    document certain for two payments - is only a proposal: a person picks."""
    return ScoredCandidate(scored.candidate, Confidence.MEDIUM, (*scored.reasons, "ambiguous"))


def fits(transaction: BankTransaction, candidate: MatchCandidate) -> bool:
    """Could this document be what the line paid? Direction decides the kind - a customer's
    payment comes in, a supplier's goes out - and the open amount must be the line's exactly."""
    if transaction.is_inflow:
        return (
            candidate.kind is CandidateKind.SALES_INVOICE and candidate.amount == transaction.amount
        )
    return candidate.kind is CandidateKind.EXPENSE and candidate.amount == -transaction.amount


def suggest(
    transactions: Sequence[BankTransaction], open_documents: Sequence[MatchCandidate]
) -> dict[uuid.UUID, ScoredCandidate]:
    """The best match for each line, among the open documents it could have paid."""
    best: dict[uuid.UUID, ScoredCandidate] = {}
    for transaction in transactions:
        scored = score(transaction, [c for c in open_documents if fits(transaction, c)])
        if not scored:
            continue
        top = scored[0]
        if top.confidence is Confidence.HIGH and certain(scored) is None:
            top = ambiguous(top)
        best[transaction.id] = top
    # One document certain for two lines (a customer paid twice) is certain for neither.
    claims = Counter(
        s.candidate.document_id for s in best.values() if s.confidence is Confidence.HIGH
    )
    for transaction_id, suggestion in list(best.items()):
        if (
            suggestion.confidence is Confidence.HIGH
            and claims[suggestion.candidate.document_id] > 1
        ):
            best[transaction_id] = ambiguous(suggestion)
    return best

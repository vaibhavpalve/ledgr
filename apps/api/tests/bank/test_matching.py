"""FR-BNK-003/004 (ADR-091): how sure a bank line's match to an open invoice is."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from api.bank.matching import Confidence, ambiguous, certain, names_match, score, suggest
from api.bank.model import BankTransaction, MatchCandidate, TransactionStatus


def transaction(name: str | None, description: str | None) -> BankTransaction:
    return BankTransaction(
        id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        bank_account_id=uuid.uuid4(),
        booking_date=date(2026, 9, 22),
        value_date=None,
        amount=Decimal("1149.50"),
        currency="EUR",
        counterparty_name=name,
        counterparty_iban=None,
        description=description,
        external_id="x",
        status=TransactionStatus.UNMATCHED,
        matched_sales_invoice_id=None,
        matched_payment_id=None,
        journal_entry_id=None,
        reconciled_at=None,
    )


def invoice(reference: str, customer: str, day: int = 1) -> MatchCandidate:
    return MatchCandidate(
        invoice_id=uuid.uuid4(),
        invoice_reference=reference,
        customer_name=customer,
        outstanding=Decimal("1149.50"),
        invoice_date=date(2026, 9, day),
    )


def test_the_invoice_number_in_the_description_is_certain() -> None:
    hotel = invoice("2026-0007", "Hotel De Gouden Leeuw B.V.")
    other = invoice("2026-0009", "Bakkerij Brood", day=2)
    scored = score(transaction("ONBEKEND", "betaling factuur 2026 0007 dank"), [other, hotel])
    assert scored[0].candidate is hotel
    assert scored[0].confidence is Confidence.HIGH
    assert "reference" in scored[0].reasons
    assert certain(scored) is scored[0]


def test_the_payers_name_on_the_only_candidate_is_certain() -> None:
    hotel = invoice("2026-0007", "Hotel De Gouden Leeuw B.V.")
    [scored] = score(transaction("HOTEL DE GOUDEN LEEUW BV", None), [hotel])
    assert scored.confidence is Confidence.HIGH
    assert set(scored.reasons) == {"name", "only_candidate"}


def test_a_name_among_several_equal_amounts_is_only_a_proposal() -> None:
    hotel = invoice("2026-0007", "Hotel De Gouden Leeuw B.V.")
    other = invoice("2026-0009", "Bakkerij Brood")
    scored = score(transaction("Hotel De Gouden Leeuw", None), [other, hotel])
    assert [s.confidence for s in scored] == [Confidence.MEDIUM, Confidence.LOW]
    assert certain(scored) is None


def test_two_certain_matches_are_not_certain() -> None:
    a = invoice("2026-0007", "Klant A")
    b = invoice("2026-0008", "Klant B")
    scored = score(transaction(None, "facturen 2026-0007 en 2026-0008"), [a, b])
    assert all(s.confidence is Confidence.HIGH for s in scored)
    assert certain(scored) is None


def test_names_ignore_legal_form_and_case() -> None:
    assert names_match("VAN DOORN BOUW BV", "Van Doorn Bouw B.V.")
    assert not names_match("Jansen", "Pietersen Holding B.V.")
    assert not names_match(None, "Anyone")


def test_a_competing_certain_match_becomes_a_proposal() -> None:
    [scored] = score(transaction(None, "factuur 2026-0007"), [invoice("2026-0007", "Klant A")])
    demoted = ambiguous(scored)
    assert demoted.confidence is Confidence.MEDIUM
    assert demoted.reasons[-1] == "ambiguous"
    assert demoted.candidate is scored.candidate


def test_one_invoice_certain_for_two_payments_is_certain_for_neither() -> None:
    hotel = invoice("2026-0007", "Hotel De Gouden Leeuw B.V.")
    first = transaction(None, "factuur 2026-0007")
    again = transaction(None, "factuur 2026-0007 (nogmaals)")
    best = suggest([first, again], [hotel])
    assert {s.confidence for s in best.values()} == {Confidence.MEDIUM}


def test_suggest_only_offers_invoices_of_exactly_the_amount() -> None:
    short = MatchCandidate(
        invoice_id=uuid.uuid4(),
        invoice_reference="2026-0007",
        customer_name="Hotel De Gouden Leeuw B.V.",
        outstanding=Decimal("1149.49"),
        invoice_date=date(2026, 9, 1),
    )
    assert suggest([transaction(None, "factuur 2026-0007")], [short]) == {}


def test_suggest_picks_the_certain_invoice() -> None:
    hotel = invoice("2026-0007", "Hotel De Gouden Leeuw B.V.")
    other = invoice("2026-0009", "Bakkerij Brood")
    line = transaction(None, "factuur 2026-0007")
    best = suggest([line], [other, hotel])
    assert best[line.id].candidate is hotel
    assert best[line.id].confidence is Confidence.HIGH

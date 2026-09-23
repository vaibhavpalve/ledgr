from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.bank.csv_parser import InvalidRow, MissingColumns, parse_statement_csv


def test_parses_a_well_formed_statement() -> None:
    csv = (
        "date,amount,counterparty_name,counterparty_iban,description\n"
        "2026-09-05,-12.50,KPN,NL00KPN0000000000,Telefoon\n"
        "2026-09-06,605.00,De Vries Holding,NL00DVH0000000000,Factuur 2026-014\n"
    )
    rows = parse_statement_csv(csv)
    assert len(rows) == 2
    assert rows[0].booking_date == date(2026, 9, 5)
    assert rows[0].amount == Decimal("-12.50")
    assert rows[0].counterparty_name == "KPN"
    assert rows[1].amount == Decimal("605.00")
    assert rows[1].description == "Factuur 2026-014"


def test_optional_columns_may_be_absent() -> None:
    csv = "date,amount\n2026-09-05,-12.50\n"
    rows = parse_statement_csv(csv)
    assert rows[0].counterparty_name is None
    assert rows[0].counterparty_iban is None
    assert rows[0].description is None


def test_header_is_case_insensitive_and_order_independent() -> None:
    csv = "AMOUNT,DATE\n-12.50,2026-09-05\n"
    rows = parse_statement_csv(csv)
    assert rows[0].amount == Decimal("-12.50")


def test_missing_required_column_is_refused() -> None:
    with pytest.raises(MissingColumns):
        parse_statement_csv("amount\n-12.50\n")


def test_empty_file_is_refused() -> None:
    with pytest.raises(MissingColumns):
        parse_statement_csv("")


def test_unparsable_date_names_the_row() -> None:
    csv = "date,amount\n05-09-2026,-12.50\n"
    with pytest.raises(InvalidRow) as excinfo:
        parse_statement_csv(csv)
    assert excinfo.value.row_number == 2
    assert excinfo.value.field == "date"


def test_unparsable_amount_names_the_row() -> None:
    csv = "date,amount\n2026-09-05,not-a-number\n"
    with pytest.raises(InvalidRow) as excinfo:
        parse_statement_csv(csv)
    assert excinfo.value.field == "amount"


def test_zero_amount_is_refused() -> None:
    csv = "date,amount\n2026-09-05,0.00\n"
    with pytest.raises(InvalidRow):
        parse_statement_csv(csv)


def test_a_bad_row_refuses_the_whole_file_rather_than_skipping_it() -> None:
    """FR-BNK's own concern: a statement with one unparsable line and the
    rest silently imported would be a missing transaction nobody knows to
    look for.
    """
    csv = "date,amount\n2026-09-05,-12.50\nnot-a-date,-5.00\n2026-09-07,-3.00\n"
    with pytest.raises(InvalidRow) as excinfo:
        parse_statement_csv(csv)
    assert excinfo.value.row_number == 3

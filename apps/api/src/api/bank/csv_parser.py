"""A canonical bank-statement CSV, parsed into rows a bank account can import.

--- Why "canonical" and not "Rabobank's export" or "ING's export" ---

Every Dutch bank ships its own CSV dialect (column order, header names, sign
convention, date format, decimal separator). Auto-detecting all of them is a
real integration per bank - the same shape of work a PSD2/AISP adapter would
replace wholesale (see `api.bank.adapters` and ADR-084). What this parses
instead is ONE fixed shape:

    date,amount,counterparty_name,counterparty_iban,description

  date                  ISO 8601, YYYY-MM-DD
  amount                a decimal STRING (NFR-031), signed: positive is money
                         IN, negative is money OUT
  counterparty_name      optional
  counterparty_iban      optional
  description            optional

A person exporting from their own bank massages it into this shape first -
a spreadsheet formula or a find-and-replace, not a code change. That is the
honest P0 substitute for the adapter this module is not: it works today, for
every bank, at the cost of a manual step ADR-084 names explicitly rather than
hiding behind an "upload your bank statement" button that only some banks'
raw exports would actually satisfy.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

REQUIRED_COLUMNS = ("date", "amount")
OPTIONAL_COLUMNS = ("counterparty_name", "counterparty_iban", "description")


class CsvStatementError(Exception):
    """Base for every refusal this module raises."""


class MissingColumns(CsvStatementError):
    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(f"missing required column(s): {', '.join(missing)}")


class InvalidRow(CsvStatementError):
    def __init__(self, row_number: int, field: str, message: str) -> None:
        self.row_number = row_number
        self.field = field
        super().__init__(f"row {row_number}, {field}: {message}")


@dataclass(frozen=True, slots=True)
class StatementRow:
    booking_date: date
    amount: Decimal
    counterparty_name: str | None
    counterparty_iban: str | None
    description: str | None


def parse_statement_csv(text: str) -> list[StatementRow]:
    """The whole file, or a refusal naming the first bad row.

    Refused rather than skipped: a statement with one unparsable row and the
    rest silently imported is the exact failure mode a bank reconciliation
    screen must never have - a missing transaction that nobody knows to look
    for.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise MissingColumns(REQUIRED_COLUMNS)
    header = {name.strip().lower() for name in reader.fieldnames}
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise MissingColumns(missing)

    rows: list[StatementRow] = []
    for index, raw in enumerate(reader, start=2):  # row 1 is the header
        normalised = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}

        raw_date = normalised.get("date", "")
        try:
            booking_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise InvalidRow(
                index, "date", f"{raw_date!r} is not an ISO date (YYYY-MM-DD)"
            ) from exc

        raw_amount = normalised.get("amount", "")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as exc:
            raise InvalidRow(index, "amount", f"{raw_amount!r} is not a decimal amount") from exc
        if amount == 0:
            raise InvalidRow(index, "amount", "a statement line cannot be zero")

        rows.append(
            StatementRow(
                booking_date=booking_date,
                amount=amount,
                counterparty_name=normalised.get("counterparty_name") or None,
                counterparty_iban=normalised.get("counterparty_iban") or None,
                description=normalised.get("description") or None,
            )
        )
    return rows

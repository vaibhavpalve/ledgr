"""The bank statement a Dutch bank actually gives you, read as it is (FR-BNK-002, ADR-091).

    FR-BNK-002  File-based import of CAMT.053, MT940 and CSV as a fallback and for banks without
                API coverage.

Until this module the import took one made-up CSV shape (`date,amount,...`) that no bank
produces, so every customer had to rebuild their statement in a spreadsheet before the product
would read it. This reads the files banks export, unchanged:

    CAMT.053     ISO 20022 XML - every Dutch bank offers it (ING, Rabobank, ABN AMRO, bunq,
                 Knab, Triodos, ASN/SNS/RegioBank). The format to recommend.
    MT940        SWIFT text, with the SEPA-structured :86: field Dutch banks write
                 (/NAME/, /IBAN/, /REMI/, Rabobank's /CNTP/).
    CSV          ING, Rabobank, bunq, Knab and ABN AMRO's tab-separated TXT, recognised by their
                 headers (or, for ABN AMRO, their shape); and the original canonical CSV.

Every format ends as the same `StatementRow`s the import already stores, plus the account the file
is FOR when the file says (CAMT, MT940, ING, Rabobank, Knab, ABN AMRO do). The service refuses a
file for a different account: importing one bank's statement into another is a mistake nothing
downstream would catch.

--- What is refused rather than guessed ---

An unrecognised file, a row whose date or amount does not parse, and an XML document with a DTD
(an entity declaration in an uploaded file is an attack, not a statement) are refusals naming the
problem. A statement with one row silently dropped is the failure a reconciliation screen must
never have: a missing transaction nobody knows to look for. Amounts go from text to Decimal
without float (NFR-031).
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from api.bank.csv_parser import (
    CsvStatementError,
    InvalidRow,
    StatementRow,
    parse_statement_csv,
)


class UnknownStatementFormat(CsvStatementError):
    def __init__(self) -> None:
        super().__init__(
            "this file is not a statement format LEDGR reads. Export CAMT.053 (recommended), "
            "MT940, or your bank's CSV (ING, Rabobank, bunq, Knab, ABN AMRO)"
        )


class UnsafeStatement(CsvStatementError):
    pass


class AmbiguousAccount(CsvStatementError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    format: str
    #: The IBAN the file is for, normalised (no spaces, upper case), when the file names one.
    account_iban: str | None
    rows: list[StatementRow]


def normalise_iban(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", "", value).upper()
    return cleaned or None


# ---------------------------------------------------------------------------
# Amounts and dates
# ---------------------------------------------------------------------------


def parse_amount(text: str, row: int, *, decimal_comma: bool | None = None) -> Decimal:
    """ "+1.234,56", "-12,50", "12.50", "1234,5" - Dutch or international - as a Decimal."""
    value = text.strip().replace(" ", "").replace(chr(0xA0), "").replace("EUR", "")
    if value.startswith("+"):
        value = value[1:]
    if decimal_comma is None:
        # The last separator is the decimal one when both appear; a lone comma is decimal.
        last_dot, last_comma = value.rfind("."), value.rfind(",")
        decimal_comma = last_comma > last_dot
    value = value.replace(".", "").replace(",", ".") if decimal_comma else value.replace(",", "")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise InvalidRow(row, "amount", f"{text!r} is not an amount") from exc
    if not amount.is_finite():
        raise InvalidRow(row, "amount", f"{text!r} is not an amount")
    return amount.quantize(Decimal("0.01"))


def parse_date(text: str, row: int) -> date:
    value = text.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%d/%m/%Y", "%y%m%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise InvalidRow(row, "date", f"{text!r} is not a date")


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


# ---------------------------------------------------------------------------
# CAMT.053
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element | None, *path: str) -> ElementTree.Element | None:
    current = element
    for name in path:
        if current is None:
            return None
        current = next((c for c in current if _local(c.tag) == name), None)
    return current


def _children(element: ElementTree.Element | None, name: str) -> list[ElementTree.Element]:
    return [] if element is None else [c for c in element if _local(c.tag) == name]


def _text(element: ElementTree.Element | None, *path: str) -> str | None:
    found = _child(element, *path)
    return found.text.strip() if found is not None and found.text else None


def _party_name(party: ElementTree.Element | None) -> str | None:
    # camt.053.001.02 puts the name directly on the party; .001.08 wraps it in <Pty>.
    return _text(party, "Nm") or _text(party, "Pty", "Nm")


def parse_camt053(text: str) -> ParsedStatement:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise UnsafeStatement("an XML statement with a DTD or entity declaration is refused")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise UnknownStatementFormat() from exc

    report = _child(root, "BkToCstmrStmt")
    statements = _children(report, "Stmt")
    if not statements:
        raise UnknownStatementFormat()
    ibans = {normalise_iban(_text(s, "Acct", "Id", "IBAN")) for s in statements} - {None}
    if len(ibans) > 1:
        raise AmbiguousAccount(
            "this CAMT.053 file holds statements for more than one account; export one account "
            "per file"
        )

    rows: list[StatementRow] = []
    number = 0
    for statement in statements:
        for entry in _children(statement, "Ntry"):
            number += 1
            status = _text(entry, "Sts") or _text(entry, "Sts", "Cd")
            if status is not None and status.upper() not in {"BOOK"}:
                continue  # pending or informational: not a booking yet
            amount_text = _text(entry, "Amt")
            if amount_text is None:
                raise InvalidRow(number, "amount", "an entry has no amount")
            amount = parse_amount(amount_text, number, decimal_comma=False)
            credit = (_text(entry, "CdtDbtInd") or "CRDT").upper() == "CRDT"
            # CdtDbtInd is the direction the money moved on THIS account - for a reversal too
            # (a returned direct debit is CRDT with RvslInd true), so RvslInd flips nothing.
            if not credit:
                amount = -amount
            booked = (
                _text(entry, "BookgDt", "Dt")
                or (_text(entry, "BookgDt", "DtTm") or "")[:10]
                or _text(entry, "ValDt", "Dt")
            )
            if not booked:
                raise InvalidRow(number, "date", "an entry has no booking date")

            details = _child(entry, "NtryDtls", "TxDtls")
            parties = _child(details, "RltdPties")
            if credit:
                name = _party_name(_child(parties, "Dbtr"))
                iban = _text(parties, "DbtrAcct", "Id", "IBAN")
            else:
                name = _party_name(_child(parties, "Cdtr"))
                iban = _text(parties, "CdtrAcct", "Id", "IBAN")
            remittance = _child(details, "RmtInf")
            lines = [u.text.strip() for u in _children(remittance, "Ustrd") if u.text]
            reference = _text(remittance, "Strd", "CdtrRefInf", "Ref")
            description = " ".join(lines) or reference or _text(entry, "AddtlNtryInf")
            rows.append(
                StatementRow(
                    booking_date=parse_date(booked, number),
                    amount=amount,
                    counterparty_name=_optional(name),
                    counterparty_iban=normalise_iban(iban),
                    description=_optional(description),
                )
            )
    return ParsedStatement("camt.053", next(iter(ibans), None), rows)


# ---------------------------------------------------------------------------
# MT940
# ---------------------------------------------------------------------------

_MT940_61 = re.compile(
    r"^(?P<value>\d{6})(?P<entry>\d{4})?(?P<mark>RC|RD|C|D)[A-Z]?(?P<amount>\d+,\d{0,2})"
)
_STRUCTURED = (
    "TRTP",
    "IBAN",
    "BIC",
    "NAME",
    "REMI",
    "EREF",
    "MARF",
    "CSID",
    "ORDP",
    "BENM",
    "ULTC",
    "ULTD",
    "PURP",
    "CNTP",
    "ADDR",
    "ISDT",
    "RTRN",
)


def _structured(info: str, key: str) -> str | None:
    """A field of the SEPA-structured :86: (/KEY/value/NEXTKEY/...)."""
    keys = "|".join(_STRUCTURED)
    match = re.search(rf"/{key}/(.*?)(?=/(?:{keys})/|$)", info)
    return match.group(1).strip() if match else None


def parse_mt940(text: str) -> ParsedStatement:
    # Fields are ':tag:' at the start of a line; a value continues on the lines after it.
    fields: list[tuple[str, str]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^:(\d{2}[A-Z]?):(.*)$", line)
        if match:
            fields.append((match.group(1), match.group(2)))
        elif fields and line and not line.startswith("-}") and line.strip() != "-":
            tag, value = fields[-1]
            fields[-1] = (tag, value + "\n" + line)
    if not any(tag == "61" for tag, _ in fields):
        raise UnknownStatementFormat()

    ibans: set[str] = set()
    rows: list[StatementRow] = []
    pending: dict[str, object] | None = None
    number = 0

    def flush(info: str | None) -> None:
        nonlocal pending
        if pending is None:
            return
        flat = " ".join((info or "").split("\n"))
        name = _structured(flat, "NAME")
        iban = _structured(flat, "IBAN")
        counterparty = _structured(flat, "CNTP")
        if counterparty:  # Rabobank: /CNTP/<iban>/<bic>/<name>/<city>/
            parts = counterparty.split("/")
            iban = iban or (parts[0] if parts else None)
            name = name or (parts[2] if len(parts) > 2 else None)
        description = _structured(flat, "REMI") or (flat if not flat.startswith("/") else None)
        rows.append(
            StatementRow(
                booking_date=pending["date"],  # type: ignore[arg-type]
                amount=pending["amount"],  # type: ignore[arg-type]
                counterparty_name=_optional(name),
                counterparty_iban=normalise_iban(iban),
                description=_optional(description),
            )
        )
        pending = None

    for tag, value in fields:
        if tag == "25":
            account = value.strip().split("\n")[0]
            iban_match = re.search(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", account.replace(" ", ""))
            if iban_match:
                ibans.add(normalise_iban(iban_match.group(0).removesuffix("EUR")) or "")
        elif tag == "61":
            flush(None)
            number += 1
            match = _MT940_61.match(value.strip())
            if match is None:
                raise InvalidRow(number, "amount", f"{value[:40]!r} is not an MT940 :61: line")
            amount = parse_amount(match.group("amount"), number, decimal_comma=True)
            if match.group("mark") in {"D", "RC"}:
                amount = -amount
            try:
                # Always YYMMDD in MT940 - never through the generic parser, which would read
                # 260922 as a year.
                booked = datetime.strptime(match.group("value"), "%y%m%d").date()
            except ValueError as exc:
                raise InvalidRow(number, "date", f"{match.group('value')!r} is not a date") from exc
            pending = {"date": booked, "amount": amount}
        elif tag == "86":
            flush(value)
    flush(None)

    ibans.discard("")
    if len(ibans) > 1:
        raise AmbiguousAccount("this MT940 file holds more than one account; export one per file")
    return ParsedStatement("mt940", next(iter(ibans), None), rows)


# ---------------------------------------------------------------------------
# Bank CSVs
# ---------------------------------------------------------------------------


def _records(text: str) -> tuple[list[str], list[list[str]]]:
    sample = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=sample.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise UnknownStatementFormat()
    return [cell.strip().lower() for cell in rows[0]], rows


def _index(header: list[str], *names: str) -> int:
    for name in names:
        if name in header:
            return header.index(name)
    return -1


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if 0 <= index < len(row) else ""


def _single_iban(values: Iterable[str]) -> str | None:
    ibans = {normalise_iban(v) for v in values if v and v.strip()} - {None}
    if len(ibans) > 1:
        raise AmbiguousAccount("this export holds more than one account; export one per file")
    return next(iter(ibans), None)


def _parse_ing(header: list[str], rows: list[list[str]]) -> ParsedStatement:
    date_i = _index(header, "datum", "date")
    name_i = _index(header, "naam / omschrijving", "name / description")
    account_i = _index(header, "rekening", "account")
    other_i = _index(header, "tegenrekening", "counterparty")
    sign_i = _index(header, "af bij", "debit/credit")
    amount_i = _index(header, "bedrag (eur)", "amount (eur)")
    notes_i = _index(header, "mededelingen", "notifications")
    parsed: list[StatementRow] = []
    for number, row in enumerate(rows[1:], start=1):
        amount = parse_amount(_cell(row, amount_i), number, decimal_comma=True)
        if _cell(row, sign_i).lower() in {"af", "debit"}:
            amount = -amount
        parsed.append(
            StatementRow(
                booking_date=parse_date(_cell(row, date_i), number),
                amount=amount,
                counterparty_name=_optional(_cell(row, name_i)),
                counterparty_iban=normalise_iban(_cell(row, other_i)),
                description=_optional(_cell(row, notes_i)),
            )
        )
    return ParsedStatement("ing-csv", _single_iban(_cell(r, account_i) for r in rows[1:]), parsed)


def _parse_rabobank(header: list[str], rows: list[list[str]]) -> ParsedStatement:
    account_i = _index(header, "iban/bban")
    date_i = _index(header, "datum")
    amount_i = _index(header, "bedrag")
    other_i = _index(header, "tegenrekening iban/bban")
    name_i = _index(header, "naam tegenpartij")
    descriptions = [i for i, name in enumerate(header) if name.startswith("omschrijving-")]
    parsed: list[StatementRow] = []
    for number, row in enumerate(rows[1:], start=1):
        parsed.append(
            StatementRow(
                booking_date=parse_date(_cell(row, date_i), number),
                amount=parse_amount(_cell(row, amount_i), number, decimal_comma=True),
                counterparty_name=_optional(_cell(row, name_i)),
                counterparty_iban=normalise_iban(_cell(row, other_i)),
                description=_optional(" ".join(_cell(row, i) for i in descriptions)),
            )
        )
    return ParsedStatement(
        "rabobank-csv", _single_iban(_cell(r, account_i) for r in rows[1:]), parsed
    )


def _parse_bunq(header: list[str], rows: list[list[str]]) -> ParsedStatement:
    date_i = _index(header, "date", "datum")
    amount_i = _index(header, "amount", "bedrag")
    account_i = _index(header, "account", "rekening")
    other_i = _index(header, "counterparty", "tegenrekening")
    name_i = _index(header, "name", "naam")
    description_i = _index(header, "description", "omschrijving")
    parsed: list[StatementRow] = []
    for number, row in enumerate(rows[1:], start=1):
        parsed.append(
            StatementRow(
                booking_date=parse_date(_cell(row, date_i), number),
                amount=parse_amount(_cell(row, amount_i), number),
                counterparty_name=_optional(_cell(row, name_i)),
                counterparty_iban=normalise_iban(_cell(row, other_i)),
                description=_optional(_cell(row, description_i)),
            )
        )
    return ParsedStatement("bunq-csv", _single_iban(_cell(r, account_i) for r in rows[1:]), parsed)


def _parse_knab(header: list[str], rows: list[list[str]]) -> ParsedStatement:
    account_i = _index(header, "rekeningnummer")
    date_i = _index(header, "transactiedatum")
    sign_i = _index(header, "creditdebet")
    amount_i = _index(header, "bedrag")
    other_i = _index(header, "tegenrekeningnummer")
    name_i = _index(header, "tegenrekeninghouder")
    description_i = _index(header, "omschrijving")
    parsed: list[StatementRow] = []
    for number, row in enumerate(rows[1:], start=1):
        amount = parse_amount(_cell(row, amount_i), number, decimal_comma=True)
        if _cell(row, sign_i).upper() == "D":
            amount = -amount
        parsed.append(
            StatementRow(
                booking_date=parse_date(_cell(row, date_i), number),
                amount=amount,
                counterparty_name=_optional(_cell(row, name_i)),
                counterparty_iban=normalise_iban(_cell(row, other_i)),
                description=_optional(_cell(row, description_i)),
            )
        )
    return ParsedStatement("knab-csv", _single_iban(_cell(r, account_i) for r in rows[1:]), parsed)


def _parse_abn_txt(rows: list[list[str]]) -> ParsedStatement:
    """ABN AMRO's TXT: no header, tab-separated - account, currency, date, start balance, end
    balance, value date, amount, description (SEPA-structured, or a card payment line)."""
    parsed: list[StatementRow] = []
    for number, row in enumerate(rows, start=1):
        description = _cell(row, 7)
        flat = " ".join(description.split())
        name = _structured(flat, "NAME")
        iban = _structured(flat, "IBAN")
        parsed.append(
            StatementRow(
                booking_date=parse_date(_cell(row, 2), number),
                amount=parse_amount(_cell(row, 6), number, decimal_comma=True),
                counterparty_name=_optional(name),
                counterparty_iban=normalise_iban(iban),
                description=_optional(_structured(flat, "REMI") or flat),
            )
        )
    # The TXT names the account by number, not IBAN; nothing to check it against.
    return ParsedStatement("abnamro-txt", None, parsed)


def _parse_csv(text: str) -> ParsedStatement:
    header, rows = _records(text)
    names = set(header)
    if {"date", "amount"} <= names and "interest date" not in names:
        return ParsedStatement("csv", None, parse_statement_csv(text))
    if "af bij" in names or "debit/credit" in names:
        return _parse_ing(header, rows)
    if "iban/bban" in names and "naam tegenpartij" in names:
        return _parse_rabobank(header, rows)
    if "creditdebet" in names and "transactiedatum" in names:
        return _parse_knab(header, rows)
    if ("interest date" in names or "rentedatum" in names) and (
        "counterparty" in names or "tegenrekening" in names
    ):
        return _parse_bunq(header, rows)
    first = rows[0]
    if len(first) >= 8 and re.fullmatch(r"\d{8}", first[2].strip()) and first[1].strip() == "EUR":
        return _parse_abn_txt(rows)
    raise UnknownStatementFormat()


def parse_statement(text: str) -> ParsedStatement:
    """Whatever a Dutch bank exported, or a refusal saying what LEDGR does read."""
    body = text[1:] if text.startswith(chr(0xFEFF)) else text
    head = body.lstrip()[:2000]
    if head.startswith("<"):
        if "camt.053" not in head and "BkToCstmrStmt" not in body[:5000]:
            raise UnknownStatementFormat()
        return parse_camt053(body)
    if head.startswith("{1:") or re.search(r"^:20:", head, re.MULTILINE):
        return parse_mt940(body)
    return _parse_csv(body)

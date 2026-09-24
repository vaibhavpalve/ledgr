"""FR-BNK-002 / ADR-091: the statement files Dutch banks export, read unchanged.

The samples follow each bank's published export layout, cut down to the fields that matter; the
amounts and parties are invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.bank.csv_parser import InvalidRow
from api.bank.statement_formats import (
    AmbiguousAccount,
    UnknownStatementFormat,
    UnsafeStatement,
    parse_statement,
)

CAMT_02 = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>1</MsgId><CreDtTm>2026-09-24T06:00:00</CreDtTm></GrpHdr>
    <Stmt>
      <Id>1</Id>
      <Acct><Id><IBAN>NL91 ABNA 0417 1643 00</IBAN></Id></Acct>
      <Ntry>
        <Amt Ccy="EUR">1149.50</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-09-22</Dt></BookgDt>
        <NtryDtls><TxDtls>
          <RltdPties>
            <Dbtr><Nm>Hotel De Gouden Leeuw B.V.</Nm></Dbtr>
            <DbtrAcct><Id><IBAN>NL02RABO0123456789</IBAN></Id></DbtrAcct>
          </RltdPties>
          <RmtInf><Ustrd>Factuur 2026-0001</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">121.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-09-23</Dt></BookgDt>
        <NtryDtls><TxDtls>
          <RltdPties>
            <Cdtr><Nm>Staples Nederland</Nm></Cdtr>
            <CdtrAcct><Id><IBAN>NL44INGB0001234567</IBAN></Id></CdtrAcct>
          </RltdPties>
        </TxDtls></NtryDtls>
        <AddtlNtryInf>Kantoorartikelen</AddtlNtryInf>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">9.99</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Sts>PDNG</Sts>
        <BookgDt><Dt>2026-09-24</Dt></BookgDt>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""

CAMT_08 = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt>
<Acct><Id><IBAN>NL91ABNA0417164300</IBAN></Id></Acct>
<Ntry><Amt Ccy="EUR">50.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><DtTm>2026-09-20T10:15:00+02:00</DtTm></BookgDt>
<NtryDtls><TxDtls><RltdPties><Cdtr><Pty><Nm>Google Ireland Ltd</Nm></Pty></Cdtr></RltdPties>
<RmtInf><Strd><CdtrRefInf><Ref>ADS-99</Ref></CdtrRefInf></Strd></RmtInf></TxDtls></NtryDtls>
</Ntry></Stmt></BkToCstmrStmt></Document>
"""

MT940_ING = """{1:F01INGBNL2ABXXX0000000000}{2:I940INGBNL2AXXXN}{4:
:20:P260924000000001
:25:NL91INGB0001234567EUR
:28C:00000
:60F:C260921EUR1000,00
:61:2609220922C1149,50NTRFEREF//00000000001
:86:/TRTP/SEPA OVERBOEKING/IBAN/NL02RABO0123456789/BIC/RABONL2U/NAME/Hotel De
Gouden Leeuw B.V./REMI/Factuur 2026-0001/EREF/NOTPROVIDED
:61:2609230923D121,00NTRFNONREF//00000000002
:86:/TRTP/SEPA OVERBOEKING/IBAN/NL44INGB0001234567/NAME/Staples Nederland/REMI/Kantoorartikelen
:62F:C260923EUR2028,50
-}"""

MT940_RABO = """:20:940S260924
:25:NL02RABO0123456789EUR
:28C:1
:60F:C260921EUR0,00
:61:260922C250,00N541NONREF
:86:/CNTP/NL44INGB0001234567/INGBNL2A/Bakkerij Brood//REMI/USTD//Bestelling 12/
:62F:C260922EUR250,00
"""

ING_CSV = (
    '"Datum";"Naam / Omschrijving";"Rekening";"Tegenrekening";"Code";"Af Bij";'
    '"Bedrag (EUR)";"Mutatiesoort";"Mededelingen"\r\n'
    '"20260922";"Hotel De Gouden Leeuw B.V.";"NL91INGB0001234567";"NL02RABO0123456789";"OV";'
    '"Bij";"1149,50";"Overschrijving";"Factuur 2026-0001"\r\n'
    '"20260923";"Staples Nederland";"NL91INGB0001234567";"NL44INGB0001234567";"IC";"Af";'
    '"1.121,00";"Incasso";"Kantoorartikelen"\r\n'
)

RABO_CSV = (
    '"IBAN/BBAN","Munt","BIC","Volgnr","Datum","Rentedatum","Bedrag","Saldo na trn",'
    '"Tegenrekening IBAN/BBAN","Naam tegenpartij","Omschrijving-1","Omschrijving-2"\n'
    '"NL02RABO0123456789","EUR","RABONL2U","000000000000001","2026-09-22","2026-09-22",'
    '"+1149,50","+1149,50","NL91ABNA0417164300","Hotel De Gouden Leeuw","Factuur","2026-0001"\n'
    '"NL02RABO0123456789","EUR","RABONL2U","000000000000002","2026-09-23","2026-09-23",'
    '"-121,00","+1028,50","NL44INGB0001234567","Staples","Kantoor",""\n'
)

BUNQ_CSV = (
    '"Date";"Interest Date";"Amount";"Account";"Counterparty";"Name";"Description"\n'
    '"2026-09-22";"2026-09-22";"1.149,50";"NL00BUNQ2025000000";"NL02RABO0123456789";'
    '"Hotel";"Factuur"\n'
    '"2026-09-23";"2026-09-23";"-121,00";"NL00BUNQ2025000000";"";"Staples";"Pin"\n'
)

KNAB_CSV = (
    '"Rekeningnummer";"Transactiedatum";"Valutacode";"CreditDebet";"Bedrag";'
    '"Tegenrekeningnummer";"Tegenrekeninghouder";"Valutadatum";"Betaalwijze";"Omschrijving"\n'
    '"NL00KNAB0000000001";"22-09-2026";"EUR";"C";"1149,50";"NL02RABO0123456789";"Hotel";'
    '"22-09-2026";"Overboeking";"Factuur"\n'
    '"NL00KNAB0000000001";"23-09-2026";"EUR";"D";"121,00";"";"Staples";"23-09-2026";'
    '"Betaalautomaat";"Pin"\n'
)

ABN_TXT = (
    "417164300\tEUR\t20260922\t0,00\t1149,50\t20260922\t1149,50\t"
    "/TRTP/SEPA OVERBOEKING/IBAN/NL02RABO0123456789/BIC/RABONL2U/NAME/Hotel/REMI/Factuur 1/\n"
    "417164300\tEUR\t20260923\t1149,50\t1028,50\t20260923\t-121,00\t"
    "BEA   NR:XXX   23.09.26/14.02 Staples Amsterdam\n"
)


def _summary(text: str) -> tuple[str, str | None, list[tuple[date, Decimal, str | None]]]:
    parsed = parse_statement(text)
    return (
        parsed.format,
        parsed.account_iban,
        [(r.booking_date, r.amount, r.counterparty_name) for r in parsed.rows],
    )


def test_camt_053_001_02() -> None:
    fmt, iban, rows = _summary(CAMT_02)
    assert (fmt, iban) == ("camt.053", "NL91ABNA0417164300")
    # The pending entry is not a booking yet.
    assert rows == [
        (date(2026, 9, 22), Decimal("1149.50"), "Hotel De Gouden Leeuw B.V."),
        (date(2026, 9, 23), Decimal("-121.00"), "Staples Nederland"),
    ]
    parsed = parse_statement(CAMT_02)
    assert parsed.rows[0].counterparty_iban == "NL02RABO0123456789"
    assert parsed.rows[0].description == "Factuur 2026-0001"
    assert parsed.rows[1].description == "Kantoorartikelen"


def test_camt_053_001_08_party_wrapper_status_code_and_datetime() -> None:
    fmt, _iban, rows = _summary(CAMT_08)
    assert rows == [(date(2026, 9, 20), Decimal("-50.00"), "Google Ireland Ltd")]
    assert parse_statement(CAMT_08).rows[0].description == "ADS-99"


def test_camt_with_a_dtd_is_refused() -> None:
    evil = CAMT_08.replace(
        '<?xml version="1.0"?>',
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x "xxxxxxxxxx">]>',
    )
    with pytest.raises(UnsafeStatement):
        parse_statement(evil)


def test_camt_with_two_accounts_is_refused() -> None:
    two = CAMT_02.replace(
        "</Stmt>", "</Stmt><Stmt><Acct><Id><IBAN>NL02RABO0123456789</IBAN></Id></Acct></Stmt>", 1
    )
    with pytest.raises(AmbiguousAccount):
        parse_statement(two)


def test_mt940_ing_with_a_continued_86_line() -> None:
    fmt, iban, rows = _summary(MT940_ING)
    assert (fmt, iban) == ("mt940", "NL91INGB0001234567")
    assert rows == [
        (date(2026, 9, 22), Decimal("1149.50"), "Hotel De Gouden Leeuw B.V."),
        (date(2026, 9, 23), Decimal("-121.00"), "Staples Nederland"),
    ]
    parsed = parse_statement(MT940_ING)
    assert parsed.rows[0].counterparty_iban == "NL02RABO0123456789"
    assert parsed.rows[0].description == "Factuur 2026-0001"


def test_mt940_rabobank_counterparty_field() -> None:
    fmt, iban, rows = _summary(MT940_RABO)
    assert (fmt, iban) == ("mt940", "NL02RABO0123456789")
    assert rows == [(date(2026, 9, 22), Decimal("250.00"), "Bakkerij Brood")]
    assert parse_statement(MT940_RABO).rows[0].counterparty_iban == "NL44INGB0001234567"


@pytest.mark.parametrize(
    ("text", "fmt", "iban"),
    [
        (ING_CSV, "ing-csv", "NL91INGB0001234567"),
        (RABO_CSV, "rabobank-csv", "NL02RABO0123456789"),
        (BUNQ_CSV, "bunq-csv", "NL00BUNQ2025000000"),
        (KNAB_CSV, "knab-csv", "NL00KNAB0000000001"),
        (ABN_TXT, "abnamro-txt", None),
    ],
)
def test_bank_csv_exports(text: str, fmt: str, iban: str | None) -> None:
    got_fmt, got_iban, rows = _summary(text)
    assert (got_fmt, got_iban) == (fmt, iban)
    assert [r[0] for r in rows] == [date(2026, 9, 22), date(2026, 9, 23)]
    assert rows[0][1] == Decimal("1149.50")
    # ING's second row is 1.121,00 (a thousands separator); the rest are 121,00.
    assert rows[1][1] in {Decimal("-121.00"), Decimal("-1121.00")}


def test_the_canonical_csv_still_reads() -> None:
    fmt, iban, rows = _summary("date,amount,counterparty_name\n2026-09-22,10.50,Klant\n")
    assert (fmt, iban, rows) == ("csv", None, [(date(2026, 9, 22), Decimal("10.50"), "Klant")])


def test_a_bom_is_ignored() -> None:
    assert parse_statement(chr(0xFEFF) + ING_CSV).format == "ing-csv"


def test_an_unknown_file_names_what_is_read() -> None:
    with pytest.raises(UnknownStatementFormat) as refused:
        parse_statement("naam;bedrag\nx;1\n")
    assert "CAMT.053" in str(refused.value)


def test_a_bad_row_is_refused_not_skipped() -> None:
    broken = ING_CSV.replace('"1149,50"', '"veel"')
    with pytest.raises(InvalidRow) as refused:
        parse_statement(broken)
    assert refused.value.row_number == 1

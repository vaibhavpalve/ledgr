"""SI-09's rules and the pain.008 file - api.invoicing.sepa (ADR-077)."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal

import pytest

from api.iban import parse_creditor_id as parse_creditor_id_from_iban
from api.invoicing.sepa import (
    MANDATE_LAPSE_MONTHS,
    CollectionLine,
    Creditor,
    MandateFacts,
    MandateScheme,
    SepaInvalid,
    SequenceKind,
    SequenceType,
    SkipReason,
    build_pain008,
    choose_mandate,
    mandate_lapsed,
    parse_creditor_id,
    sanitise,
    sequence_type_for,
    validate_collection_date,
    validate_mandate_reference,
)

D = Decimal
TODAY = date(2026, 9, 21)  # a Monday
NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"}
CREDITOR = Creditor("Bakker Consultancy B.V.", "NL91ABNA0417164300", "DE98ZZZ09999999999")


def facts(**kw: object) -> MandateFacts:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        is_active=True,
        scheme=MandateScheme.CORE,
        kind=SequenceKind.RECURRING,
        signed_on=date(2026, 1, 10),
        last_collected_on=None,
        used=False,
    )
    defaults.update(kw)
    return MandateFacts(**defaults)  # type: ignore[arg-type]


def line(**kw: object) -> CollectionLine:
    defaults: dict[str, object] = dict(
        end_to_end_id="2026-1",
        amount=D("121.00"),
        sequence_type=SequenceType.FIRST,
        scheme=MandateScheme.CORE,
        mandate_reference="M-0001",
        signed_on=date(2026, 1, 10),
        debtor_name="De Vries Holding B.V.",
        debtor_iban="NL02ABNA0123456789",
        debtor_bic=None,
        remittance="Factuur 2026-1",
    )
    defaults.update(kw)
    return CollectionLine(**defaults)  # type: ignore[arg-type]


def build(lines: list[CollectionLine], **kw: object) -> ET.Element:
    args: dict[str, object] = dict(
        message_id="LEDGR-20260921100000-ABC123",
        created_at=datetime(2026, 9, 21, 10, 0, 0, 123456),
        creditor=CREDITOR,
        collection_date=date(2026, 9, 24),
        lines=lines,
    )
    args.update(kw)
    return ET.fromstring(build_pain008(**args))  # type: ignore[arg-type]


def text(node: ET.Element, path: str) -> str:
    found = node.find(path, NS)
    assert found is not None, path
    assert found.text is not None, path
    return found.text


# --- identifiers ----------------------------------------
def test_the_epc_example_creditor_id_is_valid_and_a_wrong_check_digit_is_not() -> None:
    assert parse_creditor_id("DE98ZZZ09999999999") == "DE98ZZZ09999999999"
    assert parse_creditor_id("de98 zzz 0999 9999 999") == "DE98ZZZ09999999999"  # as typed
    assert parse_creditor_id("DE99ZZZ09999999999") is None
    assert parse_creditor_id("DE98ZZZ") is None  # no national part
    assert parse_creditor_id("") is None and parse_creditor_id(None) is None
    assert parse_creditor_id_from_iban is parse_creditor_id  # one implementation


def test_the_business_code_is_not_part_of_the_check() -> None:
    """That is what separates a creditor id from an IBAN: changing ZZZ leaves it valid."""
    assert parse_creditor_id("DE98ABC09999999999") == "DE98ABC09999999999"


@pytest.mark.parametrize(
    "ok", ["M-0001", "LEDGR-A1B2C3D4E5F6", "a" * 35, "K.1/2:3 (x)".replace(" ", "")]
)
def test_valid_mandate_references(ok: str) -> None:
    assert validate_mandate_reference(ok) == ok


@pytest.mark.parametrize("bad", ["", "a" * 36, "with space", "ümlaut", "semi;colon", "a\nb"])
def test_invalid_mandate_references_are_refused_not_altered(bad: str) -> None:
    with pytest.raises(SepaInvalid):
        validate_mandate_reference(bad)


def test_sanitise_transliterates_strips_and_truncates() -> None:
    assert sanitise("Café Zoë & Zn. B.V.", 70) == "Cafe Zoe Zn. B.V."
    assert sanitise("  a   b  ", 70) == "a b"
    assert sanitise("x" * 100, 10) == "x" * 10
    assert sanitise("€ ;<>", 10) == ""


# --- mandates ----------------------------------------
def test_a_mandate_lapses_after_36_months_from_its_last_use_or_signature() -> None:
    assert MANDATE_LAPSE_MONTHS == 36
    assert not mandate_lapsed(date(2023, 9, 21), None, TODAY)  # exactly 36 months: still valid
    assert mandate_lapsed(date(2023, 9, 20), None, TODAY)
    # A later collection restarts the clock.
    assert not mandate_lapsed(date(2020, 1, 1), date(2025, 1, 1), TODAY)
    assert mandate_lapsed(date(2020, 1, 1), date(2023, 1, 1), TODAY)


def test_no_active_mandate_means_no_mandate() -> None:
    assert choose_mandate([], TODAY) is SkipReason.NO_MANDATE
    assert choose_mandate([facts(is_active=False)], TODAY) is SkipReason.NO_MANDATE


def test_only_lapsed_mandates_means_lapsed() -> None:
    assert choose_mandate([facts(signed_on=date(2020, 1, 1))], TODAY) is SkipReason.MANDATE_LAPSED


def test_the_most_recently_signed_usable_mandate_wins() -> None:
    old = facts(signed_on=date(2026, 1, 1))
    new = facts(signed_on=date(2026, 6, 1))
    lapsed = facts(signed_on=date(2020, 1, 1))
    assert choose_mandate([old, lapsed, new], TODAY) is new


def test_a_used_one_off_mandate_cannot_be_collected_on_again() -> None:
    assert (
        choose_mandate([facts(kind=SequenceKind.ONE_OFF, used=True)], TODAY)
        is SkipReason.MANDATE_LAPSED
    )
    unused = facts(kind=SequenceKind.ONE_OFF)
    assert choose_mandate([unused], TODAY) is unused


def test_first_then_recurring_and_one_off_is_always_one_off() -> None:
    assert sequence_type_for(SequenceKind.RECURRING, used=False) is SequenceType.FIRST
    assert sequence_type_for(SequenceKind.RECURRING, used=True) is SequenceType.RECURRING
    assert sequence_type_for(SequenceKind.ONE_OFF, used=False) is SequenceType.ONE_OFF
    assert sequence_type_for(SequenceKind.ONE_OFF, used=True) is SequenceType.ONE_OFF


# --- the collection date ----------------------------------------
def test_a_collection_date_is_a_future_business_day_within_reach() -> None:
    validate_collection_date(date(2026, 9, 22), TODAY)  # tomorrow, a Tuesday
    validate_collection_date(date(2026, 11, 20), TODAY)  # 60 days, a Friday
    for bad in (TODAY, date(2026, 9, 20), date(2026, 9, 26), date(2026, 11, 23)):
        with pytest.raises(SepaInvalid):
            validate_collection_date(bad, TODAY)  # today, past, a Saturday, 63 days


# --- the file ----------------------------------------
def test_the_group_header_counts_and_sums_everything() -> None:
    root = build([line(), line(end_to_end_id="2026-2", amount=D("79.95"))])
    header = "p:CstmrDrctDbtInitn/p:GrpHdr"
    assert text(root, f"{header}/p:MsgId") == "LEDGR-20260921100000-ABC123"
    assert text(root, f"{header}/p:CreDtTm") == "2026-09-21T10:00:00"  # no microseconds
    assert text(root, f"{header}/p:NbOfTxs") == "2"
    assert text(root, f"{header}/p:CtrlSum") == "200.95"
    assert text(root, f"{header}/p:InitgPty/p:Nm") == "Bakker Consultancy B.V."


def test_one_payment_block_carries_the_creditor_and_the_scheme() -> None:
    root = build([line()])
    block = "p:CstmrDrctDbtInitn/p:PmtInf"
    assert text(root, f"{block}/p:PmtMtd") == "DD"
    assert text(root, f"{block}/p:PmtTpInf/p:SvcLvl/p:Cd") == "SEPA"
    assert text(root, f"{block}/p:PmtTpInf/p:LclInstrm/p:Cd") == "CORE"
    assert text(root, f"{block}/p:PmtTpInf/p:SeqTp") == "FRST"
    assert text(root, f"{block}/p:ReqdColltnDt") == "2026-09-24"
    assert text(root, f"{block}/p:CdtrAcct/p:Id/p:IBAN") == "NL91ABNA0417164300"
    assert text(root, f"{block}/p:ChrgBr") == "SLEV"
    assert text(root, f"{block}/p:CdtrSchmeId/p:Id/p:PrvtId/p:Othr/p:Id") == "DE98ZZZ09999999999"
    assert text(root, f"{block}/p:CdtrSchmeId/p:Id/p:PrvtId/p:Othr/p:SchmeNm/p:Prtry") == "SEPA"


def test_a_transaction_names_its_mandate_debtor_and_amount() -> None:
    root = build([line(debtor_bic="ABNANL2A")])
    tx = "p:CstmrDrctDbtInitn/p:PmtInf/p:DrctDbtTxInf"
    assert text(root, f"{tx}/p:PmtId/p:EndToEndId") == "2026-1"
    amount = root.find(f"{tx}/p:InstdAmt", NS)
    assert amount is not None and amount.text == "121.00" and amount.get("Ccy") == "EUR"
    assert text(root, f"{tx}/p:DrctDbtTx/p:MndtRltdInf/p:MndtId") == "M-0001"
    assert text(root, f"{tx}/p:DrctDbtTx/p:MndtRltdInf/p:DtOfSgntr") == "2026-01-10"
    assert text(root, f"{tx}/p:DbtrAgt/p:FinInstnId/p:BIC") == "ABNANL2A"
    assert text(root, f"{tx}/p:Dbtr/p:Nm") == "De Vries Holding B.V."
    assert text(root, f"{tx}/p:DbtrAcct/p:Id/p:IBAN") == "NL02ABNA0123456789"
    assert text(root, f"{tx}/p:RmtInf/p:Ustrd") == "Factuur 2026-1"


def test_no_bic_is_notprovided_not_omitted() -> None:
    root = build([line()])
    tx = "p:CstmrDrctDbtInitn/p:PmtInf/p:DrctDbtTxInf"
    assert text(root, f"{tx}/p:DbtrAgt/p:FinInstnId/p:Othr/p:Id") == "NOTPROVIDED"
    assert text(root, "p:CstmrDrctDbtInitn/p:PmtInf/p:CdtrAgt/p:FinInstnId/p:Othr/p:Id") == (
        "NOTPROVIDED"
    )


def test_elements_appear_in_the_order_the_schema_requires() -> None:
    """A bank validates against the XSD, where sequence matters and a wrong order refuses the
    whole file. Order is checked against pain.008.001.02's own."""
    root = build([line()])
    block = root.find("p:CstmrDrctDbtInitn/p:PmtInf", NS)
    assert block is not None
    tags = [child.tag.split("}")[1] for child in block]
    assert tags == [
        "PmtInfId", "PmtMtd", "NbOfTxs", "CtrlSum", "PmtTpInf", "ReqdColltnDt", "Cdtr",
        "CdtrAcct", "CdtrAgt", "ChrgBr", "CdtrSchmeId", "DrctDbtTxInf",
    ]  # fmt: skip
    tx = block.find("p:DrctDbtTxInf", NS)
    assert tx is not None
    assert [c.tag.split("}")[1] for c in tx] == [
        "PmtId", "InstdAmt", "DrctDbtTx", "DbtrAgt", "Dbtr", "DbtrAcct", "RmtInf",
    ]  # fmt: skip


def test_scheme_and_sequence_split_the_file_into_blocks_with_their_own_totals() -> None:
    root = build(
        [
            line(end_to_end_id="a", amount=D("10.00")),
            line(end_to_end_id="b", amount=D("20.00"), sequence_type=SequenceType.RECURRING),
            line(end_to_end_id="c", amount=D("5.50"), sequence_type=SequenceType.RECURRING),
            line(end_to_end_id="d", amount=D("1.00"), scheme=MandateScheme.B2B),
        ]
    )
    blocks = root.findall("p:CstmrDrctDbtInitn/p:PmtInf", NS)
    summary = {
        (text(b, "p:PmtTpInf/p:LclInstrm/p:Cd"), text(b, "p:PmtTpInf/p:SeqTp")): (
            text(b, "p:NbOfTxs"),
            text(b, "p:CtrlSum"),
        )
        for b in blocks
    }
    assert summary == {
        ("CORE", "FRST"): ("1", "10.00"),
        ("CORE", "RCUR"): ("2", "25.50"),
        ("B2B", "FRST"): ("1", "1.00"),
    }
    assert len({text(b, "p:PmtInfId") for b in blocks}) == 3  # each block has its own id
    assert text(root, "p:CstmrDrctDbtInitn/p:GrpHdr/p:CtrlSum") == "36.50"


def test_names_are_sanitised_and_markup_is_escaped_so_the_document_stays_well_formed() -> None:
    root = build([line(debtor_name="Zoë <b>&</b> Zn", remittance="Factuur & <x>")])
    tx = "p:CstmrDrctDbtInitn/p:PmtInf/p:DrctDbtTxInf"
    assert text(root, f"{tx}/p:Dbtr/p:Nm") == "Zoe b /b Zn"
    assert "<" not in text(root, f"{tx}/p:RmtInf/p:Ustrd")


def test_an_empty_file_duplicate_ids_or_a_float_are_refused() -> None:
    with pytest.raises(SepaInvalid, match="at least one"):
        build_pain008(
            message_id="M",
            created_at=datetime(2026, 9, 21),
            creditor=CREDITOR,
            collection_date=date(2026, 9, 24),
            lines=[],
        )
    with pytest.raises(SepaInvalid, match="unique"):
        build([line(), line()])
    with pytest.raises(SepaInvalid, match="float"):
        build([line(amount=121.0)])  # type: ignore[arg-type]
    with pytest.raises(SepaInvalid, match="positive"):
        build([line(amount=D("0.00"))])
    with pytest.raises(SepaInvalid, match="two decimals"):
        build([line(amount=D("1.005"))])

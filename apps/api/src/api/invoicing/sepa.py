"""SEPA direct debit rules and the pain.008 file - FR-AR-011, SI-09 (ADR-077).

Pure: no I/O and no clock. The service supplies the facts and today's date.

--- What LEDGR does and does not do ---

It keeps the mandates and GENERATES the file (`build_pain008`); the business uploads it to its
own bank. Nothing here talks to a bank, and the outcome of each collection is recorded by a
person once the bank's report is in. The one thing that has to be exactly right is the file:
an ill-formed pain.008 is rejected whole by the bank, and a well-formed one that names the
wrong mandate debits somebody's account.

--- Mandate rules that matter ---

* The reference is unique per creditor, at most 35 characters, in the SEPA character set.
* A mandate lapses when it has not been collected on for 36 months (measured from the last
  collection, or from signing if it was never used): collecting on a lapsed one is a
  collection without consent.
* First / recurring is DERIVED: FRST while the mandate has no submitted or collected item,
  RCUR afterwards, OOFF for a one-off mandate (which can be collected on once).
* A revoked mandate is never collected on.

--- The file ---

pain.008.001.02, the version every Dutch bank accepts. One PmtInf block per (scheme, sequence
type) because both are attributes of the block. Text is transliterated to the SEPA character
set (no accents, a limited punctuation set) and truncated to the schema's lengths, because a
character the schema does not allow gets the whole file refused.

--- Legal / scheme data flagged for review (ADR-077) ---

The 36-month lapse, the minimum lead time of one TARGET business day for both schemes, and the
scheme names are the author's understanding of the EPC rulebooks and are NOT verified against
the current versions. TARGET holidays are not modelled: a weekday is treated as a business day.
"""

from __future__ import annotations

import enum
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

from api.iban import parse_creditor_id
from api.invoicing.bad_debt import add_months

__all__ = [
    "MANDATE_LAPSE_MONTHS",
    "MIN_LEAD_DAYS",
    "MAX_LEAD_DAYS",
    "Creditor",
    "CollectionLine",
    "MandateFacts",
    "MandateScheme",
    "SequenceKind",
    "SequenceType",
    "SepaInvalid",
    "SkipReason",
    "build_pain008",
    "choose_mandate",
    "mandate_lapsed",
    "parse_creditor_id",
    "sanitise",
    "sequence_type_for",
    "validate_collection_date",
    "validate_mandate_reference",
]

#: A mandate not collected on for this many months lapses. EPC rulebook; flagged for review.
MANDATE_LAPSE_MONTHS = 36
#: The collection date must be at least this many days ahead (D-1) and not absurdly far.
MIN_LEAD_DAYS = 1
MAX_LEAD_DAYS = 60

_REFERENCE = re.compile(r"^[A-Za-z0-9+?/:().,'-]{1,35}$")
_DISALLOWED = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")


class SepaInvalid(ValueError):
    """Something a mandate or a file cannot carry."""


class MandateScheme(enum.Enum):
    CORE = "core"
    B2B = "b2b"

    @property
    def code(self) -> str:
        return self.name  # CORE / B2B, the LclInstrm/Cd value


class SequenceKind(enum.Enum):
    RECURRING = "recurring"
    ONE_OFF = "one_off"


class SequenceType(enum.Enum):
    FIRST = "FRST"
    RECURRING = "RCUR"
    ONE_OFF = "OOFF"


class SkipReason(enum.Enum):
    """Why an invoice was left out of a batch - reported, never silently dropped."""

    NOT_FOUND = "not_found"
    NOT_COLLECTABLE = "not_collectable"  # a draft or a credit note
    NOTHING_OUTSTANDING = "nothing_outstanding"
    ALREADY_IN_COLLECTION = "already_in_collection"
    NOT_YET_DUE = "not_yet_due"
    NO_MANDATE = "no_mandate"
    MANDATE_LAPSED = "mandate_lapsed"


# ---------------------------------------------------------------------------
# Text and identifiers
# ---------------------------------------------------------------------------


def sanitise(value: str, max_length: int) -> str:
    """`value` in the SEPA character set, at most `max_length` long.

    Accents are stripped rather than dropped (é -> e), anything else outside the set becomes a
    space, runs of spaces collapse. Truncation is last so a multi-byte name cannot be cut inside
    a character.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _DISALLOWED.sub(" ", stripped)
    return " ".join(cleaned.split())[:max_length].strip()


def validate_mandate_reference(reference: str) -> str:
    """The reference as given, or `SepaInvalid`. Not sanitised: the debtor's bank matches on it
    byte for byte, so silently altering it would create a mandate nobody signed."""
    if not _REFERENCE.match(reference or ""):
        raise SepaInvalid(
            "a mandate reference is 1-35 characters from A-Z a-z 0-9 and + ? / : ( ) . , ' -"
        )
    return reference


# ---------------------------------------------------------------------------
# Mandates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MandateFacts:
    """What choosing a mandate needs, and nothing more."""

    id: uuid.UUID
    is_active: bool
    scheme: MandateScheme
    kind: SequenceKind
    signed_on: date
    last_collected_on: date | None
    #: A submitted or collected collection exists on it: the next one is not the first.
    used: bool


def mandate_lapsed(signed_on: date, last_collected_on: date | None, today: date) -> bool:
    """Unused for `MANDATE_LAPSE_MONTHS`, counted from the last collection or from signing."""
    return today > add_months(last_collected_on or signed_on, MANDATE_LAPSE_MONTHS)


def choose_mandate(mandates: Sequence[MandateFacts], today: date) -> MandateFacts | SkipReason:
    """The mandate to collect on, or the reason there is none.

    Active, not lapsed, and - for a one-off - not yet used. Several may qualify (a customer who
    signed a new one after changing bank): the most recently signed wins.
    """
    active = [m for m in mandates if m.is_active]
    if not active:
        return SkipReason.NO_MANDATE
    usable = [
        m
        for m in active
        if not mandate_lapsed(m.signed_on, m.last_collected_on, today)
        and not (m.kind is SequenceKind.ONE_OFF and m.used)
    ]
    if not usable:
        return SkipReason.MANDATE_LAPSED
    return max(usable, key=lambda m: m.signed_on)


def sequence_type_for(kind: SequenceKind, used: bool) -> SequenceType:
    if kind is SequenceKind.ONE_OFF:
        return SequenceType.ONE_OFF
    return SequenceType.RECURRING if used else SequenceType.FIRST


def validate_collection_date(collection_date: date, today: date) -> None:
    """A collection date is a future TARGET business day, not absurdly far ahead."""
    if collection_date < today + timedelta(days=MIN_LEAD_DAYS):
        raise SepaInvalid(
            f"the collection date must be at least {MIN_LEAD_DAYS} day ahead of {today}"
        )
    if collection_date > today + timedelta(days=MAX_LEAD_DAYS):
        raise SepaInvalid(f"the collection date is more than {MAX_LEAD_DAYS} days ahead")
    if collection_date.weekday() >= 5:
        raise SepaInvalid(f"{collection_date} is a weekend, not a TARGET business day")


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Creditor:
    name: str
    iban: str
    creditor_id: str


@dataclass(frozen=True, slots=True)
class CollectionLine:
    end_to_end_id: str
    amount: Decimal
    sequence_type: SequenceType
    scheme: MandateScheme
    mandate_reference: str
    signed_on: date
    debtor_name: str
    debtor_iban: str
    debtor_bic: str | None
    remittance: str


_NS = "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"


def _amount(value: Decimal) -> str:
    if isinstance(value, float) or not isinstance(value, Decimal):
        raise SepaInvalid("amounts must be Decimal, not float (NFR-031)")
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        raise SepaInvalid(f"{value} is not a positive amount with at most two decimals")
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _agent(tag: str, bic: str | None) -> str:
    inner = f"<BIC>{escape(bic)}</BIC>" if bic else "<Othr><Id>NOTPROVIDED</Id></Othr>"
    return f"<{tag}><FinInstnId>{inner}</FinInstnId></{tag}>"


def build_pain008(
    *,
    message_id: str,
    created_at: datetime,
    creditor: Creditor,
    collection_date: date,
    lines: Sequence[CollectionLine],
) -> str:
    """The pain.008.001.02 document for `lines`, all collected on `collection_date`."""
    if not lines:
        raise SepaInvalid("a direct debit file needs at least one collection")
    if not 1 <= len(message_id) <= 35:
        raise SepaInvalid("the message id is 1-35 characters")
    ids = [line.end_to_end_id for line in lines]
    if len(set(ids)) != len(ids):
        raise SepaInvalid("end-to-end ids must be unique within a file")

    for line in lines:
        _amount(line.amount)  # a float or a bad scale is refused before anything is summed
    total = sum((line.amount for line in lines), Decimal(0))
    groups: dict[tuple[str, str], list[CollectionLine]] = defaultdict(list)
    for line in lines:
        groups[(line.scheme.code, line.sequence_type.value)].append(line)

    creditor_name = sanitise(creditor.name, 70)
    if not creditor_name:
        raise SepaInvalid("the creditor needs a name")

    blocks: list[str] = []
    for index, ((scheme, sequence), members) in enumerate(sorted(groups.items()), start=1):
        block_total = sum((line.amount for line in members), Decimal(0))
        transactions = "".join(_transaction(line) for line in members)
        blocks.append(
            "<PmtInf>"
            f"<PmtInfId>{escape(message_id[:30])}-{index}</PmtInfId>"
            "<PmtMtd>DD</PmtMtd>"
            f"<NbOfTxs>{len(members)}</NbOfTxs>"
            f"<CtrlSum>{_amount(block_total)}</CtrlSum>"
            "<PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl>"
            f"<LclInstrm><Cd>{scheme}</Cd></LclInstrm><SeqTp>{sequence}</SeqTp></PmtTpInf>"
            f"<ReqdColltnDt>{collection_date.isoformat()}</ReqdColltnDt>"
            f"<Cdtr><Nm>{escape(creditor_name)}</Nm></Cdtr>"
            f"<CdtrAcct><Id><IBAN>{escape(creditor.iban)}</IBAN></Id></CdtrAcct>"
            f"{_agent('CdtrAgt', None)}"
            "<ChrgBr>SLEV</ChrgBr>"
            "<CdtrSchmeId><Id><PrvtId><Othr>"
            f"<Id>{escape(creditor.creditor_id)}</Id>"
            "<SchmeNm><Prtry>SEPA</Prtry></SchmeNm>"
            "</Othr></PrvtId></Id></CdtrSchmeId>"
            f"{transactions}"
            "</PmtInf>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Document xmlns="{_NS}"><CstmrDrctDbtInitn>'
        "<GrpHdr>"
        f"<MsgId>{escape(message_id)}</MsgId>"
        f"<CreDtTm>{created_at.replace(microsecond=0, tzinfo=None).isoformat()}</CreDtTm>"
        f"<NbOfTxs>{len(lines)}</NbOfTxs>"
        f"<CtrlSum>{_amount(total)}</CtrlSum>"
        f"<InitgPty><Nm>{escape(creditor_name)}</Nm></InitgPty>"
        "</GrpHdr>"
        f"{''.join(blocks)}"
        "</CstmrDrctDbtInitn></Document>"
    )


def _transaction(line: CollectionLine) -> str:
    debtor = sanitise(line.debtor_name, 70)
    if not debtor:
        raise SepaInvalid("every debtor needs a name")
    remittance = sanitise(line.remittance, 140)
    remittance_xml = f"<RmtInf><Ustrd>{escape(remittance)}</Ustrd></RmtInf>" if remittance else ""
    return (
        "<DrctDbtTxInf>"
        f"<PmtId><EndToEndId>{escape(line.end_to_end_id)}</EndToEndId></PmtId>"
        f'<InstdAmt Ccy="EUR">{_amount(line.amount)}</InstdAmt>'
        "<DrctDbtTx><MndtRltdInf>"
        f"<MndtId>{escape(line.mandate_reference)}</MndtId>"
        f"<DtOfSgntr>{line.signed_on.isoformat()}</DtOfSgntr>"
        "</MndtRltdInf></DrctDbtTx>"
        f"{_agent('DbtrAgt', line.debtor_bic)}"
        f"<Dbtr><Nm>{escape(debtor)}</Nm></Dbtr>"
        f"<DbtrAcct><Id><IBAN>{escape(line.debtor_iban)}</IBAN></Id></DbtrAcct>"
        f"{remittance_xml}"
        "</DrctDbtTxInf>"
    )

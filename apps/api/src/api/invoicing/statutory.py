"""What an invoice must carry before it may be issued - FR-AR-003.

    FR-AR-003  Invoices comply with Dutch statutory invoice content
               requirements; THE SYSTEM BLOCKS SENDING if a mandatory field is
               absent.

--- Blocking is the requirement, so this refuses rather than warns ---

Every other validation in the invoicing module is advisory: a draft may be as
incomplete as its author likes, for as long as they like. This one is a gate.
It runs at ISSUE - the moment the number is allocated and the document becomes
a statutory record - and a failure stops the transition.

Issue is the right gate rather than send, and the distinction matters because
FR-AR-005's delivery is a separate step that may happen days later or through
three different channels. Checking at each delivery point would be three
checks; checking at issue is one, and it is the point after which the document
cannot be corrected by editing anyway.

--- Every failure is reported at once ---

`check()` returns the whole list. Somebody completing an invoice should see all
four missing fields on one screen rather than discovering them one save at a
time - the posture `Expense.missing_fields` already takes for FR-EXP-001b.

--- Every failure says what to DO about it (D5) ---

    D5  No dead ends. Every error message states what happened, why, and the
        specific next action. "Validation failed" is not an acceptable string.

A `StatutoryField` is an identifier, and an identifier on a screen is the dead
end D5 names. So each one also resolves to a sentence carrying all three parts:

    what      "Your business BTW number is missing"
    why       "Every invoice must carry it (art. 35a Wet OB)"
    action    "Add it in this administration's details"

The citation is part of the WHY on purpose. A person who does not believe the
product will believe the statute, and an invoice refused for a reason that
sounds arbitrary is the one somebody works around.

`message_for` renders one; the route sends it beside the field so a client has
both the sentence and the identifier to attach it to. The field remains the
thing code branches on - a client with its own screen puts each message against
its input, and one without still shows something a person can act on rather
than `supplier_vat_number`.

Two of these deliberately offer more than one next action, because there
genuinely is more than one: a missing customer BTW number is fixed by supplying
the number OR by choosing a treatment that does not need it, and a treatment
with no rate on the invoice date may need an administrator rather than the
person looking at the screen. Offering only the first would dead-end whoever is
in the other case.

--- The list, and where it comes from ---

Wet OB 1968 art. 35a, implementing EU VAT Directive art. 226. The requirements
that can be checked mechanically are here; the ones that cannot are noted at
the bottom of this docstring rather than silently omitted.

    supplier name, address       art. 35a(1)(e)
    supplier VAT number          art. 35a(1)(c)
    supplier KvK number          Handelsregisterwet; not art. 35a, and
                                 required on Dutch commercial documents
    customer name, address       art. 35a(1)(e)
    customer VAT number          art. 35a(1)(f) - only where the customer is
                                 liable for the VAT: the reverse charge and
                                 intra-Community cases
    invoice number               art. 35a(1)(b) - allocated at issue, so it is
                                 the transition itself that supplies this
    invoice date                 art. 35a(1)(a)
    supply date                  art. 35a(1)(g), where it differs from the
                                 invoice date
    line description, quantity   art. 35a(1)(h)
    unit price excluding VAT     art. 35a(1)(i)
    taxable amount per rate      art. 35a(1)(i)
    rate applied                 art. 35a(1)(j)
    VAT amount                   art. 35a(1)(k)
    the reason for no VAT        art. 226(11), (11a) - see wording.py

NOT checked here, and deliberately:

  * whether the supplier's VAT number is VALID, or the customer's is
    registered in VIES. Both are external lookups; a wrong-but-well-formed
    number passes this check and is caught by FR-AR-006's customer master when
    that lands.
  * whether the goods description is adequate. Art. 35a(1)(h) wants the
    "nature" of the goods or services, which no rule can decide.
  * the margin scheme's own additional bookkeeping obligations, which fall on
    the seller's records rather than on the invoice.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from api.i18n.catalogue import translate
from api.i18n.language import Language
from api.invoicing.wording import requires_customer_vat_number
from api.vat.rules import TreatmentRole

__all__ = [
    "StatutoryField",
    "StatutoryFailure",
    "InvoiceForIssue",
    "LineForIssue",
    "SupplierDetails",
    "MESSAGE_KEYS",
    "check",
    "message_for",
    "describe",
]


class StatutoryField(enum.Enum):
    """What is missing, as something a client can translate and highlight.

    An enum rather than a sentence, for the reason `api.i18n.http.problem`
    splits `reason` from `message`: the screen has to put the error next to the
    field it belongs to, and it cannot do that from prose.
    """

    SUPPLIER_NAME = "supplier_name"
    SUPPLIER_ADDRESS = "supplier_address"
    SUPPLIER_VAT_NUMBER = "supplier_vat_number"
    SUPPLIER_KVK_NUMBER = "supplier_kvk_number"
    CUSTOMER_NAME = "customer_name"
    CUSTOMER_ADDRESS = "customer_address"
    CUSTOMER_VAT_NUMBER = "customer_vat_number"
    INVOICE_DATE = "invoice_date"
    LINES = "lines"
    LINE_DESCRIPTION = "line_description"
    LINE_QUANTITY = "line_quantity"
    LINE_UNIT_PRICE = "line_unit_price"
    VAT_TREATMENT = "vat_treatment"
    VAT_RATE = "vat_rate"


@dataclass(frozen=True, slots=True)
class StatutoryFailure:
    field: StatutoryField
    #: The line this is about, 1-based, or None for the invoice as a whole. A
    #: screen showing "quantity is missing" against a twelve-line invoice
    #: without saying which line has not helped anybody.
    line_position: int | None = None


@dataclass(frozen=True, slots=True)
class SupplierDetails:
    """The administration's own details, as they will appear on the document.

    The address is structured rather than one string, for the two reasons
    migration 0038 gives: EN 16931 wants the parts separately (FR-AR-005's
    Peppol, P2), and "is the address blank" is a question a single space
    answers while "are the street, postcode and city all present" is the one
    art. 35a(1)(e) actually asks.
    """

    legal_name: str | None
    address_line1: str | None
    postal_code: str | None
    city: str | None
    vat_number: str | None
    kvk_number: str | None
    address_line2: str | None = None
    country: str = "NL"
    #: SI-02. None means no EPC QR code is rendered on this administration's
    #: invoices - see api.invoicing.epc_qr. Never a statutory requirement,
    #: unlike every other field on this dataclass.
    iban: str | None = None

    @property
    def has_address(self) -> bool:
        """All three mandatory parts. Line 2 is optional by definition."""
        return not any(_blank(part) for part in (self.address_line1, self.postal_code, self.city))

    @property
    def formatted_address(self) -> str:
        """One block, for a renderer that wants prose rather than fields.

        Built here rather than stored, so there is one answer to "how is this
        business's address written" and a template cannot invent a second.
        """
        lines = [
            self.address_line1,
            self.address_line2,
            " ".join(part for part in (self.postal_code, self.city) if part),
        ]
        return "\n".join(line.strip() for line in lines if line and line.strip())


@dataclass(frozen=True, slots=True)
class LineForIssue:
    position: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    treatment: str | None
    role: TreatmentRole | None
    net: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceForIssue:
    """Everything the gate looks at. A value, so the check is pure."""

    supplier: SupplierDetails
    customer_name: str | None
    customer_address: str | None
    customer_vat_number: str | None
    invoice_date: date | None
    lines: Sequence[LineForIssue]
    #: Treatments with no resolvable rate on the invoice date. Passed in
    #: because resolving one needs the ruleset and this function has no
    #: database - see `api.invoicing.vat.totals_for` for the same split.
    unresolvable_treatments: frozenset[str] = frozenset()


#: One catalogue key per field. Exhaustive by construction - `check()` cannot
#: produce a failure whose message is missing, because the mapping is asserted
#: complete in tests/invoicing/test_statutory.py and the catalogue check
#: (FR-LOC-001d) proves the keys resolve in both languages.
MESSAGE_KEYS: dict[StatutoryField, str] = {
    StatutoryField.SUPPLIER_NAME: "invoice.required.supplier_name",
    StatutoryField.SUPPLIER_ADDRESS: "invoice.required.supplier_address",
    StatutoryField.SUPPLIER_VAT_NUMBER: "invoice.required.supplier_vat_number",
    StatutoryField.SUPPLIER_KVK_NUMBER: "invoice.required.supplier_kvk_number",
    StatutoryField.CUSTOMER_NAME: "invoice.required.customer_name",
    StatutoryField.CUSTOMER_ADDRESS: "invoice.required.customer_address",
    StatutoryField.CUSTOMER_VAT_NUMBER: "invoice.required.customer_vat_number",
    StatutoryField.INVOICE_DATE: "invoice.required.invoice_date",
    StatutoryField.LINES: "invoice.required.lines",
    StatutoryField.LINE_DESCRIPTION: "invoice.required.line_description",
    StatutoryField.LINE_QUANTITY: "invoice.required.line_quantity",
    StatutoryField.LINE_UNIT_PRICE: "invoice.required.line_unit_price",
    StatutoryField.VAT_TREATMENT: "invoice.required.vat_treatment",
    StatutoryField.VAT_RATE: "invoice.required.vat_rate",
}

#: Fields whose message names the line it is about. A line-level failure with
#: no line number is the dead end D5 names, so the two are kept in step here
#: rather than by whoever writes the next catalogue entry.
_LINE_SCOPED: frozenset[StatutoryField] = frozenset(
    {
        StatutoryField.LINE_DESCRIPTION,
        StatutoryField.LINE_QUANTITY,
        StatutoryField.LINE_UNIT_PRICE,
        StatutoryField.VAT_TREATMENT,
        StatutoryField.VAT_RATE,
    }
)


def message_for(failure: StatutoryFailure, language: Language) -> str:
    """What happened, why, and what to do about it - D5, in one sentence group.

    `language` is the READER's, not the recipient's. This is interface text
    shown to whoever is completing the invoice, unlike the legal wording in
    `api.invoicing.wording`, which follows the customer (FR-TPL-013). The two
    are different audiences and it would be a real bug to swap them: a Dutch
    bookkeeper invoicing a German customer must not be told what to fix in
    German.

    Raises rather than returning a placeholder for a line-scoped failure with
    no line number. The catalogue would raise on the missing `{line}` anyway;
    doing it here says which of the two mistakes it was.
    """
    if failure.field in _LINE_SCOPED and failure.line_position is None:
        raise ValueError(
            f"{failure.field.value} is about a specific line and carries no line "
            f"number. A message that cannot say WHICH line is the dead end D5 "
            f"rules out."
        )
    params = {"line": failure.line_position} if failure.field in _LINE_SCOPED else {}
    return translate(MESSAGE_KEYS[failure.field], language, **params)


def describe(
    failures: Sequence[StatutoryFailure], language: Language
) -> tuple[tuple[StatutoryFailure, str], ...]:
    """Every failure with its sentence, in the order `check()` found them.

    Order matters and is not alphabetical: `check()` reports the invoice-wide
    problems before the line-level ones, which is the order somebody works
    through them - there is no point fixing line 7's quantity while the invoice
    has no customer.
    """
    return tuple((failure, message_for(failure, language)) for failure in failures)


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def check(invoice: InvoiceForIssue) -> tuple[StatutoryFailure, ...]:
    """Everything that would make this invoice non-compliant, all at once.

    Empty means it may be issued.
    """
    failures: list[StatutoryFailure] = []

    if _blank(invoice.supplier.legal_name):
        failures.append(StatutoryFailure(StatutoryField.SUPPLIER_NAME))
    # One failure for the address rather than three, because a form shows it
    # as one block and three simultaneous errors against one fieldset read as
    # noise. Which part is missing is recoverable from the value itself.
    if not invoice.supplier.has_address:
        failures.append(StatutoryFailure(StatutoryField.SUPPLIER_ADDRESS))
    if _blank(invoice.supplier.vat_number):
        failures.append(StatutoryFailure(StatutoryField.SUPPLIER_VAT_NUMBER))
    if _blank(invoice.supplier.kvk_number):
        failures.append(StatutoryFailure(StatutoryField.SUPPLIER_KVK_NUMBER))

    if _blank(invoice.customer_name):
        failures.append(StatutoryFailure(StatutoryField.CUSTOMER_NAME))
    if _blank(invoice.customer_address):
        failures.append(StatutoryFailure(StatutoryField.CUSTOMER_ADDRESS))
    if invoice.invoice_date is None:
        failures.append(StatutoryFailure(StatutoryField.INVOICE_DATE))

    # An invoice with no lines states no supply. It would also produce a
    # gapless number for a document that says nothing, which is worse than
    # refusing: the number cannot be given back.
    if not invoice.lines:
        failures.append(StatutoryFailure(StatutoryField.LINES))

    for line in invoice.lines:
        if _blank(line.description):
            failures.append(StatutoryFailure(StatutoryField.LINE_DESCRIPTION, line.position))
        # Zero is a legitimate quantity on a credit line; absent is not.
        if line.quantity is None:
            failures.append(StatutoryFailure(StatutoryField.LINE_QUANTITY, line.position))
        if line.unit_price is None:
            failures.append(StatutoryFailure(StatutoryField.LINE_UNIT_PRICE, line.position))
        if line.treatment is None or line.role is None:
            failures.append(StatutoryFailure(StatutoryField.VAT_TREATMENT, line.position))
        elif line.treatment in invoice.unresolvable_treatments:
            # A rate-bearing treatment the ruleset does not cover on this date.
            # Issuing would print a VAT amount computed from nothing.
            failures.append(StatutoryFailure(StatutoryField.VAT_RATE, line.position))

    # Art. 226(11a): where the customer accounts for the VAT, the invoice must
    # identify them by VAT number. Without it the buyer cannot make the reverse
    # charge, and an intra-Community supply cannot be zero-rated at all - so
    # this is not a formality, it is the thing that makes the treatment lawful.
    if any(
        line.role is not None and requires_customer_vat_number(line.role) for line in invoice.lines
    ) and _blank(invoice.customer_vat_number):
        failures.append(StatutoryFailure(StatutoryField.CUSTOMER_VAT_NUMBER))

    return tuple(failures)

"""Customer master types and refusals - FR-AR-006.

Mirrors migration 0039. Where this module and the database disagree, the
database is right - the same bargain `api.invoicing.model` makes with 0037.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from api.customers.address import PostalAddress
from api.customers.vies import ViesStatus
from api.i18n.language import Language

__all__ = [
    "DeliveryChannel",
    "Customer",
    "CustomerError",
    "CustomerNotFound",
    "CustomerIsArchived",
    "CustomerIsErased",
    "CustomerAddressIncomplete",
    "InvalidCustomerField",
    "NotAuthorizedToManageCustomers",
    "RetainedInvoiceRef",
]


class DeliveryChannel(enum.Enum):
    """FR-AR-006's "preferred delivery channel". Mirrors 0039's CHECK.

    `PEPPOL` is selectable before FR-AR-005 ships it in P2. It is a statement
    of what the customer wants, and losing that answer because the channel is
    not built yet would mean re-collecting it from every customer later.
    Whether it can be HONOURED is `Customer.is_deliverable_over_peppol`, which
    is a different question and asked at send time.
    """

    EMAIL = "email"
    PEPPOL = "peppol"
    POST = "post"


@dataclass(frozen=True, slots=True)
class Customer:
    """One counterparty, as believed TODAY.

    Nothing here is a fact about any document. An invoice snapshots what it
    needs at creation (migration 0037's columns, populated by this record) and
    never reads back through this object - see 0039's header for why.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID

    name: str
    trade_name: str | None
    address: PostalAddress

    # -- FR-AR-006's identifiers -------------------------------------------
    kvk_number: str | None
    #: Normalised (`api.customers.vat_number.normalise`) before it is stored,
    #: so one number has one spelling.
    vat_number: str | None
    vat_number_status: ViesStatus
    vat_number_checked_at: datetime | None
    vat_number_checked_name: str | None
    vat_number_consultation_number: str | None

    #: P2. Recordable, not yet discoverable - see `api.customers.peppol`.
    peppol_participant_id: str | None
    peppol_checked_at: datetime | None

    # -- FR-AR-006's commercial terms --------------------------------------
    payment_terms_days: int
    #: NFR-031: Decimal, never float. None is "no limit set", which is not 0.
    credit_limit: Decimal | None
    delivery_channel: DeliveryChannel
    invoice_email: str | None
    #: FR-AR-006's "language", and the recipient language FR-TPL-013 renders
    #: an invoice's legal wording in.
    language: Language

    notes: str | None
    archived_at: datetime | None
    #: PRIV-022. Set once, by CustomerService.request_erasure - see
    #: migration 0044's customer_erasure_is_frozen_trg for what setting it
    #: guarantees. Never null->non-null->null again: erasure is not undone.
    erased_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_erased(self) -> bool:
        return self.erased_at is not None

    @property
    def is_deliverable_over_peppol(self) -> bool:
        """Whether a Peppol invoice could actually be addressed.

        A preference for Peppol is not an address. This is what FR-AR-005 has
        to check before honouring `delivery_channel`, and it is here rather
        than at the send site so that "prefers Peppol, cannot receive Peppol" -
        the ordinary state today, since nothing discovers the id - is legible
        on the customer rather than discovered at delivery time.
        """
        return self.peppol_participant_id is not None

    @property
    def has_confirmed_vat_number(self) -> bool:
        """VIES said yes, on a date. See `ViesStatus.permits_zero_rating`."""
        return self.vat_number_status.permits_zero_rating

    def due_date_for(self, invoice_date: date) -> date:
        """FR-AR-006's payment terms, applied.

        Calendar days from the invoice date, which is what "30 dagen netto"
        means and what BW art. 6:119a counts. Deliberately not business days:
        that would need a holiday calendar per jurisdiction, and no Dutch
        payment term is expressed that way.
        """
        return invoice_date + timedelta(days=self.payment_terms_days)


class CustomerError(Exception):
    """Base for this module's refusals."""


class CustomerNotFound(CustomerError):
    """No such customer in this administration.

    Indistinguishable from "belongs to another tenant", and deliberately so:
    RLS filters it out either way, so this branch cannot tell and must not
    seem to. The same posture `api.invoicing.model.InvoiceNotFound` takes.
    """


class CustomerIsArchived(CustomerError):
    """An archived customer may be read - every invoice ever raised for them
    still points at this row - but not invoiced or edited. Reactivating is an
    explicit act, so that "we stopped trading with them" is not undone by
    somebody starting a new invoice.
    """


class CustomerIsErased(CustomerError):
    """PRIV-022. Unlike `CustomerIsArchived`, there is no route back: erasure
    cannot be undone, so a caller cannot "restore" their way past this one.
    """


class CustomerAddressIncomplete(CustomerError):
    """This customer cannot be put on an invoice yet.

    Distinct from a validation error at save time on purpose: FR-AR-006 does
    not require an address, and a half-known customer is a legitimate record to
    keep. What is not legitimate is a statutory document missing what Wet OB
    art. 35a(1)(e) requires, so the refusal lands where the document is made.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "this customer's address is incomplete and cannot be put on an "
            f"invoice (FR-AR-003): missing {', '.join(missing)}"
        )


class InvalidCustomerField(CustomerError):
    """A value the database would refuse, refused earlier and by name.

    Carries `field` so a form can mark the input rather than showing a whole-
    record error - the shape `api.invoicing.statutory` reports its failures in.
    """

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}")


@dataclass(frozen=True, slots=True)
class RetainedInvoiceRef:
    """One of this customer's ISSUED invoices that CMP-001 still requires
    kept, as PRIV-022's erasure decision names it.

    Not `sales_invoice`'s own type - this carries only what the explanation
    needs to be specific (the reference a person would recognise, the date,
    and the date it stops being true), not the invoice's financial content.
    """

    invoice_id: uuid.UUID
    invoice_reference: str
    invoice_date: date
    retained_until: date


class NotAuthorizedToManageCustomers(CustomerError):
    def __init__(self, action: str, resource_type: str, detail: str | None) -> None:
        self.action = action
        self.resource_type = resource_type
        self.detail = detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")

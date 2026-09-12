"""How an invoice reaches a customer - FR-AR-005's channel seam.

    FR-AR-005  Send as PDF by email in P0; structured e-invoice over Peppol
               (BIS Billing 3.0 / NLCIUS, EN 16931) added in P2. Delivery
               status tracked per channel.

--- The whole point of this module is what P2 will NOT have to touch ---

Peppol is a different protocol carrying a different artifact to a different
kind of address. If any of those three facts leaked into the invoicing service,
adding the channel would mean editing the code that issues invoices - and the
code that issues invoices is the code with the gapless number series and the
statutory gate in it.

So three things that differ per channel live on the adapter, not in the caller:

    which ADDRESS      `address_of` - an e-mail address, or a participant id
    which ARTIFACT     `required_artifacts` - the PDF, or the UBL
    how to HAND OVER   `deliver`

`InvoiceDeliveryService` therefore contains no `if channel is EMAIL` anywhere,
and adding Peppol in P2 is: write a `PeppolInvoiceChannel`, register it, teach
something to produce `ArtifactKind.UBL`. No invoice logic changes.

That claim is not left to inspection - `tests/invoicing/test_delivery.py`
registers a fabricated third channel and drives a whole dispatch over it
without touching anything in this package.

--- Why the address is not just `customer.invoice_email` ---

It is tempting to have the service read the e-mail address and pass a string.
It works exactly until the second channel, whose address is a Peppol
participant id living in a different column and validated by a different rule -
at which point the service grows the branch this design exists to avoid.

`Recipient` therefore carries every way a customer can be addressed, and each
adapter takes the one it understands. A customer who cannot be reached on the
requested channel is a refusal naming the channel, not a `None` for somebody
else to interpret.

--- Sending is not issuing ---

Migration 0041's header makes the argument in full. In short: a posting is a
database write that rolls back, and an e-mail is an irreversible act by
somebody else's server. They cannot share a transaction, so they do not share
an act.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from api.i18n.catalogue import translate
from api.i18n.formatting import format_date, format_money
from api.i18n.language import Language
from api.mail.sender import Attachment, EmailMessage, EmailSender, UnsendableMessage

__all__ = [
    "DeliveryChannel",
    "DeliveryStatus",
    "ArtifactKind",
    "DeliveryArtifact",
    "Recipient",
    "DeliverableInvoice",
    "DeliveryRequest",
    "DeliveryOutcome",
    "DeliveryChannelAdapter",
    "ChannelRegistry",
    "EmailInvoiceChannel",
    "UnreachableCustomer",
    "ChannelNotAvailable",
    "ArtifactNotAvailable",
]


class DeliveryChannel(enum.Enum):
    """Mirrors the `channel` CHECK in migration 0041 and
    `api.customers.model.DeliveryChannel`.

    Two enums for one vocabulary is a smell, and they are deliberately separate:
    that one is a customer's stated PREFERENCE and this one is what a dispatch
    actually went out over. They agree today and will not always - a customer
    who prefers Peppol and has no participant id is emailed, and the record has
    to say which happened. `from_preference` is the one-line bridge.
    """

    EMAIL = "email"
    PEPPOL = "peppol"
    POST = "post"

    @classmethod
    def from_preference(cls, preference: str) -> DeliveryChannel:
        return cls(preference)


class DeliveryStatus(enum.Enum):
    """Mirrors the `status` CHECK in migration 0041.

    `SENT` and `DELIVERED` are different facts and the distinction is the
    reason this is not a boolean: acceptance by a provider is not arrival at a
    mailbox, and a bounce comes back minutes later. See 0041's header.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    FAILED = "failed"

    @property
    def is_settled(self) -> bool:
        """Whether anything further will happen to this dispatch."""
        return self in (DeliveryStatus.DELIVERED, DeliveryStatus.BOUNCED, DeliveryStatus.FAILED)

    @property
    def reached_the_customer(self) -> bool:
        """Only DELIVERED.

        A property rather than a comparison each caller writes, for the reason
        `ViesStatus.permits_zero_rating` is one: the tempting `!= BOUNCED`
        would count a queued dispatch as arrived, and "we sent it" about an
        invoice nobody received is the specific wrong sentence this module is
        shaped to avoid.
        """
        return self is DeliveryStatus.DELIVERED


class ArtifactKind(enum.Enum):
    """What a channel carries.

    `UBL` is declared and nothing produces it. That is deliberate rather than
    speculative: it is the one-line difference between "email sends a PDF" and
    "each channel says what it needs", and declaring it now is what lets
    `required_artifacts` be part of the Protocol instead of an assumption that
    every channel wants the PDF. `ArtifactNotAvailable` is what a caller gets
    for asking - a clear refusal rather than a silent empty attachment.
    """

    #: FR-TPL-017's stored rendering, from the document archive.
    PDF = "pdf"
    #: FR-AR-005's structured e-invoice (EN 16931 / NLCIUS). P2, not built.
    UBL = "ubl"


@dataclass(frozen=True, slots=True)
class DeliveryArtifact:
    """Bytes to hand over, and where they came from."""

    kind: ArtifactKind
    filename: str
    content: bytes
    content_type: str
    #: The archive row these bytes were read from, recorded on the dispatch so
    #: "what exactly did the customer receive" resolves to a hash-verified
    #: original rather than to a re-rendering (FR-TPL-017).
    document_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Recipient:
    """Every way a customer can be addressed, so no adapter has to ask.

    Populated from the invoice's own frozen snapshot where it can be, and from
    the customer master where the invoice does not carry the field. Which is
    which matters: the NAME on a dispatch is the invoice's, because that is who
    the document was made out to, while the e-mail address is the master's,
    because that is where they read mail today.
    """

    name: str
    language: Language
    email: str | None = None
    peppol_participant_id: str | None = None
    postal_address: str | None = None


@dataclass(frozen=True, slots=True)
class DeliverableInvoice:
    """What any channel might say about the invoice it is carrying.

    Deliberately not `SalesInvoice`: an adapter that received the whole record
    could reach for `customer_id` or `journal_entry_id`, and a channel has no
    business knowing an invoice has a ledger entry. This is the covering
    message's vocabulary and nothing else.
    """

    id: uuid.UUID
    reference: str
    invoice_date: date
    due_date: date | None
    gross: Decimal
    is_credit_note: bool
    supplier_name: str
    supplier_email: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    invoice: DeliverableInvoice
    recipient: Recipient
    #: Resolved by the adapter's own `address_of`, so the service never had to
    #: know what kind of string this is.
    address: str
    artifacts: Mapping[ArtifactKind, DeliveryArtifact] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What the channel did with it.

    `accepted` means handed over, not arrived - the distinction
    `api.mail.sender` makes and this preserves.
    """

    accepted: bool
    provider: str
    reference: str | None = None
    #: Operator-facing. Never rendered to a user (FR-UX-007).
    detail: str | None = None
    #: Whether trying again could plausibly work. A provider outage is
    #: retryable; a rejected recipient is not, and queueing it would retry a
    #: mistake forever while looking like an outage.
    retryable: bool = True


class DeliveryChannelAdapter(Protocol):
    """CLAUDE.md non-negotiable #4's adapter seam, for FR-AR-005's channels.

    Four members, and every one of them exists because it differs per channel.
    A fifth would mean the service is asking a question it should not.
    """

    #: Which channel this serves. The registry keys on it.
    channel: DeliveryChannel
    #: What `deliver` needs handed to it. The service fetches exactly these,
    #: which is how a channel that wants UBL rather than PDF costs the service
    #: nothing.
    required_artifacts: frozenset[ArtifactKind]

    def address_of(self, recipient: Recipient, *, override: str | None = None) -> str | None:
        """Where this channel would send to, or None if it cannot reach them.

        `override` is an address the caller supplied for THIS send - a one-off
        customer with no master record, or "copy their bookkeeper". Resolved
        here rather than by the service because validating it is
        channel-specific: an e-mail address and a Peppol participant id are
        checked by different rules, and a service that substituted a string
        blindly would hand a typo to the transport and report it as a failed
        send rather than as a bad address.

        Not `raise`: "this customer has no e-mail address" is an ordinary state
        the service turns into one refusal naming the channel, and an exception
        per adapter would be a second way to express it.
        """
        ...

    async def deliver(self, request: DeliveryRequest) -> DeliveryOutcome: ...


class UnreachableCustomer(Exception):
    """The customer cannot be addressed on the requested channel.

    Carries the channel, so the message can say which - "this customer has no
    e-mail address" and "this customer is not on Peppol" are different problems
    with different fixes.
    """

    def __init__(self, channel: DeliveryChannel, detail: str) -> None:
        self.channel = channel
        super().__init__(detail)


class ChannelNotAvailable(Exception):
    """No adapter is registered for the requested channel.

    What a P0 deployment answers for `peppol`. Deliberately distinct from
    `UnreachableCustomer`: "we cannot do that yet" and "they cannot receive
    that" are different sentences, and telling a user the second when the first
    is true sends them to correct customer data that is already right.
    """

    def __init__(self, channel: DeliveryChannel) -> None:
        self.channel = channel
        super().__init__(f"no delivery channel is configured for {channel.value}")


class ArtifactNotAvailable(Exception):
    """A channel asked for something nothing produces yet.

    Today: `UBL`. A clear refusal rather than an empty attachment, because an
    e-invoice with no payload is worse than one that was never sent.
    """

    def __init__(self, kind: ArtifactKind) -> None:
        self.kind = kind
        super().__init__(f"nothing produces a {kind.value} artifact yet")


class ChannelRegistry:
    """The lookup that keeps `if channel is ...` out of the service.

    A class rather than a dict so registration can refuse a duplicate: two
    adapters claiming one channel would make which one sends depend on
    construction order, and the losing one would be dead code nobody notices.
    """

    def __init__(self, adapters: tuple[DeliveryChannelAdapter, ...] = ()) -> None:
        self._adapters: dict[DeliveryChannel, DeliveryChannelAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: DeliveryChannelAdapter) -> None:
        if adapter.channel in self._adapters:
            raise ValueError(
                f"two adapters claim the {adapter.channel.value} channel; which one "
                f"sends would depend on construction order"
            )
        self._adapters[adapter.channel] = adapter

    def adapter_for(self, channel: DeliveryChannel) -> DeliveryChannelAdapter:
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise ChannelNotAvailable(channel)
        return adapter

    def supports(self, channel: DeliveryChannel) -> bool:
        return channel in self._adapters

    @property
    def channels(self) -> frozenset[DeliveryChannel]:
        """What this deployment can actually send over. Returned to clients so
        a screen can grey out Peppol rather than offering it and failing.
        """
        return frozenset(self._adapters)


#: Reused from `api.mail.sender`'s deliberately loose check - one definition of
#: "could be an e-mail address", so the adapter and the transport cannot
#: disagree about an address the other would reject.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailInvoiceChannel:
    """FR-AR-005's P0 channel: the stored PDF, attached to a covering message.

    The message body is deliberately short. Its job is to say what is attached,
    from whom, for how much and by when - everything else the reader needs is
    on the invoice itself, and a body that restated the invoice would be a
    second document to keep in step with the first.

    FR-LOC-003: written in the RECIPIENT's language, which the invoice carries
    as a frozen snapshot (`customer_language`, migration 0039). Not the
    sender's, and not the language of whoever pressed send.
    """

    channel = DeliveryChannel.EMAIL
    #: FR-TPL-017's stored rendering. Not a fresh render: the bytes a customer
    #: receives are the bytes that were stored when the invoice was issued.
    required_artifacts = frozenset({ArtifactKind.PDF})

    def __init__(self, sender: EmailSender, *, from_address: str, from_name: str | None = None):
        self._sender = sender
        self._from_address = from_address
        self._from_name = from_name

    def address_of(self, recipient: Recipient, *, override: str | None = None) -> str | None:
        """The override if it is a usable address, else the customer's own.

        An override that is NOT a usable address returns None rather than
        falling back: somebody who typed an address meant to send there, and
        quietly sending to the customer instead would be the worst outcome
        available - the invoice goes somewhere the sender did not intend and
        the response says it succeeded.
        """
        address = (override if override is not None else recipient.email or "").strip()
        return address if _EMAIL.match(address) else None

    async def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        pdf = request.artifacts.get(ArtifactKind.PDF)
        if pdf is None:  # pragma: no cover - the service resolves these first
            raise ArtifactNotAvailable(ArtifactKind.PDF)

        language = request.recipient.language
        message = EmailMessage(
            to=request.address,
            subject=_subject(request.invoice, language),
            body=_body(request.invoice, language),
            from_address=self._from_address,
            # The SUPPLIER's name, not LEDGR's: the customer is receiving an
            # invoice from their supplier, and a sender line naming the
            # bookkeeping software is how an invoice ends up in a spam folder.
            from_name=self._from_name or request.invoice.supplier_name,
            # So a reply about the invoice reaches the business rather than
            # a no-reply mailbox nobody reads.
            reply_to=request.invoice.supplier_email,
            attachments=(
                Attachment(
                    filename=pdf.filename,
                    content=pdf.content,
                    content_type=pdf.content_type,
                ),
            ),
        )

        try:
            outcome = await self._sender.send(message)
        except UnsendableMessage as exc:
            # A defect in the message rather than an outage. NOT retryable: the
            # same message would fail identically forever while occupying the
            # queue and looking like a provider problem.
            return DeliveryOutcome(
                accepted=False,
                provider="email",
                detail=str(exc),
                retryable=False,
            )

        return DeliveryOutcome(
            accepted=outcome.accepted,
            provider=outcome.provider,
            reference=outcome.reference,
            detail=outcome.detail,
            # A refusal that named a reference got as far as the provider and
            # was rejected by it - a bad recipient, not a bad connection.
            retryable=outcome.accepted or outcome.reference is None,
        )


def _subject(invoice: DeliverableInvoice, language: Language) -> str:
    key = "invoice.email.credit_note_subject" if invoice.is_credit_note else "invoice.email.subject"
    return translate(key, language, reference=invoice.reference, supplier=invoice.supplier_name)


def _body(invoice: DeliverableInvoice, language: Language) -> str:
    """The covering message. Short, plain text, and in the recipient's language.

    Amounts and dates are formatted with the product's default locale rather
    than the administration's, and that is a known wrinkle rather than a
    decision: the body is a courtesy restating what the PDF says exactly, and
    `format_money`'s only locale is `nl-NL` today anyway (FR-LOC-005 is where a
    second one arrives). Recorded in ADR-040 so it is corrected with the rest
    rather than discovered.
    """
    lines = [
        translate("invoice.email.greeting", language),
        "",
        translate(
            "invoice.email.credit_note_intro" if invoice.is_credit_note else "invoice.email.intro",
            language,
            reference=invoice.reference,
            supplier=invoice.supplier_name,
            amount=format_money(invoice.gross),
        ),
    ]

    if invoice.due_date and not invoice.is_credit_note:
        lines.append(
            translate(
                "invoice.email.due",
                language,
                due_date=format_date(invoice.due_date),
                amount=format_money(invoice.gross),
            )
        )

    lines.extend(
        [
            "",
            translate("invoice.email.questions", language, supplier=invoice.supplier_name),
            "",
            translate("invoice.email.signoff", language),
            invoice.supplier_name,
        ]
    )
    return "\n".join(lines)

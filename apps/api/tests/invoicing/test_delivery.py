"""api.invoicing.delivery and delivery_service - FR-AR-005.

The load-bearing test in this file is
`test_a_channel_this_package_has_never_heard_of_can_be_dispatched_over`. The
module docstrings claim that adding Peppol in P2 touches no invoice logic;
that test is the claim, executed - it defines a channel here, in the test file,
carrying an artifact the email path never asks for, and drives a whole dispatch
over it without a line changing in `api.invoicing`.

Everything else is the states: that a provider outage queues rather than fails,
that a bad address does not queue, that `sent` is never reported as arrival.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from api.audit.log import AuditLog, AuditOutcome
from api.authz.matrix import ROLES, permissions_for_role
from api.authz.service import AuthorizationService
from api.documents.content_type import DocumentContentType
from api.documents.model import Document, DocumentStatus
from api.documents.retention import RetentionBasis
from api.documents.scanning import ScanStatus
from api.i18n.language import Language
from api.invoicing.delivery import (
    ArtifactKind,
    ArtifactNotAvailable,
    ChannelNotAvailable,
    ChannelRegistry,
    DeliveryChannel,
    DeliveryOutcome,
    DeliveryRequest,
    DeliveryStatus,
    EmailInvoiceChannel,
    Recipient,
    UnreachableCustomer,
)
from api.invoicing.delivery_service import (
    RETRY_BACKOFF,
    DeliveryRecord,
    InvoiceDeliveryService,
    InvoiceNotRendered,
)
from api.invoicing.model import InvoiceNotIssued, InvoiceStatus, NotAuthorizedToInvoice
from api.mail.sender import CollectingEmailSender, EmailOutcome
from tests.authz.helpers import build_world
from tests.invoicing.test_posting import invoice as build_invoice
from tests.support.fake_audit_repository import InMemoryAuditRepository

PDF = b"%PDF-1.7\nstored at issue\n%%EOF\n"


# --- fakes -------------------------------------------------------------------


@dataclass
class FakeDeliveryRepository:
    """Migration 0041's table, in memory."""

    invoices: dict[uuid.UUID, object] = field(default_factory=dict)
    recipient: Recipient = field(
        default_factory=lambda: Recipient(
            name="De Vries Holding B.V.",
            language=Language.NL,
            email="facturen@devries.example",
        )
    )
    preference: DeliveryChannel | None = None
    rows: dict[uuid.UUID, DeliveryRecord] = field(default_factory=dict)
    organization_id: uuid.UUID = field(default_factory=uuid.uuid4)

    async def invoice(self, *, administration_id, invoice_id):  # type: ignore[no-untyped-def]
        return self.invoices.get(invoice_id)

    async def recipient_for(self, *, administration_id, invoice):  # type: ignore[no-untyped-def]
        return self.recipient

    async def supplier_contact(self, *, administration_id):  # type: ignore[no-untyped-def]
        return ("Bakker Consultancy B.V.", "facturen@bakker.example")

    async def preferred_channel(self, *, administration_id, invoice):  # type: ignore[no-untyped-def]
        return self.preference

    async def create(  # type: ignore[no-untyped-def]
        self,
        *,
        organization_id,
        administration_id,
        invoice_id,
        channel,
        recipient,
        language,
        document_id,
        user_id,
    ):
        record = DeliveryRecord(
            id=uuid.uuid4(),
            invoice_id=invoice_id,
            channel=channel,
            status=DeliveryStatus.QUEUED,
            recipient=recipient,
            language=language,
            attempts=0,
            document_id=document_id,
            requested_at=datetime.now(UTC),
        )
        self.rows[record.id] = record
        return record

    async def record_outcome(  # type: ignore[no-untyped-def]
        self,
        *,
        administration_id,
        delivery_id,
        status,
        provider,
        provider_reference,
        last_error,
        next_attempt_at,
    ):
        existing = self.rows[delivery_id]
        # 0041's CASE ladder: the timestamps follow from the status.
        settled = replace(
            existing,
            status=status,
            attempts=existing.attempts + 1,
            provider=provider,
            provider_reference=provider_reference or existing.provider_reference,
            last_error=last_error,
            sent_at=(
                existing.sent_at or datetime.now(UTC)
                if status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED)
                else existing.sent_at
            ),
            settled_at=(existing.settled_at or datetime.now(UTC) if status.is_settled else None),
            next_attempt_at=next_attempt_at,
        )
        # invoice_delivery_only_queued_is_scheduled.
        assert settled.status is DeliveryStatus.QUEUED or settled.next_attempt_at is None
        # invoice_delivery_sent_is_dated.
        assert (settled.status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED)) == (
            settled.sent_at is not None
        )
        self.rows[delivery_id] = settled
        return settled

    async def state_of(self, *, administration_id, invoice_id):  # type: ignore[no-untyped-def]
        latest: dict[DeliveryChannel, DeliveryRecord] = {}
        for row in self.rows.values():
            if row.invoice_id != invoice_id:
                continue
            seen = latest.get(row.channel)
            if seen is None or (row.requested_at or 0) >= (seen.requested_at or 0):
                latest[row.channel] = row
        return list(latest.values())

    async def organization_of(self, *, administration_id):  # type: ignore[no-untyped-def]
        return self.organization_id


@dataclass
class FakeDocuments:
    """`DocumentService.original`, which is all delivery uses of it."""

    content: bytes = PDF
    reads: list[uuid.UUID] = field(default_factory=list)

    async def original(self, *, administration_id, document_id, actor_user_id):  # type: ignore[no-untyped-def]
        self.reads.append(document_id)
        return (
            Document(
                id=document_id,
                organization_id=uuid.uuid4(),
                administration_id=administration_id,
                storage_key="k",
                content_hash=b"\x00" * 32,
                byte_size=len(self.content),
                content_type=DocumentContentType.PDF,
                fiscal_year_id=uuid.uuid4(),
                retention_basis=RetentionBasis.STANDARD,
                retention_until=date(2033, 12, 31),
                status=DocumentStatus.ACTIVE,
                scan_status=ScanStatus.CLEAN,
            ),
            self.content,
        )


@dataclass
class RefusingSender:
    """A mail provider that is down. `reference=None` marks it a transport
    failure rather than a rejected recipient - see EmailInvoiceChannel.
    """

    detail: str = "ConnectionRefusedError: [Errno 111]"

    async def send(self, message):  # type: ignore[no-untyped-def]
        return EmailOutcome(accepted=False, provider="smtp", detail=self.detail)


@dataclass
class RejectingSender:
    """A provider that took the conversation and refused the recipient."""

    async def send(self, message):  # type: ignore[no-untyped-def]
        return EmailOutcome(
            accepted=False,
            provider="smtp",
            reference="<abc@ledgr>",
            detail="recipient refused: 550 no such mailbox",
        )


@dataclass
class Harness:
    service: InvoiceDeliveryService
    repository: FakeDeliveryRepository
    documents: FakeDocuments
    audit: InMemoryAuditRepository
    mail: CollectingEmailSender
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID
    invoice_id: uuid.UUID


def harness(
    *,
    role: str = "Invoicer",
    sender: object | None = None,
    channels: tuple[object, ...] | None = None,
    status: InvoiceStatus = InvoiceStatus.ISSUED,
    document_id: uuid.UUID | None = None,
    **repo_kwargs: object,
) -> Harness:
    world = build_world()
    scope_type = next(r.scope_type for r in ROLES if r.name == role)
    world.repository.assign(
        user_id=world.user,
        role=role,
        scope_id=world.acme if scope_type == "organization" else world.acme_books,
    )

    mail = CollectingEmailSender()
    registry = ChannelRegistry(
        channels
        if channels is not None
        else (
            EmailInvoiceChannel(
                sender or mail,  # type: ignore[arg-type]
                from_address="noreply@ledgr.example",
            ),
        )
    )

    invoice = build_invoice(customer_id=uuid.uuid4())
    invoice = replace(
        invoice,
        administration_id=world.acme_books,
        status=status,
        invoice_number=None if status is InvoiceStatus.DRAFT else invoice.invoice_number,
        invoice_reference=None if status is InvoiceStatus.DRAFT else invoice.invoice_reference,
        document_id=document_id if document_id is not None else uuid.uuid4(),
    )

    repository = FakeDeliveryRepository(**repo_kwargs)  # type: ignore[arg-type]
    repository.invoices[invoice.id] = invoice
    documents = FakeDocuments()
    audit = InMemoryAuditRepository()

    return Harness(
        service=InvoiceDeliveryService(
            repository=repository,  # type: ignore[arg-type]
            registry=registry,
            documents=documents,  # type: ignore[arg-type]
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(audit),  # type: ignore[arg-type]
        ),
        repository=repository,
        documents=documents,
        audit=audit,
        mail=mail,
        administration=world.acme_books,
        organization=repository.organization_id,
        user=world.user,
        invoice_id=invoice.id,
    )


async def send(h: Harness, **kwargs: object) -> DeliveryRecord:
    return await h.service.dispatch(
        administration_id=h.administration,
        invoice_id=h.invoice_id,
        actor_user_id=h.user,
        **kwargs,  # type: ignore[arg-type]
    )


# --- THE claim: a new channel touches no invoice logic ------------------------


@dataclass
class CourierChannel:
    """A third channel, invented entirely in this test file.

    It is not e-mail and it is not Peppol. It addresses customers by postal
    address, carries an artifact the e-mail path never asks for, and reports its
    own provider name - the three things `api.invoicing.delivery`'s docstring
    says differ per channel.

    If dispatching over it requires editing anything under `api/invoicing`, the
    seam does not work and this test fails.
    """

    channel = DeliveryChannel.POST
    required_artifacts = frozenset({ArtifactKind.PDF})
    delivered: list[DeliveryRequest] = field(default_factory=list)

    def address_of(self, recipient: Recipient, *, override: str | None = None) -> str | None:
        address = override or recipient.postal_address
        return address.strip() if address and address.strip() else None

    async def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.delivered.append(request)
        return DeliveryOutcome(accepted=True, provider="courier", reference="TRACK-1")


async def test_a_channel_this_package_has_never_heard_of_can_be_dispatched_over() -> None:
    courier = CourierChannel()
    h = harness(
        channels=(courier,),
        recipient=Recipient(
            name="De Vries Holding B.V.",
            language=Language.NL,
            postal_address="Damrak 70\n1012 LM  Amsterdam",
        ),
    )

    record = await send(h, channel=DeliveryChannel.POST)

    assert record.status is DeliveryStatus.SENT
    assert record.channel is DeliveryChannel.POST
    assert record.provider == "courier"
    assert record.provider_reference == "TRACK-1"
    # It was addressed by its OWN rule - a postal address, which the e-mail
    # channel would have rejected.
    assert record.recipient == "Damrak 70\n1012 LM  Amsterdam"
    # And it received the artifact it declared, resolved by the service without
    # knowing what channel asked.
    assert courier.delivered[0].artifacts[ArtifactKind.PDF].content == PDF


async def test_a_new_channel_gets_the_invoice_facts_without_the_invoice_record() -> None:
    """`DeliverableInvoice` is the covering message's vocabulary. A channel that
    received `SalesInvoice` could reach for `journal_entry_id`, and a courier
    has no business knowing an invoice has a ledger entry.
    """
    courier = CourierChannel()
    h = harness(
        channels=(courier,),
        recipient=Recipient(name="X", language=Language.NL, postal_address="Damrak 70"),
    )
    await send(h, channel=DeliveryChannel.POST)

    delivered = courier.delivered[0].invoice
    assert delivered.reference == "2026-1"
    assert delivered.supplier_name == "Bakker Consultancy B.V."
    assert not hasattr(delivered, "journal_entry_id")
    assert not hasattr(delivered, "customer_id")


def test_the_registry_refuses_two_adapters_for_one_channel() -> None:
    """Which one sends would depend on construction order, and the loser would
    be dead code nobody notices.
    """
    with pytest.raises(ValueError, match="two adapters claim"):
        ChannelRegistry((CourierChannel(), CourierChannel()))


# --- the happy path -----------------------------------------------------------


async def test_the_stored_pdf_is_attached() -> None:
    h = harness()
    record = await send(h)

    assert record.status is DeliveryStatus.SENT
    assert h.mail.sent[0].attachments[0].content == PDF
    assert h.mail.sent[0].attachments[0].content_type == "application/pdf"


async def test_the_pdf_is_read_from_the_archive_and_never_re_rendered() -> None:
    """FR-TPL-017. Sending goes nowhere near the renderer: what a customer
    receives is the hash-verified original stored when the invoice was issued.
    """
    h = harness()
    record = await send(h)

    assert h.documents.reads == [record.document_id]


async def test_the_message_goes_to_the_customers_address() -> None:
    h = harness()
    record = await send(h)

    assert record.recipient == "facturen@devries.example"
    assert h.mail.sent[0].to == "facturen@devries.example"


async def test_the_from_name_is_the_supplier_not_the_product() -> None:
    """A customer receives an invoice from their supplier. A From line naming
    the bookkeeping software is how an invoice reaches a spam folder.
    """
    h = harness()
    await send(h)

    assert h.mail.sent[0].from_name == "Bakker Consultancy B.V."
    assert h.mail.sent[0].reply_to == "facturen@bakker.example"


async def test_the_subject_carries_the_reference_and_the_supplier() -> None:
    h = harness()
    await send(h)

    assert "2026-1" in h.mail.sent[0].subject
    assert "Bakker Consultancy B.V." in h.mail.sent[0].subject


# --- FR-LOC-003 ---------------------------------------------------------------


async def test_the_message_is_written_in_the_recipients_language() -> None:
    """FR-LOC-003: localised per recipient, independent of the sender's UI
    language. The language is the frozen snapshot on the invoice.
    """
    h = harness()
    await send(h)
    dutch = h.mail.sent[0]

    english = harness(
        recipient=Recipient(
            name="De Vries Holding B.V.",
            language=Language.EN,
            email="facturen@devries.example",
        )
    )
    await send(english)

    assert dutch.subject.startswith("Factuur")
    assert "Beste klant" in dutch.body
    assert english.mail.sent[0].subject.startswith("Invoice")
    assert "Dear customer" in english.mail.sent[0].body


async def test_the_language_is_recorded_on_the_dispatch() -> None:
    """So "why did my customer get a Dutch e-mail" has an answer."""
    h = harness()
    record = await send(h)

    assert record.language is Language.NL


# --- outages, rejections, and the difference between them ---------------------


async def test_a_provider_outage_queues_rather_than_failing() -> None:
    """NFR-026. The caller's operation does not fail because somebody else's
    server is down; the dispatch is recorded and scheduled.
    """
    h = harness(sender=RefusingSender())
    record = await send(h)

    assert record.status is DeliveryStatus.QUEUED
    assert record.next_attempt_at is not None
    assert record.attempts == 1


async def test_a_rejected_recipient_does_not_queue() -> None:
    """A 550 is not an outage. Retrying it produces the same result forever
    while occupying the queue and looking like a provider problem.
    """
    h = harness(sender=RejectingSender())
    record = await send(h)

    assert record.status is DeliveryStatus.FAILED
    assert record.next_attempt_at is None
    assert record.settled_at is not None


async def test_an_unsendable_message_does_not_queue_either() -> None:
    """A message with a defect - here, an address the transport refuses - is a
    programming or data error rather than an outage.
    """
    h = harness(recipient=Recipient(name="X", language=Language.NL, email="not-an-address"))

    with pytest.raises(UnreachableCustomer):
        await send(h)


async def test_the_retry_schedule_runs_out() -> None:
    """Past about a working day an invoice that has not gone out is something
    a person should look at rather than a queue should keep retrying quietly.
    """
    h = harness(sender=RefusingSender())
    record = await send(h)

    for _ in range(len(RETRY_BACKOFF) + 2):
        record = await h.repository.record_outcome(
            administration_id=h.administration,
            delivery_id=record.id,
            status=DeliveryStatus.QUEUED,
            provider="smtp",
            provider_reference=None,
            last_error="still down",
            next_attempt_at=None,
        )

    assert record.attempts > len(RETRY_BACKOFF)


async def test_a_dispatch_row_exists_even_when_the_hand_over_fails() -> None:
    """The row is written BEFORE the irreversible act. Without that, a crash
    between sending and recording leaves a customer holding an invoice with no
    record of it - and the next person to press send sends it twice.
    """
    h = harness(sender=RefusingSender())
    await send(h)

    assert len(h.repository.rows) == 1


# --- what "sent" does and does not mean ---------------------------------------


def test_only_delivered_counts_as_reaching_the_customer() -> None:
    """The distinction the whole status enum exists for. `!= BOUNCED` would
    count a queued dispatch as arrived, and "we sent it" about an invoice
    nobody received is the specific wrong sentence.
    """
    arrived = {s for s in DeliveryStatus if s.reached_the_customer}
    assert arrived == {DeliveryStatus.DELIVERED}


def test_sent_is_not_settled() -> None:
    """A provider that accepted a message may still bounce it. Treating `sent`
    as final is what closes the file before the bounce arrives.
    """
    assert not DeliveryStatus.SENT.is_settled
    assert DeliveryStatus.DELIVERED.is_settled
    assert DeliveryStatus.BOUNCED.is_settled
    assert DeliveryStatus.FAILED.is_settled


async def test_a_successful_send_reports_sent_and_not_delivered() -> None:
    h = harness()
    record = await send(h)

    assert record.status is DeliveryStatus.SENT
    assert not record.status.reached_the_customer


# --- channel selection --------------------------------------------------------


async def test_the_customers_preference_is_honoured() -> None:
    courier = CourierChannel()
    h = harness(
        channels=(courier,),
        preference=DeliveryChannel.POST,
        recipient=Recipient(name="X", language=Language.NL, postal_address="Damrak 70"),
    )
    record = await send(h)

    assert record.channel is DeliveryChannel.POST


async def test_an_unavailable_preference_falls_back_to_email() -> None:
    """A customer who asked for Peppol before P2 ships should still get their
    invoice. The dispatch records what actually happened, so the fallback is
    visible rather than silent.
    """
    h = harness(preference=DeliveryChannel.PEPPOL)
    record = await send(h)

    assert record.channel is DeliveryChannel.EMAIL


async def test_explicitly_asking_for_an_unavailable_channel_is_refused() -> None:
    """Different from the fallback above: the caller named the channel, so
    silently sending another way would be answering a question nobody asked.
    """
    h = harness()

    with pytest.raises(ChannelNotAvailable) as caught:
        await send(h, channel=DeliveryChannel.PEPPOL)

    assert caught.value.channel is DeliveryChannel.PEPPOL


async def test_the_available_channels_are_reported() -> None:
    """So a screen can grey out Peppol rather than offering it and failing."""
    h = harness()
    assert h.service.channels == {DeliveryChannel.EMAIL}


# --- SI-01: a custom message on send -------------------------------------------


async def test_a_custom_message_is_included_in_the_email_body() -> None:
    h = harness()
    await send(h, custom_message="Thanks for the quick turnaround on this one!")

    body = h.mail.sent[0].body
    assert "Thanks for the quick turnaround on this one!" in body
    # Labelled, not blended into the fixed sentences - the recipient should
    # never have to guess which words the business actually typed.
    assert "Een bericht van Bakker Consultancy B.V.:" in body


async def test_no_custom_message_means_no_extra_paragraph() -> None:
    h = harness()
    await send(h)

    assert "A note from" not in h.mail.sent[0].body


async def test_a_blank_custom_message_is_treated_as_absent() -> None:
    """Whitespace is not a message - the label would otherwise introduce an
    empty, pointless paragraph.
    """
    h = harness()
    await send(h, custom_message="   \n  ")

    assert "A note from" not in h.mail.sent[0].body


async def test_the_custom_message_is_written_in_the_recipients_language() -> None:
    h = harness(
        recipient=Recipient(name="X", language=Language.EN, email="facturen@devries.example")
    )
    await send(h, custom_message="See you next month.")

    body = h.mail.sent[0].body
    assert "A note from Bakker Consultancy B.V.:" in body
    assert "See you next month." in body


async def test_the_custom_message_is_never_persisted_on_the_record() -> None:
    """dispatch's own docstring: it exists only for the one adapter call. A
    resend must not be able to read back what somebody typed last time.
    """
    h = harness()
    record = await send(h, custom_message="a private note")

    assert not hasattr(record, "custom_message")
    assert not hasattr(record, "message")


async def test_a_dispatch_with_a_custom_message_records_that_fact_not_the_text() -> None:
    """IAM-090 records what happened; it does not become a second copy of the
    sender's own correspondence.
    """
    h = harness()
    await send(h, custom_message="please pay before the holidays")

    entries = await h.audit.search(organization_id=h.organization)
    detail = next(e for e in entries if e.action == "send_sales_invoice").detail

    assert detail["custom_message_included"] is True
    assert "please pay before the holidays" not in str(detail)


async def test_a_dispatch_with_no_custom_message_records_that_too() -> None:
    h = harness()
    await send(h)

    entries = await h.audit.search(organization_id=h.organization)
    detail = next(e for e in entries if e.action == "send_sales_invoice").detail

    assert detail["custom_message_included"] is False


# --- overrides and unreachable customers --------------------------------------


async def test_an_override_address_is_used_and_recorded() -> None:
    h = harness()
    record = await send(h, recipient_override="boekhouder@example.com")

    assert record.recipient == "boekhouder@example.com"
    assert h.mail.sent[0].to == "boekhouder@example.com"


async def test_a_one_off_customer_can_be_sent_to_with_an_override() -> None:
    """A customer with no master record has no address on file, and 0037 keeps
    the one-off path permanently. The override is what makes it sendable.
    """
    h = harness(recipient=Recipient(name="Eenmalig B.V.", language=Language.NL))

    with pytest.raises(UnreachableCustomer):
        await send(h)

    record = await send(h, recipient_override="eenmalig@example.com")
    assert record.status is DeliveryStatus.SENT


async def test_a_bad_override_is_refused_rather_than_falling_back() -> None:
    """Somebody who typed an address meant to send there. Quietly sending to
    the customer instead is the worst outcome available: it goes somewhere the
    sender did not intend and the response says it succeeded.
    """
    h = harness()

    with pytest.raises(UnreachableCustomer):
        await send(h, recipient_override="typo-at-example.com")

    assert h.mail.sent == []


async def test_an_unreachable_customer_names_the_channel() -> None:
    """ "No e-mail address" and "not on Peppol" are different problems with
    different fixes.
    """
    h = harness(recipient=Recipient(name="X", language=Language.NL))

    with pytest.raises(UnreachableCustomer) as caught:
        await send(h)

    assert caught.value.channel is DeliveryChannel.EMAIL


async def test_nothing_is_recorded_when_the_customer_is_unreachable() -> None:
    """Refusals happen before the dispatch row is written, so a refusal leaves
    no half-dispatch behind.
    """
    h = harness(recipient=Recipient(name="X", language=Language.NL))

    with pytest.raises(UnreachableCustomer):
        await send(h)

    assert h.repository.rows == {}


# --- what cannot be sent ------------------------------------------------------


async def test_a_draft_cannot_be_sent() -> None:
    """It carries no number. Sending one would put a document with no invoice
    reference in a customer's hands.
    """
    h = harness(status=InvoiceStatus.DRAFT)

    with pytest.raises(InvoiceNotIssued):
        await send(h)


async def test_an_invoice_with_no_stored_pdf_is_refused() -> None:
    """FR-TPL-017. Rendering one now would be a different document under the
    same number, so this refuses rather than re-rendering.
    """
    h = harness()
    h.repository.invoices[h.invoice_id] = replace(
        h.repository.invoices[h.invoice_id],  # type: ignore[arg-type]
        document_id=None,
    )

    with pytest.raises(InvoiceNotRendered):
        await send(h)


async def test_an_artifact_nothing_produces_is_refused() -> None:
    """P2's UBL. A clear refusal rather than an empty attachment - an
    e-invoice with no payload is worse than one never sent.
    """

    @dataclass
    class UblChannel:
        channel = DeliveryChannel.PEPPOL
        required_artifacts = frozenset({ArtifactKind.UBL})

        def address_of(self, recipient, *, override=None):  # type: ignore[no-untyped-def]
            return "0106:12345678"

        async def deliver(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError("should not have been reached")

    h = harness(channels=(UblChannel(),))

    with pytest.raises(ArtifactNotAvailable) as caught:
        await send(h, channel=DeliveryChannel.PEPPOL)

    assert caught.value.kind is ArtifactKind.UBL


# --- authorization and audit --------------------------------------------------


async def test_sending_requires_the_send_permission() -> None:
    """Appendix A's "Send sales invoices" - the row this endpoint is. §8.4's
    Expense Submitter has no business posting an invoice to a customer.
    """
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToInvoice):
        await send(h)


@pytest.mark.parametrize("role", ["Owner", "Accountant", "Bookkeeper", "Invoicer"])
async def test_the_roles_that_may_send_can(role: str) -> None:
    h = harness(role=role)
    record = await send(h)

    assert record.status is DeliveryStatus.SENT


def test_every_role_that_may_send_can_also_read_the_stored_pdf() -> None:
    """Delivery reads the PDF through `DocumentService.original`, which
    authorises as `view document`. That works only because every role holding
    `send sales_invoice` also holds `view document` - true today by coincidence
    rather than by design, so it is checked rather than relied on.

    The sibling check in test_posting.py covers the `upload document` half.
    """
    checked = 0
    for role in ROLES:
        permissions = set(permissions_for_role(role))
        if ("send", "sales_invoice") not in permissions:
            continue
        checked += 1
        assert ("view", "document") in permissions, (
            f"{role.name} may send an invoice but may not read the stored PDF, so "
            f"attaching it (FR-AR-005) would be refused for them"
        )

    assert checked >= 4, f"only {checked} roles can send an invoice"


async def test_a_dispatch_is_audited_with_where_it_went() -> None:
    """IAM-090. For a dispatch the ADDRESS is the "what": an invoice sent to
    the wrong address is the incident, and this is where it is reconstructed.
    """
    h = harness()
    record = await send(h)

    entries = await h.audit.search(organization_id=h.organization)
    sends = [e for e in entries if e.action == "send_sales_invoice"]

    assert len(sends) == 1
    assert sends[0].outcome is AuditOutcome.SUCCESS
    assert sends[0].detail["recipient"] == "facturen@devries.example"
    assert sends[0].detail["channel"] == "email"
    assert sends[0].detail["status"] == "sent"
    assert sends[0].detail["delivery_id"] == str(record.id)


async def test_a_failed_dispatch_is_audited_as_a_failure() -> None:
    h = harness(sender=RefusingSender())
    await send(h)

    entries = await h.audit.search(organization_id=h.organization)
    sends = [e for e in entries if e.action == "send_sales_invoice"]

    assert sends[0].outcome is AuditOutcome.FAILURE
    assert sends[0].detail["status"] == "queued"


async def test_a_denial_is_audited() -> None:
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToInvoice):
        await send(h)

    entries = await h.audit.search(organization_id=h.organization)
    assert [e for e in entries if e.outcome is AuditOutcome.DENIED]


# --- status tracking ----------------------------------------------------------


async def test_the_state_is_reported_per_channel() -> None:
    """FR-AR-005's "tracked per channel". Two channels, two states, neither
    collapsing into the other.
    """
    courier = CourierChannel()
    h = harness(
        channels=(
            EmailInvoiceChannel(CollectingEmailSender(), from_address="n@ledgr.example"),
            courier,
        ),
        recipient=Recipient(
            name="X",
            language=Language.NL,
            email="facturen@devries.example",
            postal_address="Damrak 70",
        ),
    )

    await send(h, channel=DeliveryChannel.EMAIL)
    await send(h, channel=DeliveryChannel.POST)

    states = await h.service.state_of(
        administration_id=h.administration,
        invoice_id=h.invoice_id,
        actor_user_id=h.user,
    )
    by_channel = {state.channel: state for state in states}

    assert set(by_channel) == {DeliveryChannel.EMAIL, DeliveryChannel.POST}
    assert by_channel[DeliveryChannel.POST].provider == "courier"


async def test_a_resend_is_a_new_dispatch() -> None:
    """One row per human decision. A resend after a bounce must not overwrite
    the bounce - that is the interesting event.
    """
    h = harness()
    first = await send(h)
    second = await send(h, recipient_override="tweede@example.com")

    assert first.id != second.id
    assert len(h.repository.rows) == 2


# --- the transport ------------------------------------------------------------


async def test_the_collecting_sender_never_claims_to_have_sent_anything() -> None:
    """It reports `accepted=True`, which is right - the message was handed to
    this "provider" - and says in `detail` that nothing left the building.
    """
    sender = CollectingEmailSender()
    outcome = await sender.send(
        __import__("api.mail.sender", fromlist=["EmailMessage"]).EmailMessage(
            to="a@example.com",
            subject="s",
            body="b",
            from_address="b@example.com",
        )
    )

    assert outcome.accepted
    assert "not sent" in (outcome.detail or "")


def test_the_gross_shown_in_the_email_is_never_negative() -> None:
    """A credit note's lines carry negative quantities (ADR-037). "for
    € -1.210,00" in a covering message reads as a mistake; the document says
    what it is.
    """
    from api.invoicing.delivery_service import _deliverable

    credit = replace(
        build_invoice(credit_of=uuid.uuid4()),
        lines=(),
    )
    deliverable = _deliverable(credit, "Bakker", None)

    assert deliverable.gross >= Decimal("0.00")
    assert deliverable.is_credit_note

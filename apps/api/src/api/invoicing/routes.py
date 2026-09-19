"""Sales invoicing's HTTP surface - FR-AR-001 .. FR-AR-004.

Registered via `register(app)` rather than `include_router`, for the reason
`api.documents.routes.register` explains at length: this FastAPI version hides
an included router's routes behind a wrapper the authorization middleware and
all four coverage checks walk straight past.

--- Amounts cross the wire as STRINGS ---

Both ways. JSON has one number type and it is a double, so an unquoted
`1234.56` has already lost the value before pydantic sees it, and NFR-031
covers the whole calculation path. The same rule `api.expenses.routes` applies
to `gross_amount`, applied here to quantities, prices and every total.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.auth.email_verification import require_verified_email
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.config import settings
from api.customers.model import (
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerNotFound,
)
from api.customers.routes import get_customer_service
from api.customers.service import CustomerService
from api.db import get_db_session
from api.documents.routes import get_document_service
from api.documents.service import DocumentService
from api.i18n.catalogue import translate
from api.i18n.formatting import format_date, format_money
from api.i18n.http import problem, request_language
from api.i18n.language import Language, parse_language
from api.invoicing.delivery import (
    ArtifactNotAvailable,
    ChannelNotAvailable,
    ChannelRegistry,
    DeliveryChannel,
    EmailInvoiceChannel,
    UnreachableCustomer,
)
from api.invoicing.delivery_repository import SqlDeliveryRepository
from api.invoicing.delivery_service import (
    DeliveryRecord,
    InvoiceDeliveryService,
    InvoiceNotRendered,
)
from api.invoicing.dunning import DunningAssessment, LadderInvalid, LadderStep, StepKind
from api.invoicing.dunning_repository import SqlDunningRepository
from api.invoicing.dunning_service import (
    ChaseItem,
    ChaseReport,
    ChaseResult,
    ChaseSkip,
    CustomerNotFoundForPause,
    DunningInvoice,
    DunningService,
    NothingToSend,
    ReminderAlreadySent,
    ReminderNotDelivered,
)
from api.invoicing.duplicates import message_for as duplicate_message
from api.invoicing.model import (
    AlreadyCredited,
    CreditNoteMismatch,
    CustomerDetailsConflict,
    CustomerDetailsMissing,
    InvoiceAlreadyIssued,
    InvoiceNotFound,
    InvoiceNotIssued,
    InvoiceView,
    NotStatutoryCompliant,
    SalesInvoice,
)
from api.invoicing.payments import (
    InvalidPaymentAmount,
    InvoiceBalance,
    InvoiceNotPayable,
    InvoicePayment,
    NoPaymentJournal,
    PaymentAlreadyVoided,
    PaymentExceedsOutstanding,
    PaymentMethod,
    PaymentNotFound,
    PaymentPostingUnavailable,
    SalesPaymentService,
)
from api.invoicing.payments_repository import SqlPaymentRepository
from api.invoicing.posting import (
    NoOpenPeriod,
    NoSalesJournal,
    PostingConfigurationMissing,
    SalesPostingService,
)
from api.invoicing.posting_repository import SqlSalesPostingRepository
from api.invoicing.quote_repository import SqlQuoteRepository
from api.invoicing.quote_service import (
    ConversionResult,
    Quote,
    QuoteConversionFailed,
    QuoteDraft,
    QuoteExpired,
    QuoteNotEditable,
    QuoteNotFound,
    QuoteService,
    QuoteTransitionInvalid,
)
from api.invoicing.quotes import (
    QuoteInvalid,
    QuoteKind,
    QuoteLine,
    QuoteStatus,
    net_total,
)
from api.invoicing.receivables import AgeBucket, AgeingReport
from api.invoicing.receivables_repository import SqlReceivablesRepository
from api.invoicing.receivables_service import (
    CustomerStatement,
    InvalidStatementPeriod,
    ReceivablesService,
    StatementCustomerNotFound,
)
from api.invoicing.recurrence import DefinitionInvalid, RecurringLine, indexed_price
from api.invoicing.recurring_repository import SqlRecurringRepository
from api.invoicing.recurring_service import (
    RecurringDefinition,
    RecurringInvoice,
    RecurringInvoiceService,
    RecurringNotFound,
    RunOutcome,
    RunStatus,
    ScheduleLocked,
)
from api.invoicing.rendering import build_invoice_renderer
from api.invoicing.repository import SqlInvoiceRepository
from api.invoicing.sepa import MandateScheme, SequenceKind
from api.invoicing.sepa_repository import SqlSepaRepository
from api.invoicing.sepa_service import (
    BatchNotCancellable,
    BatchNotFound,
    BatchRaced,
    CollectionBatch,
    CollectionInvalid,
    CollectionItem,
    CollectionNotDue,
    CreditorNotConfigured,
    ItemAlreadyDecided,
    ItemNotFound,
    Mandate,
    MandateAlreadyRevoked,
    MandateNotFound,
    MandateReferenceTaken,
    NothingToCollect,
    SepaCustomerNotFound,
    SepaDirectDebitService,
    TooManyInvoices,
)
from api.invoicing.service import InvoicingService, NewLine
from api.invoicing.statutory import describe
from api.invoicing.vat import line_net
from api.invoicing.write_off_repository import SqlWriteOffRepository
from api.invoicing.write_off_service import (
    ExpenseAccountInvalid,
    InvoiceWriteOff,
    NothingOutstanding,
    NothingToReclaim,
    NoWriteOffJournal,
    VatAlreadyReclaimed,
    VatReclaimNotYetAllowed,
    WriteOffAlreadyVoided,
    WriteOffDateInFuture,
    WriteOffNotAllowed,
    WriteOffNotFound,
    WriteOffPostingUnavailable,
    WriteOffReasonMissing,
    WriteOffService,
)
from api.ledger.service import build_ledger_service
from api.mail.sender import build_email_sender
from api.templates.assets import LogoNotRenderable, build_resolve_logo
from api.tenancy import TenantContext, get_tenant_context


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices",
        create_invoice,
        methods=["POST"],
        name="create_sales_invoice",
    )
    # MOB-005's View tab. Same path as the line above, a different method - a
    # POST creates, a GET (with no further path segment) lists.
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices",
        list_invoices,
        methods=["GET"],
        name="list_sales_invoices",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}",
        get_invoice,
        methods=["GET"],
        name="get_sales_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/lines",
        set_invoice_lines,
        methods=["PUT"],
        name="set_sales_invoice_lines",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}",
        discard_invoice,
        methods=["DELETE"],
        name="discard_sales_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/issue",
        issue_invoice,
        methods=["POST"],
        name="issue_sales_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/credit",
        credit_invoice,
        methods=["POST"],
        name="credit_sales_invoice",
    )
    # FR-AR-005. Its own endpoint rather than a step inside `issue`: an e-mail
    # is an irreversible act by somebody else's server and cannot share a
    # transaction with a posting - see migration 0041's header.
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/send",
        send_invoice,
        methods=["POST"],
        name="send_sales_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/deliveries",
        get_invoice_deliveries,
        methods=["GET"],
        name="get_sales_invoice_deliveries",
    )
    # ADR-070. Payments received against an issued invoice: the receivable's
    # other half, which dunning (SI-04) and aged receivables (SI-06) both need.
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/payments",
        record_payment,
        methods=["POST"],
        name="record_sales_invoice_payment",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/payments",
        list_payments,
        methods=["GET"],
        name="list_sales_invoice_payments",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}"
        "/payments/{payment_id}/void",
        void_payment,
        methods=["POST"],
        name="void_sales_invoice_payment",
    )
    # SI-09 (ADR-077): SEPA direct debit mandates and pain.008 files.
    for path, handler, verb, name in (
        (
            "/customers/{customer_id}/sepa-mandates",
            create_sepa_mandate,
            "POST",
            "create_sepa_mandate",
        ),
        ("/customers/{customer_id}/sepa-mandates", list_sepa_mandates, "GET", "list_sepa_mandates"),
        ("/sepa-mandates/{mandate_id}/revoke", revoke_sepa_mandate, "POST", "revoke_sepa_mandate"),
        ("/sepa-collections", create_sepa_batch, "POST", "create_sepa_collection_batch"),
        ("/sepa-collections", list_sepa_batches, "GET", "list_sepa_collection_batches"),
        ("/sepa-collections/{batch_id}", get_sepa_batch, "GET", "get_sepa_collection_batch"),
        ("/sepa-collections/{batch_id}/file", download_sepa_file, "GET", "download_sepa_file"),
        ("/sepa-collections/{batch_id}/cancel", cancel_sepa_batch, "POST", "cancel_sepa_batch"),
        (
            "/sepa-collections/{batch_id}/items/{item_id}/collected",
            record_sepa_collected,
            "POST",
            "record_sepa_collected",
        ),
        (
            "/sepa-collections/{batch_id}/items/{item_id}/failed",
            record_sepa_failed,
            "POST",
            "record_sepa_failed",
        ),
    ):
        app.add_api_route(
            "/v1/administrations/{administration_id}" + path,
            handler,
            methods=[verb],
            name=name,
        )  # SI-10 (ADR-076): bad-debt write-off with the VAT reclaim entry.
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/write-offs",
        write_off_invoice,
        methods=["POST"],
        name="write_off_sales_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/write-offs",
        list_write_offs,
        methods=["GET"],
        name="list_sales_invoice_write_offs",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}"
        "/write-offs/{write_off_id}/reclaim-vat",
        reclaim_write_off_vat,
        methods=["POST"],
        name="reclaim_sales_invoice_write_off_vat",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}"
        "/write-offs/{write_off_id}/void",
        void_write_off,
        methods=["POST"],
        name="void_sales_invoice_write_off",
    )
    # SI-04 (ADR-071): the reminder ladder.
    app.add_api_route(
        "/v1/administrations/{administration_id}/dunning",
        get_dunning_overview,
        methods=["GET"],
        name="get_dunning_overview",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/dunning/ladder",
        put_dunning_ladder,
        methods=["PUT"],
        name="put_dunning_ladder",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/dunning",
        get_invoice_dunning,
        methods=["GET"],
        name="get_sales_invoice_dunning",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/sales-invoices/{invoice_id}/dunning/send",
        send_invoice_reminder,
        methods=["POST"],
        # Returns either the JSON body or a JSONResponse (the 502 that must commit).
        response_model=None,
        name="send_sales_invoice_reminder",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/customers/{customer_id}/dunning-pause",
        pause_customer_dunning,
        methods=["PUT"],
        name="pause_customer_dunning",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/customers/{customer_id}/dunning-pause",
        resume_customer_dunning,
        methods=["DELETE"],
        name="resume_customer_dunning",
    )
    # SI-11 (ADR-073): chase everything overdue.
    app.add_api_route(
        "/v1/administrations/{administration_id}/dunning/chase",
        chase_overdue,
        methods=["POST"],
        name="chase_overdue_invoices",
    )
    # SI-07 (ADR-074): recurring invoices.
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices",
        create_recurring_invoice,
        methods=["POST"],
        name="create_recurring_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices",
        list_recurring_invoices,
        methods=["GET"],
        name="list_recurring_invoices",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices/run",
        run_recurring_invoices,
        methods=["POST"],
        name="run_recurring_invoices",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices/{recurring_id}",
        get_recurring_invoice,
        methods=["GET"],
        name="get_recurring_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices/{recurring_id}",
        update_recurring_invoice,
        methods=["PUT"],
        name="update_recurring_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices/{recurring_id}/pause",
        pause_recurring_invoice,
        methods=["POST"],
        name="pause_recurring_invoice",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/recurring-invoices/{recurring_id}/resume",
        resume_recurring_invoice,
        methods=["POST"],
        name="resume_recurring_invoice",
    )
    # SI-08 (ADR-075): quotes and order confirmations, and their conversion.
    app.add_api_route(
        "/v1/administrations/{administration_id}/quotes",
        create_quote,
        methods=["POST"],
        name="create_quote",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/quotes",
        list_quotes,
        methods=["GET"],
        name="list_quotes",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/quotes/{quote_id}",
        get_quote,
        methods=["GET"],
        name="get_quote",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/quotes/{quote_id}",
        update_quote,
        methods=["PUT"],
        name="update_quote",
    )
    for _path, _handler, _name in (
        ("sent", mark_quote_sent, "mark_quote_sent"),
        ("accept", accept_quote, "accept_quote"),
        ("decline", decline_quote, "decline_quote"),
        ("cancel", cancel_quote, "cancel_quote"),
        ("extend", extend_quote, "extend_quote"),
        ("convert", convert_quote, "convert_quote"),
    ):
        app.add_api_route(
            f"/v1/administrations/{{administration_id}}/quotes/{{quote_id}}/{_path}",
            _handler,
            methods=["POST"],
            name=_name,
        )
    # SI-06 (ADR-072): aged receivables and the customer statement.
    app.add_api_route(
        "/v1/administrations/{administration_id}/receivables/ageing",
        get_receivables_ageing,
        methods=["GET"],
        name="get_receivables_ageing",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/customers/{customer_id}/statement",
        get_customer_statement,
        methods=["GET"],
        name="get_customer_statement",
    )


async def get_invoicing_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
    # FR-AR-006. Injected as the dependency the customers package already
    # publishes, rather than constructed here, so both packages read one
    # customer master through one configuration - and so that a change to how
    # a customer service is built does not have to be remembered twice.
    customers: CustomerService = Depends(get_customer_service),
    # FR-TPL-017. Already scoped to THIS administration, because the blob store
    # inside it is constructed around this administration's encryption key -
    # the same reason api.documents.routes.get_document_service takes the path
    # parameter. That is why this factory takes it too.
    documents: DocumentService = Depends(get_document_service),
) -> InvoicingService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return InvoicingService(
        repository=SqlInvoiceRepository(session),
        authorization=authorization,
        audit_log=audit_log,
        customers=customers,
        posting=SalesPostingService(
            repository=SqlSalesPostingRepository(session),
            # `build_ledger_service` rather than the repository: CLAUDE.md's
            # first non-negotiable, and tests/ledger/test_bounded_context.py
            # fails the build if this module names api.ledger.repository.
            ledger=build_ledger_service(session, audit_log),
            documents=documents,
            # FR-TPL-001: the same `resolve_logo` construction
            # `api.templates.routes.get_template_service` uses for preview -
            # see `api.templates.assets.build_resolve_logo`'s docstring on why
            # "the same engine" (FR-TPL-008) extends to this hook too.
            renderer=build_invoice_renderer(
                settings.invoice_renderer_provider,
                resolve_logo=build_resolve_logo(
                    administration_id=administration_id, session=session
                ),
            ),
        ),
    )


async def get_delivery_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
    documents: DocumentService = Depends(get_document_service),
) -> InvoiceDeliveryService:
    """FR-AR-005's dispatcher, with this deployment's channels registered.

    The registry is built here and nowhere else, which is the one place that
    knows P0 has e-mail and not Peppol. Adding the P2 channel is one more
    `register(...)` on this line - no other file in the invoicing package
    mentions a channel by name.
    """
    registry = ChannelRegistry(
        (
            EmailInvoiceChannel(
                build_email_sender(
                    settings.email_provider,
                    host=settings.email_smtp_host,
                    port=settings.email_smtp_port,
                    username=settings.email_smtp_username,
                    password=settings.email_smtp_password,
                    use_tls=settings.email_smtp_use_tls,
                ),
                from_address=settings.email_from_address,
            ),
        )
    )
    return InvoiceDeliveryService(
        repository=SqlDeliveryRepository(session),
        registry=registry,
        documents=documents,
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


class LineBody(BaseModel):
    """One line. `quantity`, `unit_price` and `discount_percent` are strings on
    the wire and Decimal here - see the module docstring.
    """

    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_treatment: str
    discount_percent: Decimal = Decimal(0)


class InvoiceBody(BaseModel):
    """Either `customer_id`, or the customer's details typed out. Not both.

    Both paths are permanently supported (FR-AR-006's master fills 0037's
    snapshot columns in; it is not a prerequisite for invoicing somebody once).
    Sending both is refused with a 422 rather than silently resolved - see
    `api.invoicing.model.CustomerDetailsConflict`.
    """

    fiscal_year_id: uuid.UUID
    invoice_date: date
    #: FR-AR-006. When given, the customer master supplies the name, address,
    #: country, VAT number, recipient language and - unless `due_date` says
    #: otherwise - the due date from its payment terms.
    customer_id: uuid.UUID | None = None
    customer_name: str | None = None
    customer_address: str | None = None
    customer_country: str | None = None
    customer_vat_number: str | None = None
    #: FR-TPL-013's recipient language, for a one-off customer. Ignored - and
    #: refused - alongside `customer_id`, which carries the customer's own.
    customer_language: str | None = None
    supply_date: date | None = None
    due_date: date | None = None
    notes: str | None = None
    lines: list[LineBody] = Field(default_factory=list)


class LinesBody(BaseModel):
    lines: list[LineBody] = Field(default_factory=list)


def _lines(bodies: list[LineBody]) -> list[NewLine]:
    return [
        NewLine(
            description=body.description,
            quantity=body.quantity,
            unit_price=body.unit_price,
            vat_treatment=body.vat_treatment,
            discount_percent=body.discount_percent,
        )
        for body in bodies
    ]


def _view_json(view: InvoiceView, language: Language) -> dict[str, object]:
    """`language` is the READER's, for the statutory messages below.

    Not the recipient's: `legal_wording` follows the customer (FR-TPL-013)
    and is resolved upstream in the service, while these sentences are shown
    to whoever is completing the invoice. Two audiences, and swapping them
    would tell a Dutch bookkeeper in German what to fix.
    """
    invoice = view.invoice
    return {
        "id": str(invoice.id),
        "status": invoice.status.value,
        # FR-AR-004. Null on a draft: the number is allocated at issue so that
        # an abandoned draft leaves no hole in a gapless series.
        "invoice_number": invoice.invoice_number,
        "invoice_reference": invoice.invoice_reference,
        "invoice_date": invoice.invoice_date.isoformat(),
        "supply_date": invoice.supply_date.isoformat() if invoice.supply_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "customer_name": invoice.customer_name,
        "customer_address": invoice.customer_address,
        "customer_country": invoice.customer_country,
        "customer_vat_number": invoice.customer_vat_number,
        # FR-AR-006. Which master record filled the snapshot in, or null for a
        # one-off customer. Provenance, not a pointer to read the name from:
        # the four fields above are what the document SAYS, and they are frozen.
        "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
        # FR-TPL-013. The language `legal_wording` below is written in, frozen
        # with the rest of the customer snapshot.
        "customer_language": invoice.customer_language.value,
        "credits_invoice_id": (
            str(invoice.credits_invoice_id) if invoice.credits_invoice_id else None
        ),
        # FR-GL-006 and FR-TPL-017. Both null on a draft, both set on an issued
        # invoice, and never afterwards changed (migration 0040). A client
        # showing "posted" or offering a download reads these rather than
        # inferring either from `status`.
        "journal_entry_id": (str(invoice.journal_entry_id) if invoice.journal_entry_id else None),
        "document_id": str(invoice.document_id) if invoice.document_id else None,
        "notes": invoice.notes,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "lines": [
            {
                "id": str(line.id),
                "position": line.position,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "discount_percent": str(line.discount_percent),
                "vat_treatment": line.vat_treatment,
                # A line has a net amount and deliberately NO VAT amount: VAT
                # is computed per treatment group, so a client adding up a
                # per-line column would get a different total from the invoice.
                "line_net": str(line.line_net),
            }
            for line in invoice.lines
        ],
        # FR-AR-002 / EU VAT Directive art. 226: taxable amount and VAT per rate.
        "vat_groups": [
            {
                "vat_treatment": group.treatment,
                "role": group.role.value,
                # Null for the margin scheme, where stating a rate on the sale
                # price is not permitted. Never conflated with 0.
                "rate": str(group.rate) if group.rate is not None else None,
                "taxable_amount": str(group.taxable),
                "vat_amount": str(group.vat),
                "legal_wording": view.wording.get(group.treatment),
            }
            for group in view.groups
        ],
        "net_amount": str(view.net),
        "vat_amount": str(view.vat),
        "gross_amount": str(view.gross),
        # FR-AR-003: what would stop this being issued, all at once, so a
        # screen shows them together rather than one save at a time. D5: each
        # carries its own sentence, so a draft form can show what is still
        # needed without the client owning fourteen strings of its own.
        "statutory_failures": [
            {
                "field": failure.field.value,
                "line_position": failure.line_position,
                "message": message,
            }
            for failure, message in describe(view.statutory_failures, language)
        ],
        "can_be_issued": view.can_be_issued,
        # SI-13. The OB-aangifte boxes this invoice reports into - the same
        # mapping the return reads, so a draft shows its consequence before
        # anything is filed. Informational: it gates nothing.
        "rubriek_preview": {
            "boxes": [
                {
                    "code": box.code,
                    "description": box.description(language),
                    "turnover_amount": str(box.turnover),
                    # Null where the box has no VAT column, which is not the
                    # same as a VAT column of zero.
                    "vat_amount": str(box.vat) if box.vat is not None else None,
                    "treatments": list(box.treatments),
                }
                for box in view.rubriek_preview.boxes
            ],
            # Treatments whose amounts cannot be shown in a box - no mapping on
            # the invoice date, or the margin scheme - said out loud rather
            # than dropped or shown as a zero.
            "unplaced_treatments": list(view.rubriek_preview.unplaced_treatments),
        },
        # SI-12. Recent invoices to the same customer that look like this one -
        # a warning shown beside the draft, never a reason `can_be_issued` is
        # false. A client must not gate the issue button on this being empty.
        "duplicate_warnings": [
            {
                "invoice_id": str(warning.invoice_id),
                "strength": warning.strength.value,
                "status": warning.status,
                "invoice_reference": warning.invoice_reference,
                "invoice_date": warning.invoice_date.isoformat(),
                "net_amount": str(warning.net_total),
                "message": duplicate_message(warning, language),
            }
            for warning in view.duplicate_warnings
        ],
        # The legal wording has not been reviewed by a Dutch tax adviser. Said
        # out loud rather than left in a comment - see api.invoicing.wording.
        "wording_is_provisional": view.wording_is_provisional,
    }


async def create_invoice(
    administration_id: uuid.UUID,
    body: InvoiceBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-001: a draft. It carries no number and asserts nothing."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    language = parse_language(body.customer_language) if body.customer_language else None
    if body.customer_language and language is None:
        raise problem(
            request,
            422,
            "errors.customer_field_invalid",
            reason="customer_field_invalid",
            field="customer_language",
        )

    try:
        view = await service.create_draft(
            administration_id=administration_id,
            fiscal_year_id=body.fiscal_year_id,
            actor_user_id=tenant.user_id,
            invoice_date=body.invoice_date,
            customer_id=body.customer_id,
            customer_name=body.customer_name,
            customer_address=body.customer_address,
            customer_country=body.customer_country,
            customer_vat_number=body.customer_vat_number,
            customer_language=language,
            supply_date=body.supply_date,
            due_date=body.due_date,
            notes=body.notes,
            lines=_lines(body.lines),
        )
    except CustomerDetailsConflict as exc:
        raise problem(
            request,
            422,
            "errors.invoice_customer_conflict",
            reason="invoice_customer_conflict",
            fields=list(exc.fields),
        ) from exc
    except CustomerDetailsMissing as exc:
        # The opposite fault, and it needs the opposite sentence. FR-AR-003:
        # a document that cannot say who it is addressed to is not an invoice.
        raise problem(
            request,
            422,
            "errors.invoice_customer_missing",
            reason="invoice_customer_missing",
            missing_fields=list(exc.fields),
        ) from exc
    except CustomerNotFound as exc:
        raise problem(
            request, 404, "errors.customer_not_found", reason="customer_not_found"
        ) from exc
    except CustomerIsArchived as exc:
        raise problem(request, 409, "errors.customer_archived", reason="customer_archived") from exc
    except CustomerAddressIncomplete as exc:
        # FR-AR-003's requirement, met at the moment the customer is put on a
        # document rather than at the moment they were saved: a half-known
        # customer is a legitimate record, a statutory document missing an
        # address is not.
        raise problem(
            request,
            422,
            "errors.customer_address_incomplete",
            reason="customer_address_incomplete",
            missing_fields=list(exc.missing),
        ) from exc
    return _view_json(view, request_language(request))


def _invoice_summary_json(invoice: SalesInvoice) -> dict[str, object]:
    """A list row - MOB-005's View tab.

    Deliberately lighter than `_view_json`: no VAT groups, no
    `statutory_failures`, no computed totals. Those come from `_build_view`,
    which resolves rates and runs FR-AR-003's gate per invoice - a per-invoice
    cost that belongs to opening one specific document (`GET
    .../sales-invoices/{id}`), not to rendering a bounded list of them.
    """
    return {
        "id": str(invoice.id),
        "status": invoice.status.value,
        "invoice_number": invoice.invoice_number,
        "invoice_reference": invoice.invoice_reference,
        "invoice_date": invoice.invoice_date.isoformat(),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "customer_name": invoice.customer_name,
        "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
        "document_id": str(invoice.document_id) if invoice.document_id else None,
    }


async def list_invoices(
    administration_id: uuid.UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> list[dict[str, object]]:
    """MOB-005's View tab: issued and draft invoices, newest first.

    Drafts are included so a half-finished mobile-created invoice is
    resumable rather than disappearing until issued - the same reasoning
    `api.expenses.routes.list_expenses` applies to a half-finished capture.

    No cursor pagination, matching `list_expenses` and
    `api.customers.routes.list_customers` - nothing in this codebase
    paginates yet.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    invoices = await service.list_invoices(
        administration_id=administration_id, actor_user_id=tenant.user_id, limit=limit
    )
    return [_invoice_summary_json(invoice) for invoice in invoices]


async def get_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.view(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    return _view_json(view, request_language(request))


async def set_invoice_lines(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    body: LinesBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-001's edit. PUT, because lines are edited as a block."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.set_lines(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            lines=_lines(body.lines),
        )
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    except InvoiceAlreadyIssued as exc:
        raise problem(
            request, 409, "errors.sales_invoice_issued", reason="sales_invoice_issued"
        ) from exc
    return _view_json(view, request_language(request))


async def discard_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Drafts only. An issued invoice's number is part of a gapless series and
    0037 refuses the delete in a trigger (FR-AR-004).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        await service.discard_draft(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    except InvoiceAlreadyIssued as exc:
        raise problem(
            request, 409, "errors.sales_invoice_issued", reason="sales_invoice_issued"
        ) from exc
    return {"id": str(invoice_id), "status": "discarded"}


async def issue_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Send sales invoices". Issuing is the act that makes
            # a claim to somebody outside the business, which is a different
            # authority from drafting one.
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
    # IAM-010b: issuing posts to the ledger, the one action an unverified
    # address cannot perform - see api.auth.email_verification.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """FR-AR-003, FR-AR-004, FR-GL-006 and FR-TPL-017: check, resolve the
    posting, number, freeze, post, and store the PDF as issued - in one
    transaction. See `InvoicingService.issue` for why the order is what it is.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.issue(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except PostingConfigurationMissing as exc:
        # 409, not 422: nothing about the REQUEST is wrong, and nothing the
        # caller can change in the payload would fix it. The administration's
        # chart is not ready to record a sale, which is somebody else's job and
        # usually somebody else's screen. `purpose` names what to map.
        raise problem(
            request,
            409,
            "errors.sales_invoice_posting_unconfigured",
            reason="sales_invoice_posting_unconfigured",
            purpose=exc.purpose,
        ) from exc
    except NoSalesJournal as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_no_journal",
            reason="sales_invoice_no_journal",
        ) from exc
    except NoOpenPeriod as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_no_open_period",
            reason="sales_invoice_no_open_period",
        ) from exc
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    except InvoiceAlreadyIssued as exc:
        raise problem(
            request, 409, "errors.sales_invoice_issued", reason="sales_invoice_issued"
        ) from exc
    except NotStatutoryCompliant as exc:
        # 422. D5: the summary counts, and each entry carries its OWN sentence
        # saying what is missing, why the law wants it and what to do next -
        # so a client with no screen of its own still shows something a person
        # can act on, and one with a screen puts each message against its
        # input using `field`.
        raise problem(
            request,
            422,
            "errors.sales_invoice_incomplete",
            reason="sales_invoice_incomplete",
            count=len(exc.failures),
            missing_fields=[
                {
                    "field": failure.field.value,
                    "line_position": failure.line_position,
                    "message": message,
                }
                for failure, message in describe(exc.failures, request_language(request))
            ],
        ) from exc
    except LogoNotRenderable as exc:
        # 409, the same posture as PostingConfigurationMissing above: nothing
        # about THIS request is wrong, and nothing in the payload would fix
        # it - the administration's TEMPLATE is not ready to render, which is
        # usually a different screen's job (the template designer, not
        # invoice issue). `reason` names it specifically so a client can
        # offer "edit the template" rather than a generic retry.
        raise problem(
            request,
            409,
            "errors.invoice_logo_not_renderable",
            reason="invoice_logo_not_renderable",
        ) from exc
    return _view_json(view, request_language(request))


async def credit_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoicingService = Depends(get_invoicing_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
    # IAM-010b: a credit note is issued, and issuing posts - same gate as issue.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """FR-AR-001's credit: a NEW issued document pointing at the original.

    The original is never touched. CLAUDE.md's second rule, one layer above the
    ledger - the record of what was claimed survives the correction of it.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.credit(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    # A credit note is issued the moment it is created, so every refusal
    # `issue` can raise reaches here too. Restated rather than shared, because
    # this route's ladder is what a reader checks against this route's
    # behaviour - and the two lists are allowed to diverge if crediting ever
    # gains a refusal of its own.
    except PostingConfigurationMissing as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_posting_unconfigured",
            reason="sales_invoice_posting_unconfigured",
            purpose=exc.purpose,
        ) from exc
    except NoSalesJournal as exc:
        raise problem(
            request, 409, "errors.sales_invoice_no_journal", reason="sales_invoice_no_journal"
        ) from exc
    except NoOpenPeriod as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_no_open_period",
            reason="sales_invoice_no_open_period",
        ) from exc
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    except InvoiceNotIssued as exc:
        raise problem(
            request, 409, "errors.sales_invoice_not_issued", reason="sales_invoice_not_issued"
        ) from exc
    except AlreadyCredited as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_already_credited",
            reason="sales_invoice_already_credited",
        ) from exc
    except CreditNoteMismatch as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_is_credit_note",
            reason="sales_invoice_is_credit_note",
        ) from exc
    return _view_json(view, request_language(request))


# --- FR-AR-005: delivery ------------------------------------------------------


class SendBody(BaseModel):
    """All three fields are optional, and all three are overrides.

    `channel` defaults to the customer's stated preference (FR-AR-006) and
    falls back to e-mail. `to` is the address to use instead of the customer's,
    interpreted by whichever channel is used - an e-mail address for e-mail, a
    participant id for Peppol. It exists for a one-off customer with no master
    record to hold an address, and for "send a copy to their bookkeeper".

    `message` (SI-01) is a free-text note added to the covering e-mail,
    never persisted - see `InvoiceDeliveryService.dispatch`'s docstring. The
    length cap is a sanity bound on an e-mail body, not a business rule.
    """

    channel: str | None = None
    to: str | None = None
    message: str | None = Field(default=None, max_length=2000)


def _delivery_json(record: DeliveryRecord) -> dict[str, object]:
    return {
        "channel": record.channel.value,
        "status": record.status.value,
        # Whether it actually ARRIVED, which is not the same as having been
        # sent - see api.invoicing.delivery.DeliveryStatus. Answered here so a
        # client cannot get the distinction wrong by comparing strings.
        "reached_the_customer": record.status.reached_the_customer,
        "is_settled": record.status.is_settled,
        "recipient": record.recipient,
        "language": record.language.value,
        "attempts": record.attempts,
        "provider": record.provider,
        "provider_reference": record.provider_reference,
        "document_id": str(record.document_id) if record.document_id else None,
        "requested_at": record.requested_at.isoformat() if record.requested_at else None,
        "sent_at": record.sent_at.isoformat() if record.sent_at else None,
        "settled_at": record.settled_at.isoformat() if record.settled_at else None,
        "next_attempt_at": (record.next_attempt_at.isoformat() if record.next_attempt_at else None),
        # Deliberately NOT `last_error`: it is operator-facing and FR-UX-007
        # keeps developer-facing strings away from users. It is in the audit
        # entry for whoever investigates.
    }


async def send_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    body: SendBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceDeliveryService = Depends(get_delivery_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Send sales invoices" - the row this endpoint IS.
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-005: hand the stored PDF to the customer's channel.

    Answers 200 even when the provider would not take it. A mail outage is not
    the caller's mistake and NFR-026 says queued work resumes rather than
    failing the operation that scheduled it - so the dispatch comes back
    `queued` with a `next_attempt_at` and the client shows that, rather than a
    502 the user would read as "the invoice is broken".

    A 4xx is reserved for the cases the caller can actually act on: a draft, an
    unreachable customer, a channel this deployment does not have.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    channel: DeliveryChannel | None = None
    if body is not None and body.channel:
        try:
            channel = DeliveryChannel(body.channel)
        except ValueError as exc:
            raise problem(
                request,
                422,
                "errors.customer_field_invalid",
                reason="customer_field_invalid",
                field="channel",
                accepted=[member.value for member in DeliveryChannel],
            ) from exc

    try:
        record = await service.dispatch(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            channel=channel,
            recipient_override=body.to if body else None,
            custom_message=body.message if body else None,
        )
    except InvoiceNotFound as exc:
        raise problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        ) from exc
    except InvoiceNotIssued as exc:
        raise problem(
            request, 409, "errors.sales_invoice_not_issued", reason="sales_invoice_not_issued"
        ) from exc
    except InvoiceNotRendered as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_not_rendered",
            reason="sales_invoice_not_rendered",
        ) from exc
    except ChannelNotAvailable as exc:
        # 409 rather than 501: the endpoint works and the request was
        # well-formed; this deployment simply has no adapter for that channel
        # yet (Peppol is P2). `available_channels` says what it does have, so a
        # client can retry without guessing.
        raise problem(
            request,
            409,
            "errors.sales_invoice_channel_unavailable",
            reason="sales_invoice_channel_unavailable",
            channel=exc.channel.value,
            available_channels=sorted(c.value for c in service.channels),
        ) from exc
    except UnreachableCustomer as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_unreachable",
            reason="sales_invoice_unreachable",
            channel=exc.channel.value,
        ) from exc
    except ArtifactNotAvailable as exc:
        raise problem(
            request,
            409,
            "errors.sales_invoice_channel_unavailable",
            reason="sales_invoice_channel_unavailable",
            channel=exc.kind.value,
            available_channels=sorted(c.value for c in service.channels),
        ) from exc
    return _delivery_json(record)


async def get_invoice_deliveries(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: InvoiceDeliveryService = Depends(get_delivery_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """FR-AR-005's "delivery status tracked per channel", as one object.

    One entry per channel this invoice has been dispatched over - the LATEST
    dispatch for each, because that is what "the status of email for this
    invoice" means. `available_channels` is returned beside it so a screen can
    offer only what this deployment can actually send.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    records = await service.state_of(
        administration_id=administration_id,
        invoice_id=invoice_id,
        actor_user_id=tenant.user_id,
    )
    return {
        "deliveries": [_delivery_json(record) for record in records],
        "available_channels": sorted(c.value for c in service.channels),
    }


# -- SI-04 groundwork: payments received against an invoice (ADR-070) ---------


async def get_payment_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> SalesPaymentService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return SalesPaymentService(
        repository=SqlPaymentRepository(session),
        # `build_ledger_service`, not the ledger repository: CLAUDE.md's first
        # non-negotiable, enforced by tests/ledger/test_bounded_context.py.
        ledger=build_ledger_service(session, audit_log),
        authorization=authorization,
        audit_log=audit_log,
    )


class PaymentBody(BaseModel):
    """`amount` is a string on the wire and a Decimal here - see the module
    docstring. `method` is validated against `PaymentMethod` in the handler, so a
    typo is a 422 in the app's own problem format rather than pydantic's."""

    amount: Decimal
    paid_on: date
    method: str = "bank_transfer"
    bank_account_id: uuid.UUID
    reference: str | None = None


def _payment_json(payment: InvoicePayment) -> dict[str, object]:
    return {
        "id": str(payment.id),
        "invoice_id": str(payment.invoice_id),
        "amount": str(payment.amount),
        "paid_on": payment.paid_on.isoformat(),
        "method": payment.method.value,
        "reference": payment.reference,
        "bank_account_id": str(payment.bank_account_id),
        "journal_entry_id": str(payment.journal_entry_id),
        "recorded_at": payment.recorded_at.isoformat(),
        # A voided payment stays in the list: it is history, and hiding it would
        # make the ledger's reversing entry look like it corrected nothing.
        "voided_at": payment.voided_at.isoformat() if payment.voided_at else None,
        "void_journal_entry_id": (
            str(payment.void_journal_entry_id) if payment.void_journal_entry_id else None
        ),
    }


def _balance_json(balance: InvoiceBalance) -> dict[str, object]:
    return {
        "gross_amount": str(balance.gross),
        "credited_amount": str(balance.credited),
        "paid_amount": str(balance.paid),
        "written_off_amount": str(balance.written_off),
        "outstanding_amount": str(balance.outstanding),
        "state": balance.state.value,
    }


def _payment_problem(request: Request, exc: Exception) -> Exception | None:
    """The refusals recording or voiding a payment can raise, as problems.

    Shared by both handlers because most of them are the same species - the
    books are not ready, or the request names something that does not exist.
    Returns None for an exception this does not recognise, so the caller
    re-raises it rather than this function swallowing it.
    """
    if isinstance(exc, InvoiceNotFound):
        return problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        )
    if isinstance(exc, InvoiceNotPayable):
        return problem(
            request, 409, "errors.sales_invoice_not_payable", reason="sales_invoice_not_payable"
        )
    if isinstance(exc, InvalidPaymentAmount):
        return problem(
            request, 422, "errors.payment_amount_invalid", reason="payment_amount_invalid"
        )
    if isinstance(exc, PaymentExceedsOutstanding):
        return problem(
            request,
            409,
            "errors.payment_exceeds_outstanding",
            reason="payment_exceeds_outstanding",
            outstanding=format_money(exc.outstanding),
        )
    if isinstance(exc, NoPaymentJournal):
        return problem(
            request,
            409,
            "errors.payment_no_journal",
            reason="payment_no_journal",
            journal_type=exc.journal_type,
        )
    if isinstance(exc, PaymentPostingUnavailable):
        return problem(
            request,
            409,
            "errors.payment_posting_unavailable",
            reason="payment_posting_unavailable",
            missing=exc.missing,
        )
    if isinstance(exc, NoOpenPeriod):
        return problem(
            request, 409, "errors.payment_no_open_period", reason="payment_no_open_period"
        )
    if isinstance(exc, PaymentNotFound):
        return problem(request, 404, "errors.payment_not_found", reason="payment_not_found")
    if isinstance(exc, PaymentAlreadyVoided):
        return problem(
            request, 409, "errors.payment_already_voided", reason="payment_already_voided"
        )
    return None


async def record_payment(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    body: PaymentBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SalesPaymentService = Depends(get_payment_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Post journal entries" - NOT `send sales_invoice`. The
            # person who raises invoices must not be the one who records that
            # cash arrived against them; see api.invoicing.payments.
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    # IAM-010b: this posts to the ledger.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """FR-AR-010's groundwork: record money received against an issued invoice.

    Posts `Dr bank / Cr Debiteuren` through the ledger in the same transaction, so
    the invoice's outstanding balance and the debtor's sub-ledger balance fall by
    the same amount.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        method = PaymentMethod(body.method)
    except ValueError:
        raise problem(
            request, 422, "errors.payment_method_invalid", reason="payment_method_invalid"
        ) from None
    try:
        payment = await service.record(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            amount=body.amount,
            paid_on=body.paid_on,
            method=method,
            bank_account_id=body.bank_account_id,
            reference=body.reference,
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _payment_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"payment": _payment_json(payment), "balance": _balance_json(balance)}


async def list_payments(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SalesPaymentService = Depends(get_payment_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """Every payment against one invoice - voided ones included - and what the
    invoice still owes."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        payments = await service.payments(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _payment_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {
        "payments": [_payment_json(payment) for payment in payments],
        "balance": _balance_json(balance),
    }


async def void_payment(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SalesPaymentService = Depends(get_payment_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Reverse a posting": undoing a receipt is a reversing
            # entry (FR-GL-003), and it is the same authority as any other.
            "reverse",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Undo a payment by reversing its entry. The invoice is owed again."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        payment = await service.void(
            administration_id=administration_id,
            invoice_id=invoice_id,
            payment_id=payment_id,
            actor_user_id=tenant.user_id,
            void_date=date.today(),
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _payment_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"payment": _payment_json(payment), "balance": _balance_json(balance)}


# -- SI-04: the reminder ladder (ADR-071) --------------------------------------


async def get_dunning_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
    # One session, so the dispatch row, the reminder row and the audit entries are
    # one transaction. The delivery service is the same one `send` uses: a reminder
    # is a dispatch, not a second email system.
    delivery: InvoiceDeliveryService = Depends(get_delivery_service),
) -> DunningService:
    return DunningService(
        repository=SqlDunningRepository(session),
        delivery=delivery,
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


class LadderStepBody(BaseModel):
    position: int
    days_after_due: int
    kind: str
    charge_interest: bool = False
    charge_collection_cost: bool = False


class LadderBody(BaseModel):
    steps: list[LadderStepBody] = Field(default_factory=list)


class PauseBody(BaseModel):
    reason: str | None = None


def _step_json(step: LadderStep) -> dict[str, object]:
    return {
        "position": step.position,
        "days_after_due": step.days_after_due,
        "kind": step.kind.value,
        "charge_interest": step.charge_interest,
        "charge_collection_cost": step.charge_collection_cost,
    }


def _assessment_json(
    invoice: DunningInvoice, assessment: DunningAssessment, language: Language
) -> dict[str, object]:
    blocker = assessment.blocker
    return {
        "invoice_id": str(invoice.invoice_id),
        "invoice_reference": invoice.invoice_reference,
        "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
        "customer_name": invoice.customer_name,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "days_overdue": assessment.days_overdue,
        "outstanding_amount": str(assessment.outstanding),
        "can_send": assessment.can_send,
        # A code for a client to branch on, and a sentence for one that will not.
        "blocker": blocker.value if blocker else None,
        "blocker_message": (
            translate(f"invoice.dunning.blocker.{blocker.value}", language) if blocker else None
        ),
        # The step in question, including when it is not yet due, so a screen can
        # say "step 2 in 4 days". Null only when there is nothing left to send.
        "next_step": _step_json(assessment.step) if assessment.step else None,
        # What THIS step will claim. Null where it claims nothing - never "0.00",
        # which would put the idea of a claim in a friendly reminder.
        "interest_amount": str(assessment.interest) if assessment.interest is not None else None,
        "collection_cost_amount": (
            str(assessment.collection_cost) if assessment.collection_cost is not None else None
        ),
        "pay_by": assessment.pay_by.isoformat() if assessment.pay_by else None,
        # A judgement the assessment surfaces rather than hides: there is no
        # consumer flag on the customer, so this is inferred (a VAT or KvK number).
        "is_business": invoice.is_business,
        "interest_kind": assessment.interest_kind.value,
        "is_paused": invoice.is_paused,
        "sent_steps": sorted(invoice.sent_positions),
    }


def _dunning_problem(request: Request, exc: Exception) -> Exception | None:
    if isinstance(exc, InvoiceNotFound):
        return problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        )
    if isinstance(exc, CustomerNotFoundForPause):
        return problem(request, 404, "errors.customer_not_found", reason="customer_not_found")
    if isinstance(exc, NothingToSend):
        why = exc.assessment.blocker
        return problem(
            request,
            409,
            "errors.dunning_nothing_to_send",
            reason="dunning_nothing_to_send",
            blocker=why.value if why else None,
            reason_text=(
                translate(f"invoice.dunning.blocker.{why.value}", request_language(request))
                if why
                else ""
            ),
        )
    if isinstance(exc, ReminderNotDelivered):
        return problem(
            request,
            502,
            "errors.dunning_reminder_not_delivered",
            reason="dunning_reminder_not_delivered",
            delivery_status=exc.delivery.status.value,
        )
    if isinstance(exc, ReminderAlreadySent):
        return problem(
            request, 409, "errors.dunning_reminder_already_sent", reason="dunning_reminder_sent"
        )
    if isinstance(exc, LadderInvalid):
        return problem(
            request, 422, "errors.dunning_ladder_invalid", reason="dunning_ladder_invalid"
        )
    return None


def _raise_dunning(request: Request, exc: Exception) -> NoReturn:
    refusal = _dunning_problem(request, exc)
    if refusal is None:
        raise exc
    raise refusal from exc


async def get_dunning_overview(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """Every overdue invoice with what should happen to it today, and the ladder
    in force. Read-only: nothing is sent by looking."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    language = request_language(request)
    try:
        steps, is_default = await service.ladder(
            administration_id=administration_id, actor_user_id=tenant.user_id
        )
        rows = await service.overview(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return {
        "ladder": {"is_default": is_default, "steps": [_step_json(s) for s in steps]},
        "overdue": [_assessment_json(invoice, result, language) for invoice, result in rows],
    }


async def put_dunning_ladder(
    administration_id: uuid.UUID,
    body: LadderBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Replace the whole ladder. Validated first: a ladder that would claim what
    the law does not allow is refused here, not discovered when a customer is
    mailed. An empty list is valid and means "chase nobody"."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        steps = [
            LadderStep(
                position=s.position,
                days_after_due=s.days_after_due,
                kind=StepKind(s.kind),
                charge_interest=s.charge_interest,
                charge_collection_cost=s.charge_collection_cost,
            )
            for s in body.steps
        ]
    except ValueError:
        raise problem(
            request, 422, "errors.dunning_ladder_invalid", reason="dunning_ladder_invalid"
        ) from None
    try:
        ordered = await service.set_ladder(
            administration_id=administration_id, actor_user_id=tenant.user_id, steps=steps
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return {"is_default": False, "steps": [_step_json(s) for s in ordered]}


async def get_invoice_dunning(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """What should happen to ONE invoice today - including "nothing, because...\""""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        invoice, result = await service.assessment(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return _assessment_json(invoice, result, request_language(request))


async def send_invoice_reminder(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Send sales invoices": a reminder is a claim to somebody
            # outside the business, the act that permission names (ADR-012).
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object] | JSONResponse:
    """Send the NEXT reminder step for one invoice, if the ladder says one is due.

    Always the lowest step not yet sent - an invoice far overdue still gets step 1
    first. 409 with the reason when nothing is due; 502 when the provider did not
    accept it (nothing is recorded, the step stays due).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        sent = await service.send_next(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except ReminderNotDelivered as exc:
        # RETURNED, not raised. The request runs in one transaction that a raised
        # exception rolls back - taking the failed dispatch's row and its FAILURE
        # audit entry with it, and leaving no record that an attempt was made.
        # Nothing was e-mailed, so the 502 is honest; the record is worth keeping.
        return _not_delivered_response(request, exc)
    except Exception as exc:
        _raise_dunning(request, exc)
    step = sent.assessment.step
    return {
        "step": _step_json(step) if step else None,
        "delivery": _delivery_json(sent.delivery),
        "outstanding_amount": str(sent.assessment.outstanding),
        "interest_amount": (
            str(sent.assessment.interest) if sent.assessment.interest is not None else None
        ),
        "collection_cost_amount": (
            str(sent.assessment.collection_cost)
            if sent.assessment.collection_cost is not None
            else None
        ),
        "pay_by": sent.assessment.pay_by.isoformat() if sent.assessment.pay_by else None,
    }


async def pause_customer_dunning(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    body: PauseBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Stop chasing this customer's invoices. Idempotent."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        await service.pause(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
            reason=body.reason,
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return {"customer_id": str(customer_id), "is_paused": True}


async def resume_customer_dunning(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Resume chasing where the ladder stood: steps already sent stay sent."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        await service.resume(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return {"customer_id": str(customer_id), "is_paused": False}


# -- SI-06: aged receivables and the customer statement (ADR-072) -----------------


async def get_receivables_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ReceivablesService:
    return ReceivablesService(
        repository=SqlReceivablesRepository(session),
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


def _ageing_json(report: AgeingReport, language: Language) -> dict[str, object]:
    def label(bucket: AgeBucket) -> str:
        return translate(f"invoice.ageing.bucket.{bucket.value}", language)

    return {
        "as_of": report.as_of.isoformat(),
        # In display order. A list rather than an object so a client cannot depend
        # on key order, and each carries its own label.
        "buckets": [
            {
                "bucket": bucket.value,
                "label": label(bucket),
                "amount": str(report.bucket_totals[bucket]),
            }
            for bucket in AgeBucket
        ],
        "grand_total": str(report.grand_total),
        "customers": [
            {
                "customer_id": str(customer.customer_id) if customer.customer_id else None,
                "customer_name": customer.customer_name,
                "total": str(customer.total),
                "buckets": {b.value: str(customer.buckets[b]) for b in AgeBucket},
                # FR-RPT-002: down to the invoice, and from there to its document.
                "invoices": [
                    {
                        "invoice_id": str(found.invoice_id),
                        "invoice_reference": found.invoice_reference,
                        "invoice_date": found.invoice_date.isoformat(),
                        "due_date": found.due_date.isoformat() if found.due_date else None,
                        "outstanding_amount": str(found.outstanding),
                        "bucket": bucket.value,
                        "days_late": days_late,
                    }
                    for found, bucket, days_late in customer.items
                ],
            }
            for customer in report.customers
        ],
    }


def _statement_json(result: CustomerStatement, language: Language) -> dict[str, object]:
    statement = result.statement
    return {
        "customer_id": str(result.customer_id),
        "customer_name": result.customer_name,
        "date_from": statement.date_from.isoformat(),
        "date_to": statement.date_to.isoformat(),
        # Positive: the customer owes. Negative: they are in credit.
        "opening_balance": str(statement.opening_balance),
        "lines": [
            {
                "date": line.movement.movement_date.isoformat(),
                "kind": line.movement.kind.value,
                "label": translate(f"invoice.statement.kind.{line.movement.kind.value}", language),
                "reference": line.movement.reference,
                "invoice_id": str(line.movement.invoice_id) if line.movement.invoice_id else None,
                "debit": str(line.movement.debit),
                "credit": str(line.movement.credit),
                "balance": str(line.balance),
            }
            for line in statement.lines
        ],
        "total_debit": str(statement.total_debit),
        "total_credit": str(statement.total_credit),
        "closing_balance": str(statement.closing_balance),
    }


async def get_receivables_ageing(
    administration_id: uuid.UUID,
    request: Request,
    as_of: date | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ReceivablesService = Depends(get_receivables_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "View reports" - not `create sales_invoice`, which would
            # let an Invoicer read every customer's debt for having drafted one.
            "view",
            "report",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """FR-AR-012: who owes what and how overdue, as of a date (today by default).

    Reproducible: the same `as_of` gives the same report next month, because every
    payment and credit is counted by its own date. Drills down to the invoices.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    report = await service.ageing(
        administration_id=administration_id,
        actor_user_id=tenant.user_id,
        as_of=as_of or date.today(),
    )
    return _ageing_json(report, request_language(request))


async def get_customer_statement(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    # `from` is a Python keyword, so the parameter is `date_from` and the URL says `from`.
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ReceivablesService = Depends(get_receivables_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """FR-AR-012: one customer's statement of account. `to` defaults to today and
    `from` to the start of that year. Everything before `from` is folded into the
    opening balance, so any period's statement starts from what was truly owed."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    end = date_to or date.today()
    start = date_from or date(end.year, 1, 1)
    try:
        result = await service.statement(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
            date_from=start,
            date_to=end,
        )
    except InvalidStatementPeriod as exc:
        raise problem(
            request, 422, "errors.report_period_invalid", reason="report_period_invalid"
        ) from exc
    except StatementCustomerNotFound as exc:
        raise problem(
            request, 404, "errors.customer_not_found", reason="customer_not_found"
        ) from exc
    return _statement_json(result, request_language(request))


# -- SI-11: chase everything overdue (ADR-073) ----------------------------------------


def _not_delivered_response(request: Request, exc: ReminderNotDelivered) -> JSONResponse:
    """The 502 for a provider that did not accept a reminder, as a RESPONSE so the
    transaction commits the failed dispatch's record. Same body shape `problem`
    gives, so a client cannot tell the difference."""
    refusal = problem(
        request,
        502,
        "errors.dunning_reminder_not_delivered",
        reason="dunning_reminder_not_delivered",
        delivery_status=exc.delivery.status.value,
    )
    return JSONResponse(status_code=refusal.status_code, content={"detail": refusal.detail})


class ChaseItemBody(BaseModel):
    invoice_id: uuid.UUID
    #: The step the person REVIEWED. If it is no longer the next one, the invoice is
    #: skipped (`step_changed`) rather than sent as whatever the step has become.
    step_position: int


class ChaseBody(BaseModel):
    #: Omitted or null: "chase everything" - friendly and ordinary reminders only, a
    #: formal notice is never sent this way. Given: exactly these, and naming a
    #: formal notice here IS the explicit confirmation.
    items: list[ChaseItemBody] | None = None


def _chase_json(report: ChaseReport, language: Language) -> dict[str, object]:
    def result_json(result: ChaseResult) -> dict[str, object]:
        invoice, assessment = result.invoice, result.assessment
        blocker = assessment.blocker if assessment else None
        return {
            "invoice_id": str(result.invoice_id),
            "invoice_reference": invoice.invoice_reference if invoice else None,
            "customer_name": invoice.customer_name if invoice else None,
            "status": result.status.value,
            "step": _step_json(assessment.step) if assessment and assessment.step else None,
            "skip": result.skip.value if result.skip else None,
            "skip_message": (
                translate(f"invoice.chase.skip.{result.skip.value}", language)
                if result.skip and result.skip is not ChaseSkip.BLOCKED
                else (
                    translate(f"invoice.dunning.blocker.{blocker.value}", language)
                    if result.skip and blocker
                    else None
                )
            ),
            "blocker": blocker.value if blocker else None,
            "failure": result.failure.value if result.failure else None,
            "failure_message": (
                translate(f"invoice.chase.failure.{result.failure.value}", language)
                if result.failure
                else None
            ),
            "delivery": _delivery_json(result.delivery) if result.delivery else None,
        }

    return {
        "summary": {
            "sent": report.sent,
            "skipped": report.skipped,
            "failed": report.failed,
            # Over the per-call cap: call again. Anyone already reminded is
            # `too_soon`, so repeating the call is safe.
            "deferred": report.deferred,
        },
        "results": [result_json(result) for result in report.results],
    }


async def chase_overdue(
    administration_id: uuid.UUID,
    request: Request,
    body: ChaseBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DunningService = Depends(get_dunning_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Send sales invoices" - as sending one reminder.
            "send",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """SI-11: send the next reminder step to every overdue invoice that is due one.

    Always 200 with a per-invoice report: one customer's missing address or a
    provider refusal does not stop the rest. Re-assesses every invoice at send time.
    `items` (invoice + the step reviewed) sends exactly those; omitted, it sends
    friendly and ordinary reminders only and reports formal notices as
    `needs_confirmation`.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    items = (
        None
        if body is None or body.items is None
        else [ChaseItem(i.invoice_id, i.step_position) for i in body.items]
    )
    try:
        report = await service.chase(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
            items=items,
        )
    except Exception as exc:
        _raise_dunning(request, exc)
    return _chase_json(report, request_language(request))


# -- SI-07: recurring invoices (ADR-074) --------------------------------------------------


async def get_recurring_service(
    administration_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
    # The same service `POST .../sales-invoices` uses, so a schedule's invoices are
    # created, numbered and posted by exactly the code a person's are.
    invoicing: InvoicingService = Depends(get_invoicing_service),
) -> RecurringInvoiceService:
    return RecurringInvoiceService(
        repository=SqlRecurringRepository(session),
        invoicing=invoicing,
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


class RecurringBody(BaseModel):
    """Amounts are strings on the wire and Decimal here - see the module docstring."""

    customer_id: uuid.UUID
    name: str
    interval_months: int
    start_date: date
    end_date: date | None = None
    max_runs: int | None = None
    due_days: int | None = None
    indexation_percent: Decimal = Decimal(0)
    auto_issue: bool = False
    notes: str | None = None
    lines: list[LineBody] = Field(default_factory=list)


def _definition(body: RecurringBody) -> RecurringDefinition:
    return RecurringDefinition(
        customer_id=body.customer_id,
        name=body.name,
        interval_months=body.interval_months,
        start_date=body.start_date,
        end_date=body.end_date,
        max_runs=body.max_runs,
        due_days=body.due_days,
        indexation_percent=body.indexation_percent,
        auto_issue=body.auto_issue,
        notes=body.notes,
        lines=tuple(
            RecurringLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_treatment=line.vat_treatment,
                discount_percent=line.discount_percent,
            )
            for line in body.lines
        ),
    )


def _recurring_json(schedule: RecurringInvoice, language: Language) -> dict[str, object]:
    definition = schedule.definition
    upcoming = schedule.next_run_on
    return {
        "id": str(schedule.id),
        "customer_id": str(definition.customer_id),
        "name": definition.name,
        "interval_months": definition.interval_months,
        "start_date": definition.start_date.isoformat(),
        "end_date": definition.end_date.isoformat() if definition.end_date else None,
        "max_runs": definition.max_runs,
        "due_days": definition.due_days,
        "indexation_percent": str(definition.indexation_percent),
        "auto_issue": definition.auto_issue,
        "notes": definition.notes,
        "status": schedule.status.value,
        "runs_generated": schedule.runs_generated,
        "next_run_on": upcoming.isoformat() if upcoming else None,
        "last_error": schedule.last_error,
        "last_error_message": (
            translate(f"invoice.recurring.error.{schedule.last_error}", language)
            if schedule.last_error
            else None
        ),
        "lines": [
            {
                "description": line.description,
                "quantity": str(line.quantity),
                # What is AGREED (the base), and what the NEXT invoice will charge
                # once indexation is applied - so a screen can show a coming increase
                # before the customer is billed it.
                "unit_price": str(line.unit_price),
                "next_unit_price": (
                    str(
                        indexed_price(
                            line.unit_price,
                            definition.indexation_percent,
                            definition.start_date,
                            upcoming,
                        )
                    )
                    if upcoming
                    else None
                ),
                "discount_percent": str(line.discount_percent),
                "vat_treatment": line.vat_treatment,
            }
            for line in definition.lines
        ],
    }


def _outcome_json(outcome: RunOutcome, language: Language) -> dict[str, object]:
    return {
        "schedule_id": str(outcome.schedule_id),
        "schedule_name": outcome.schedule_name,
        "run_date": outcome.run_date.isoformat(),
        "status": outcome.status.value,
        "invoice_id": str(outcome.invoice_id) if outcome.invoice_id else None,
        "issued": outcome.issued,
        # Set when the schedule asked to issue and the invoice was left a draft.
        "issue_error": outcome.issue_error,
        "issue_error_message": (
            translate(f"invoice.recurring.issue_error.{outcome.issue_error}", language)
            if outcome.issue_error
            else None
        ),
        "error": outcome.error,
        "error_message": (
            translate(f"invoice.recurring.error.{outcome.error}", language)
            if outcome.error
            else None
        ),
    }


def _recurring_problem(request: Request, exc: Exception) -> Exception | None:
    if isinstance(exc, RecurringNotFound):
        return problem(request, 404, "errors.recurring_not_found", reason="recurring_not_found")
    if isinstance(exc, CustomerNotFound):
        return problem(request, 404, "errors.customer_not_found", reason="customer_not_found")
    if isinstance(exc, DefinitionInvalid):
        return problem(request, 422, "errors.recurring_invalid", reason="recurring_invalid")
    if isinstance(exc, ScheduleLocked):
        return problem(
            request, 409, "errors.recurring_locked", reason="recurring_locked", field=exc.field
        )
    return None


def _raise_recurring(request: Request, exc: Exception) -> NoReturn:
    refusal = _recurring_problem(request, exc)
    if refusal is None:
        raise exc
    raise refusal from exc


async def create_recurring_invoice(
    administration_id: uuid.UUID,
    body: RecurringBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-008: a schedule that generates invoices on a rhythm. Validated when
    saved - an unrunnable schedule is refused now, not on the first of the month."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        created = await service.create(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            definition=_definition(body),
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return _recurring_json(created, request_language(request))


async def list_recurring_invoices(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create", "sales_invoice", scope=administration_from_path("administration_id")
        )
    ),
) -> list[dict[str, object]]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    language = request_language(request)
    schedules = await service.list(
        administration_id=administration_id, actor_user_id=tenant.user_id
    )
    return [_recurring_json(schedule, language) for schedule in schedules]


async def get_recurring_invoice(
    administration_id: uuid.UUID,
    recurring_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create", "sales_invoice", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        schedule = await service.get(
            administration_id=administration_id,
            recurring_id=recurring_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return _recurring_json(schedule, request_language(request))


async def update_recurring_invoice(
    administration_id: uuid.UUID,
    recurring_id: uuid.UUID,
    body: RecurringBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Replace the definition. Applies to FUTURE runs only; the start date, interval
    and customer are locked once the schedule has generated an invoice."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        updated = await service.update(
            administration_id=administration_id,
            recurring_id=recurring_id,
            actor_user_id=tenant.user_id,
            definition=_definition(body),
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return _recurring_json(updated, request_language(request))


async def pause_recurring_invoice(
    administration_id: uuid.UUID,
    recurring_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Stop generating. Idempotent."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        schedule = await service.pause(
            administration_id=administration_id,
            recurring_id=recurring_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return _recurring_json(schedule, request_language(request))


async def resume_recurring_invoice(
    administration_id: uuid.UUID,
    recurring_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Start again where it stood. Runs that fell due while paused are NOT skipped:
    they are generated, each dated in its own month. A pause postpones billing; it
    does not waive it."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        schedule = await service.resume(
            administration_id=administration_id,
            recurring_id=recurring_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return _recurring_json(schedule, request_language(request))


async def run_recurring_invoices(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: RecurringInvoiceService = Depends(get_recurring_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
    # IAM-010b: a schedule with `auto_issue` posts to the ledger, the one action an
    # unverified address cannot perform - the same gate `issue` carries.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Generate every invoice that should exist by today and does not.

    Idempotent: calling it twice generates each scheduled date once. Never sends
    anything. Always 200 with a per-run report; a schedule whose run fails stops
    there (later dates are not attempted out of order) and the others carry on.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    language = request_language(request)
    try:
        outcomes = await service.run_due(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        _raise_recurring(request, exc)
    return {
        "summary": {
            "generated": sum(1 for o in outcomes if o.status is RunStatus.GENERATED),
            "already_generated": sum(
                1 for o in outcomes if o.status is RunStatus.ALREADY_GENERATED
            ),
            "failed": sum(1 for o in outcomes if o.status is RunStatus.FAILED),
            "issued": sum(1 for o in outcomes if o.issued),
            # Generated but not issued although the schedule asked to be: somebody
            # needs to finish these.
            "left_as_draft": sum(1 for o in outcomes if o.issue_error is not None),
        },
        "results": [_outcome_json(outcome, language) for outcome in outcomes],
    }


# -- SI-08: quotes and order confirmations (ADR-075) --------------------------------------


async def get_quote_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
    # The same service `POST .../sales-invoices` uses: a converted quote becomes an
    # ordinary draft invoice, raised by exactly the code a person's is.
    invoicing: InvoicingService = Depends(get_invoicing_service),
) -> QuoteService:
    return QuoteService(
        repository=SqlQuoteRepository(session),
        invoicing=invoicing,
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


class QuoteBody(BaseModel):
    kind: str = "quote"
    customer_id: uuid.UUID
    subject: str | None = None
    valid_until: date | None = None
    notes: str | None = None
    lines: list[LineBody] = Field(default_factory=list)


class AcceptBody(BaseModel):
    #: Who accepted, on the customer's side, and their own reference (a purchase-order
    #: number). Free text the customer supplied; both optional.
    accepted_by_name: str | None = None
    reference: str | None = None


class DeclineBody(BaseModel):
    reason: str | None = None


class ExtendBody(BaseModel):
    valid_until: date


class ConvertBody(BaseModel):
    #: Defaults to today.
    invoice_date: date | None = None


def _quote_draft(body: QuoteBody) -> QuoteDraft:
    try:
        kind = QuoteKind(body.kind)
    except ValueError:
        raise QuoteInvalid("the kind is quote or order_confirmation") from None
    return QuoteDraft(
        kind=kind,
        customer_id=body.customer_id,
        subject=body.subject.strip() if body.subject and body.subject.strip() else None,
        valid_until=body.valid_until,
        notes=body.notes,
        lines=tuple(
            QuoteLine(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_treatment=line.vat_treatment,
                discount_percent=line.discount_percent,
            )
            for line in body.lines
        ),
    )


def _quote_json(quote: Quote, today: date) -> dict[str, object]:
    def stamp(value: date | datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    return {
        "id": str(quote.id),
        "kind": quote.kind.value,
        "reference": quote.reference,
        "customer_id": str(quote.customer_id),
        "subject": quote.subject,
        "valid_until": stamp(quote.valid_until),
        "notes": quote.notes,
        "status": quote.status.value,
        # Derived, never stored: only a quote still OUT can expire.
        "is_expired": quote.is_expired(today),
        # NET only, deliberately: the VAT rate is the one in force on the INVOICE date
        # (CMP-014), so a VAT figure here would be a promise about a rate that can change.
        "net_amount": str(net_total(quote.lines)),
        "sent_at": stamp(quote.sent_at),
        "accepted_at": stamp(quote.accepted_at),
        "accepted_by_name": quote.accepted_by_name,
        "acceptance_reference": quote.acceptance_reference,
        "declined_at": stamp(quote.declined_at),
        "decline_reason": quote.decline_reason,
        "cancelled_at": stamp(quote.cancelled_at),
        "converted_at": stamp(quote.converted_at),
        "converted_invoice_id": str(quote.converted_invoice_id)
        if quote.converted_invoice_id
        else None,
        "lines": [
            {
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "discount_percent": str(line.discount_percent),
                "vat_treatment": line.vat_treatment,
                "line_net": str(
                    line_net(
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        discount_percent=line.discount_percent,
                    )
                ),
            }
            for line in quote.lines
        ],
    }


def _quote_problem(request: Request, exc: Exception) -> Exception | None:
    if isinstance(exc, QuoteNotFound):
        return problem(request, 404, "errors.quote_not_found", reason="quote_not_found")
    if isinstance(exc, CustomerNotFound):
        return problem(request, 404, "errors.customer_not_found", reason="customer_not_found")
    if isinstance(exc, QuoteInvalid):
        return problem(request, 422, "errors.quote_invalid", reason="quote_invalid")
    if isinstance(exc, QuoteTransitionInvalid):
        return problem(
            request,
            409,
            "errors.quote_transition_invalid",
            reason="quote_transition_invalid",
            current=exc.current.value,
            target=exc.target.value,
        )
    if isinstance(exc, QuoteExpired):
        return problem(request, 409, "errors.quote_expired", reason="quote_expired")
    if isinstance(exc, QuoteNotEditable):
        return problem(
            request,
            409,
            "errors.quote_not_editable",
            reason="quote_not_editable",
            status=exc.status.value,
        )
    if isinstance(exc, QuoteConversionFailed):
        return problem(
            request,
            409,
            "errors.quote_conversion_failed",
            reason="quote_conversion_failed",
            code=exc.code,
            reason_text=translate(
                f"invoice.quote.conversion_error.{exc.code}", request_language(request)
            ),
        )
    return None


def _raise_quote(request: Request, exc: Exception) -> NoReturn:
    refusal = _quote_problem(request, exc)
    if refusal is None:
        raise exc
    raise refusal from exc


async def create_quote(
    administration_id: uuid.UUID,
    body: QuoteBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-007: a numbered quote or order confirmation, as a draft. Asserts nothing
    in the books - no invoice number, no posting, no VAT."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.create(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            draft=_quote_draft(body),
            today=date.today(),
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def list_quotes(
    administration_id: uuid.UUID,
    request: Request,
    status: str | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> list[dict[str, object]]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        wanted = QuoteStatus(status) if status else None
    except ValueError:
        raise problem(request, 422, "errors.quote_invalid", reason="quote_status_invalid") from None
    quotes = await service.list(
        administration_id=administration_id, actor_user_id=tenant.user_id, status=wanted
    )
    today = date.today()
    return [_quote_json(quote, today) for quote in quotes]


async def get_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.get(
            administration_id=administration_id, quote_id=quote_id, actor_user_id=tenant.user_id
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def update_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    body: QuoteBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Replace a DRAFT. Once sent, a quote is what the customer was told: cancel it
    and create a new one, or only extend its validity."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.update(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            draft=_quote_draft(body),
            today=date.today(),
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def mark_quote_sent(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Record that the quote has gone to the customer. Does not send it: there is no
    quote PDF or e-mail yet (ADR-075); it says a person did."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.mark_sent(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def accept_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    body: AcceptBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Record the customer's acceptance. Refused once the offer has expired."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.accept(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
            accepted_by_name=body.accepted_by_name if body else None,
            acceptance_reference=body.reference if body else None,
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def decline_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    body: DeclineBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.decline(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
            reason=body.reason if body else None,
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def cancel_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Withdraw a quote that is still a draft, sent or accepted. A quote is cancelled,
    never deleted: "we never offered that" must stay answerable."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.cancel(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def extend_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    body: ExtendBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Push out the validity of a quote that is still out - the one edit allowed after
    sending, because it can only help the customer. Revives an expired quote."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        quote = await service.extend_validity(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            valid_until=body.valid_until,
            today=date.today(),
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return _quote_json(quote, date.today())


async def convert_quote(
    administration_id: uuid.UUID,
    quote_id: uuid.UUID,
    request: Request,
    body: ConvertBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuoteService = Depends(get_quote_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Create sales invoices" (ADR-012): a quote is a pre-invoice
            # commercial document, drafted by the same people who draft invoices.
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-007: turn an ACCEPTED quote into a DRAFT invoice with exactly its lines.

    A draft, not an issued invoice: a numbered, posted invoice with no human looking at
    it is the bulk-click problem again. Idempotent - converting a converted quote
    returns its invoice (`already_converted`), so a retry cannot invoice twice.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        result: ConversionResult = await service.convert(
            administration_id=administration_id,
            quote_id=quote_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
            invoice_date=body.invoice_date if body else None,
        )
    except Exception as exc:
        _raise_quote(request, exc)
    return {
        "invoice_id": str(result.invoice_id),
        "already_converted": result.already_converted,
        "quote": _quote_json(result.quote, date.today()),
    }


# -- SI-10: bad-debt write-off (ADR-076) ---------------------------------------


async def get_write_off_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> WriteOffService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return WriteOffService(
        repository=SqlWriteOffRepository(session),
        ledger=build_ledger_service(session, audit_log),
        authorization=authorization,
        audit_log=audit_log,
    )


class WriteOffBody(BaseModel):
    """`written_off_on` defaults to today. `reclaim_vat` asks for the VAT reclaim entry in the
    same request; it is refused (and nothing is written) while the waiting period runs."""

    expense_account_id: uuid.UUID
    reason: str
    written_off_on: date | None = None
    customer_insolvent: bool = False
    reclaim_vat: bool = False


def _write_off_json(write_off: InvoiceWriteOff) -> dict[str, object]:
    return {
        "id": str(write_off.id),
        "invoice_id": str(write_off.invoice_id),
        "amount": str(write_off.amount),
        "vat_amount": str(write_off.vat_amount),
        "vat_split": [
            {"vat_treatment": line.treatment, "vat_amount": str(line.vat)}
            for line in write_off.vat_split
        ],
        "written_off_on": write_off.written_off_on.isoformat(),
        "reason": write_off.reason,
        "customer_insolvent": write_off.customer_insolvent,
        "expense_account_id": str(write_off.expense_account_id),
        "journal_entry_id": str(write_off.journal_entry_id),
        "recorded_at": write_off.recorded_at.isoformat(),
        "vat_reclaimed_on": (
            write_off.vat_reclaimed_on.isoformat() if write_off.vat_reclaimed_on else None
        ),
        "vat_reclaim_journal_entry_id": (
            str(write_off.vat_reclaim_journal_entry_id)
            if write_off.vat_reclaim_journal_entry_id
            else None
        ),
        # A voided write-off stays in the list: it is history.
        "voided_at": write_off.voided_at.isoformat() if write_off.voided_at else None,
        "void_journal_entry_id": (
            str(write_off.void_journal_entry_id) if write_off.void_journal_entry_id else None
        ),
    }


def _write_off_problem(request: Request, exc: Exception) -> Exception | None:
    """The refusals a write-off, a reclaim or a void can raise, as problems. None for an
    exception this does not recognise, so the caller re-raises it."""
    if isinstance(exc, InvoiceNotFound):
        return problem(
            request, 404, "errors.sales_invoice_not_found", reason="sales_invoice_not_found"
        )
    if isinstance(exc, WriteOffNotAllowed):
        return problem(request, 409, "errors.write_off_not_allowed", reason="write_off_not_allowed")
    if isinstance(exc, NothingOutstanding):
        return problem(
            request,
            409,
            "errors.write_off_nothing_outstanding",
            reason="write_off_nothing_outstanding",
        )
    if isinstance(exc, WriteOffReasonMissing):
        return problem(
            request, 422, "errors.write_off_reason_missing", reason="write_off_reason_missing"
        )
    if isinstance(exc, WriteOffDateInFuture):
        return problem(
            request, 422, "errors.write_off_date_in_future", reason="write_off_date_in_future"
        )
    if isinstance(exc, ExpenseAccountInvalid):
        return problem(
            request, 422, "errors.write_off_account_invalid", reason="write_off_account_invalid"
        )
    if isinstance(exc, NoWriteOffJournal):
        return problem(request, 409, "errors.write_off_no_journal", reason="write_off_no_journal")
    if isinstance(exc, WriteOffPostingUnavailable):
        return problem(
            request,
            409,
            "errors.write_off_posting_unavailable",
            reason="write_off_posting_unavailable",
            missing=exc.missing,
        )
    if isinstance(exc, NoOpenPeriod):
        return problem(
            request, 409, "errors.write_off_no_open_period", reason="write_off_no_open_period"
        )
    if isinstance(exc, WriteOffNotFound):
        return problem(request, 404, "errors.write_off_not_found", reason="write_off_not_found")
    if isinstance(exc, WriteOffAlreadyVoided):
        return problem(
            request, 409, "errors.write_off_already_voided", reason="write_off_already_voided"
        )
    if isinstance(exc, VatAlreadyReclaimed):
        return problem(
            request,
            409,
            "errors.write_off_vat_already_reclaimed",
            reason="write_off_vat_already_reclaimed",
        )
    if isinstance(exc, NothingToReclaim):
        return problem(
            request,
            409,
            "errors.write_off_nothing_to_reclaim",
            reason="write_off_nothing_to_reclaim",
        )
    if isinstance(exc, VatReclaimNotYetAllowed):
        return problem(
            request,
            409,
            "errors.write_off_vat_not_yet",
            reason="write_off_vat_not_yet",
            eligible_on=format_date(exc.eligible_on),
            eligible_on_iso=exc.eligible_on.isoformat(),
        )
    return None


async def write_off_invoice(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    body: WriteOffBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: WriteOffService = Depends(get_write_off_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Post journal entries", as for a payment: the person who raises
            # invoices must not be the one who makes a debt disappear.
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """FR-AR-013: write the whole outstanding balance of an issued invoice off as
    uncollectable. Posts `Dr expense / Cr Debiteuren`; the VAT reclaim is a second entry,
    made now (`reclaim_vat`) if the rules allow it, or later."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    today = date.today()
    try:
        write_off = await service.write_off(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
            expense_account_id=body.expense_account_id,
            written_off_on=body.written_off_on or today,
            today=today,
            reason=body.reason,
            customer_insolvent=body.customer_insolvent,
            reclaim_vat=body.reclaim_vat,
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _write_off_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"write_off": _write_off_json(write_off), "balance": _balance_json(balance)}


async def list_write_offs(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: WriteOffService = Depends(get_write_off_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """Every write-off of one invoice - voided ones included - and what it still owes."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        write_offs = await service.write_offs(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _write_off_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {
        "write_offs": [_write_off_json(item) for item in write_offs],
        "balance": _balance_json(balance),
    }


async def reclaim_write_off_vat(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    write_off_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: WriteOffService = Depends(get_write_off_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Claim back the VAT of an earlier write-off, once the waiting period has passed (or
    the write-off recorded an insolvent customer)."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        write_off = await service.reclaim_vat(
            administration_id=administration_id,
            invoice_id=invoice_id,
            write_off_id=write_off_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except Exception as exc:
        refusal = _write_off_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"write_off": _write_off_json(write_off)}


async def void_write_off(
    administration_id: uuid.UUID,
    invoice_id: uuid.UUID,
    write_off_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: WriteOffService = Depends(get_write_off_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reverse",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Undo a write-off - and its VAT reclaim - by reversing the entries. The invoice is
    owed again."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        write_off = await service.void(
            administration_id=administration_id,
            invoice_id=invoice_id,
            write_off_id=write_off_id,
            actor_user_id=tenant.user_id,
            void_date=date.today(),
        )
        balance = await service.balance(
            administration_id=administration_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except Exception as exc:
        refusal = _write_off_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"write_off": _write_off_json(write_off), "balance": _balance_json(balance)}


# -- SI-09: SEPA direct debit (ADR-077) ------------------------------------------


async def get_sepa_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> SepaDirectDebitService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return SepaDirectDebitService(
        repository=SqlSepaRepository(session),
        # Recording that a collection arrived is an ordinary payment: same service, same
        # ledger posting, same guards.
        payments=SalesPaymentService(
            repository=SqlPaymentRepository(session),
            ledger=build_ledger_service(session, audit_log),
            authorization=authorization,
            audit_log=audit_log,
        ),
        authorization=authorization,
        audit_log=audit_log,
    )


class MandateBody(BaseModel):
    signed_on: date
    debtor_name: str
    debtor_iban: str
    debtor_bic: str | None = None
    #: Generated when omitted. Unique per administration; 1-35 SEPA characters.
    mandate_reference: str | None = None
    scheme: str = "core"
    kind: str = "recurring"


class RevokeMandateBody(BaseModel):
    reason: str | None = None


class SepaBatchBody(BaseModel):
    collection_date: date
    #: Omitted: every invoice of a customer with a usable mandate that is due by then.
    invoice_ids: list[uuid.UUID] | None = None


class SepaCollectedBody(BaseModel):
    #: The asset account the money landed in, as for any payment.
    bank_account_id: uuid.UUID


class SepaFailedBody(BaseModel):
    reason: str


def _mandate_json(mandate: Mandate, today: date) -> dict[str, object]:
    return {
        "id": str(mandate.id),
        "customer_id": str(mandate.customer_id),
        "mandate_reference": mandate.mandate_reference,
        "scheme": mandate.scheme.value,
        "kind": mandate.kind.value,
        "signed_on": mandate.signed_on.isoformat(),
        "debtor_name": mandate.debtor_name,
        "debtor_iban": mandate.debtor_iban,
        "debtor_bic": mandate.debtor_bic,
        "status": mandate.status,
        # Derived, never stored: a mandate unused for 36 months may no longer be collected on.
        "is_lapsed": mandate.is_lapsed(today),
        # What the next collection on it would be flagged as (the first / recurring flag).
        "next_sequence": (
            "OOFF" if mandate.kind is SequenceKind.ONE_OFF else ("RCUR" if mandate.used else "FRST")
        ),
        "last_collected_on": (
            mandate.last_collected_on.isoformat() if mandate.last_collected_on else None
        ),
        "revoked_at": mandate.revoked_at.isoformat() if mandate.revoked_at else None,
        "revoked_reason": mandate.revoked_reason,
        "created_at": mandate.created_at.isoformat(),
    }


def _batch_json(batch: CollectionBatch) -> dict[str, object]:
    return {
        "id": str(batch.id),
        "message_id": batch.message_id,
        "collection_date": batch.collection_date.isoformat(),
        "item_count": batch.item_count,
        "total_amount": str(batch.total_amount),
        "file_sha256": batch.file_sha256,
        "created_at": batch.created_at.isoformat(),
        "cancelled_at": batch.cancelled_at.isoformat() if batch.cancelled_at else None,
    }


def _collection_json(item: CollectionItem) -> dict[str, object]:
    return {
        "id": str(item.id),
        "batch_id": str(item.batch_id),
        "invoice_id": str(item.invoice_id),
        "mandate_id": str(item.mandate_id),
        "amount": str(item.amount),
        "sequence_type": item.sequence_type.value,
        "end_to_end_id": item.end_to_end_id,
        "status": item.status,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "failure_reason": item.failure_reason,
        "payment_id": str(item.payment_id) if item.payment_id else None,
    }


def _sepa_problem(request: Request, exc: Exception) -> Exception | None:
    """The refusals mandate and collection operations can raise, as problems. None for an
    exception this does not recognise, so the caller re-raises it."""
    if isinstance(exc, CollectionInvalid):
        return problem(
            request,
            422,
            f"errors.sepa_invalid_{exc.code}",
            reason=f"sepa_{exc.code}",
        )
    table: tuple[tuple[type[Exception], int, str], ...] = (
        (SepaCustomerNotFound, 404, "sepa_customer_not_found"),
        (MandateNotFound, 404, "sepa_mandate_not_found"),
        (MandateReferenceTaken, 409, "sepa_mandate_reference_taken"),
        (MandateAlreadyRevoked, 409, "sepa_mandate_already_revoked"),
        (TooManyInvoices, 422, "sepa_too_many_invoices"),
        (BatchNotFound, 404, "sepa_batch_not_found"),
        (BatchNotCancellable, 409, "sepa_batch_not_cancellable"),
        (BatchRaced, 409, "sepa_batch_raced"),
        (ItemNotFound, 404, "sepa_item_not_found"),
        (ItemAlreadyDecided, 409, "sepa_item_already_decided"),
    )
    for error_type, status, key in table:
        if isinstance(exc, error_type):
            return problem(request, status, f"errors.{key}", reason=key)
    if isinstance(exc, CreditorNotConfigured):
        return problem(
            request,
            409,
            "errors.sepa_creditor_not_configured",
            reason="sepa_creditor_not_configured",
            missing=exc.missing,
        )
    if isinstance(exc, CollectionNotDue):
        return problem(
            request,
            409,
            "errors.sepa_collection_not_due",
            reason="sepa_collection_not_due",
            collection_date=format_date(exc.collection_date),
            collection_date_iso=exc.collection_date.isoformat(),
        )
    if isinstance(exc, NothingToCollect):
        return problem(
            request,
            409,
            "errors.sepa_nothing_to_collect",
            reason="sepa_nothing_to_collect",
            skipped=[
                {"invoice_id": str(item.invoice_id), "reason": item.reason.value}
                for item in exc.skipped
            ],
        )
    # Recording a collection records a payment, which has refusals of its own.
    return _payment_problem(request, exc)


async def create_sepa_mandate(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    body: MandateBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-011: register a mandate the customer signed. It is evidence of consent and is
    never edited; a customer who changes bank signs a new one."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        scheme = MandateScheme(body.scheme)
        kind = SequenceKind(body.kind)
    except ValueError:
        raise problem(
            request, 422, "errors.sepa_invalid_scheme_or_kind", reason="sepa_scheme_or_kind_invalid"
        ) from None
    today = date.today()
    try:
        mandate = await service.create_mandate(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            customer_id=customer_id,
            signed_on=body.signed_on,
            today=today,
            debtor_name=body.debtor_name,
            debtor_iban=body.debtor_iban,
            debtor_bic=body.debtor_bic,
            mandate_reference=body.mandate_reference,
            scheme=scheme,
            kind=kind,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"mandate": _mandate_json(mandate, today)}


async def list_sepa_mandates(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create", "sales_invoice", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    """A customer's mandates, revoked ones included, newest signature first."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    today = date.today()
    try:
        mandates = await service.mandates(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            customer_id=customer_id,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"mandates": [_mandate_json(m, today) for m in mandates]}


async def revoke_sepa_mandate(
    administration_id: uuid.UUID,
    mandate_id: uuid.UUID,
    request: Request,
    body: RevokeMandateBody | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "create",
            "sales_invoice",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Withdraw a mandate. It is never collected on again, and stays as history."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    today = date.today()
    try:
        mandate = await service.revoke_mandate(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            mandate_id=mandate_id,
            reason=body.reason if body else None,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"mandate": _mandate_json(mandate, today)}


async def create_sepa_batch(
    administration_id: uuid.UUID,
    body: SepaBatchBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Generate a pain.008 file and reserve its invoices. Invoices that cannot be collected
    are reported in `skipped`; if none can, 409 says why."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        result = await service.create_batch(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            collection_date=body.collection_date,
            today=date.today(),
            now=datetime.now().astimezone(),
            invoice_ids=body.invoice_ids,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {
        "batch": _batch_json(result.batch),
        "items": [_collection_json(item) for item in result.items],
        "skipped": [
            {"invoice_id": str(item.invoice_id), "reason": item.reason.value}
            for item in result.skipped
        ],
    }


async def list_sepa_batches(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post", "journal_entry", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    batches = await service.batches(
        administration_id=administration_id, actor_user_id=tenant.user_id
    )
    return {"batches": [_batch_json(batch) for batch in batches]}


async def get_sepa_batch(
    administration_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post", "journal_entry", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        batch, items = await service.batch(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            batch_id=batch_id,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"batch": _batch_json(batch), "items": [_collection_json(item) for item in items]}


async def download_sepa_file(
    administration_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post", "journal_entry", scope=administration_from_path("administration_id")
        )
    ),
) -> Response:
    """The pain.008 file exactly as generated - the bytes the bank is given, whose SHA-256 the
    batch records."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        batch, xml = await service.file(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            batch_id=batch_id,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return Response(
        content=xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{batch.message_id}.xml"'},
    )


async def cancel_sepa_batch(
    administration_id: uuid.UUID,
    batch_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Withdraw a file nobody uploaded: its invoices are free to be collected again."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        batch = await service.cancel_batch(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            batch_id=batch_id,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"batch": _batch_json(batch)}


async def record_sepa_collected(
    administration_id: uuid.UUID,
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SepaCollectedBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """The bank confirms this collection arrived: records the payment (posting Dr bank / Cr
    Debiteuren) and settles the item in one step."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        item = await service.record_collected(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            batch_id=batch_id,
            item_id=item_id,
            bank_account_id=body.bank_account_id,
            today=date.today(),
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"item": _collection_json(item)}


async def record_sepa_failed(
    administration_id: uuid.UUID,
    batch_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SepaFailedBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: SepaDirectDebitService = Depends(get_sepa_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """The bank returned or rejected this collection. The invoice stays owed."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        item = await service.record_failed(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            batch_id=batch_id,
            item_id=item_id,
            reason=body.reason,
        )
    except Exception as exc:
        refusal = _sepa_problem(request, exc)
        if refusal is None:
            raise
        raise refusal from exc
    return {"item": _collection_json(item)}

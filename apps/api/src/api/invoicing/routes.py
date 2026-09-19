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
from datetime import date
from decimal import Decimal

from fastapi import Depends, FastAPI, Query, Request
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
from api.i18n.formatting import format_money
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
from api.invoicing.rendering import build_invoice_renderer
from api.invoicing.repository import SqlInvoiceRepository
from api.invoicing.service import InvoicingService, NewLine
from api.invoicing.statutory import describe
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

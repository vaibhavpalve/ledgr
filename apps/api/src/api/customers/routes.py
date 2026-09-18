"""The customer master's HTTP surface - FR-AR-006, FR-ONB-003.

Registered via `register(app)` rather than `include_router`, for the reason
`api.documents.routes.register` explains at length: this FastAPI version hides
an included router's routes behind a wrapper the authorization middleware and
all four coverage checks walk straight past.

--- The credit limit crosses the wire as a STRING ---

JSON has one number type and it is a double, so an unquoted `50000.00` has
already lost the value before pydantic sees it. NFR-031 covers the whole
calculation path, and a credit limit is on it. The same rule
`api.invoicing.routes` applies to quantities and prices.

--- One permission on every route, reads included ---

`manage customer`, from `api.authz.matrix.MANAGE_CUSTOMER`. There is no
`view customer` and none is invented here (ADR-012). The consequence - a Viewer
cannot list customers - is real and is recorded in ADR-038 rather than papered
over with a permission the PRD does not grant.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.config import settings
from api.customers.kvk import KvkLookupResult, KvkLookupService, build_kvk_lookup
from api.customers.model import (
    Customer,
    CustomerIsArchived,
    CustomerIsErased,
    CustomerNotFound,
    DeliveryChannel,
    InvalidCustomerField,
)
from api.customers.peppol import DiscoveryResult, build_peppol_directory
from api.customers.repository import SqlCustomerRepository
from api.customers.service import CustomerDetails, CustomerService
from api.customers.vies import build_vies_validator
from api.db import get_db_session
from api.i18n.http import problem
from api.i18n.language import Language, parse_language
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}/customers"


def register(app: FastAPI) -> None:
    app.add_api_route(_BASE, create_customer, methods=["POST"], name="create_customer")
    app.add_api_route(_BASE, list_customers, methods=["GET"], name="list_customers")
    app.add_api_route(
        f"{_BASE}/{{customer_id}}", get_customer, methods=["GET"], name="get_customer"
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}", update_customer, methods=["PUT"], name="update_customer"
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}/archive",
        archive_customer,
        methods=["POST"],
        name="archive_customer",
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}/restore",
        restore_customer,
        methods=["POST"],
        name="restore_customer",
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}/erasure-request",
        request_customer_erasure,
        methods=["POST"],
        name="request_customer_erasure",
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}/vat-number/validate",
        validate_customer_vat_number,
        methods=["POST"],
        name="validate_customer_vat_number",
    )
    app.add_api_route(
        f"{_BASE}/{{customer_id}}/peppol/discover",
        discover_peppol_participant,
        methods=["POST"],
        name="discover_peppol_participant",
    )
    # SI-03. Bare `_BASE/kvk-lookup`, not `_BASE/{customer_id}/...` - there is
    # no customer yet at this point, only a "new customer" form being filled
    # in. Deliberately does not collide with GET/PUT _BASE/{customer_id}: it
    # is POST-only, and neither of those is.
    app.add_api_route(
        f"{_BASE}/kvk-lookup", lookup_kvk_number, methods=["POST"], name="lookup_kvk_number"
    )


async def get_customer_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> CustomerService:
    return CustomerService(
        repository=SqlCustomerRepository(session),
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
        # Built per request from configuration, the same way
        # api.documents.routes builds its scanner: which provider is in use is
        # a deployment fact, and a module-level singleton would make it one
        # chosen at import.
        vies=build_vies_validator(settings.vies_provider),
        peppol=build_peppol_directory(settings.peppol_directory_provider),
    )


class CustomerBody(BaseModel):
    """One body for create and update, because they accept the same fields.

    `credit_limit` is a string on the wire and Decimal here - see the module
    docstring. It is parsed by hand rather than typed as `Decimal` so that a
    JSON number (which has already lost precision) can be refused with a
    sentence naming the field, instead of being silently accepted by
    pydantic's float coercion.
    """

    name: str
    trade_name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str = "NL"
    kvk_number: str | None = None
    vat_number: str | None = None
    peppol_participant_id: str | None = None
    payment_terms_days: int = 30
    credit_limit: str | None = None
    delivery_channel: str = "email"
    invoice_email: str | None = None
    language: str = "nl"
    notes: str | None = None


def _details(body: CustomerBody, request: Request) -> CustomerDetails:
    """Body to domain, with the two enum fields resolved.

    An unknown delivery channel or language is refused HERE rather than passed
    down as a string: the service takes the enums, so an invalid value cannot
    reach it, and the caller gets a message naming the field with the accepted
    values beside it as machine-readable context.
    """
    try:
        channel = DeliveryChannel(body.delivery_channel)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.customer_field_invalid",
            reason="customer_field_invalid",
            field="delivery_channel",
            accepted=[member.value for member in DeliveryChannel],
        ) from exc

    language = parse_language(body.language)
    if language is None:
        raise problem(
            request,
            422,
            "errors.customer_field_invalid",
            reason="customer_field_invalid",
            field="language",
            accepted=[member.value for member in Language],
        )

    credit_limit: Decimal | None = None
    if body.credit_limit is not None and body.credit_limit.strip():
        try:
            credit_limit = Decimal(body.credit_limit.strip())
        except InvalidOperation as exc:
            raise problem(
                request,
                422,
                "errors.customer_field_invalid",
                reason="customer_field_invalid",
                field="credit_limit",
            ) from exc

    return CustomerDetails(
        name=body.name,
        trade_name=body.trade_name,
        address_line1=body.address_line1,
        address_line2=body.address_line2,
        postal_code=body.postal_code,
        city=body.city,
        country=body.country,
        kvk_number=body.kvk_number,
        vat_number=body.vat_number,
        peppol_participant_id=body.peppol_participant_id,
        payment_terms_days=body.payment_terms_days,
        credit_limit=credit_limit,
        delivery_channel=channel,
        invoice_email=body.invoice_email,
        language=language,
        notes=body.notes,
    )


def _customer_json(customer: Customer) -> dict[str, object]:
    """One shape, produced once, so web and mobile cannot disagree about what a
    customer is - the same reason `api.main._entry_json` exists.
    """
    return {
        "id": str(customer.id),
        "name": customer.name,
        "trade_name": customer.trade_name,
        "address_line1": customer.address.address_line1,
        "address_line2": customer.address.address_line2,
        "postal_code": customer.address.postal_code,
        "city": customer.address.city,
        "country": customer.address.country,
        # FR-AR-003: whether this customer could go on an invoice at all.
        # Returned rather than left for the client to infer from four nullable
        # fields, because the rule is "street, postcode and city", not "any of
        # these is filled in".
        "address_is_complete": customer.address.is_complete,
        "kvk_number": customer.kvk_number,
        # FR-AR-006 / FR-ONB-003. The number and the VERDICT travel together:
        # a client showing one without the other would present an unverified
        # number as a verified one.
        "vat_number": customer.vat_number,
        "vat_number_status": customer.vat_number_status.value,
        "vat_number_checked_at": (
            customer.vat_number_checked_at.isoformat() if customer.vat_number_checked_at else None
        ),
        "vat_number_checked_name": customer.vat_number_checked_name,
        "vat_number_consultation_number": customer.vat_number_consultation_number,
        # P2. Present but never discovered yet - see api.customers.peppol.
        "peppol_participant_id": customer.peppol_participant_id,
        "peppol_checked_at": (
            customer.peppol_checked_at.isoformat() if customer.peppol_checked_at else None
        ),
        "is_deliverable_over_peppol": customer.is_deliverable_over_peppol,
        "payment_terms_days": customer.payment_terms_days,
        # NFR-031: a string on the wire, both ways. Null is "no limit set",
        # which is not "0".
        "credit_limit": str(customer.credit_limit) if customer.credit_limit is not None else None,
        "delivery_channel": customer.delivery_channel.value,
        "invoice_email": customer.invoice_email,
        "language": customer.language.value,
        "notes": customer.notes,
        "archived_at": customer.archived_at.isoformat() if customer.archived_at else None,
        "erased_at": customer.erased_at.isoformat() if customer.erased_at else None,
    }


def _refuse(request: Request, exc: Exception) -> Exception:
    """The refusals every write route shares, mapped once.

    Written as a helper rather than repeated in eight `except` ladders, because
    a status code that differed between two routes for the same domain error
    would be a bug nobody could see from either route.
    """
    if isinstance(exc, CustomerNotFound):
        return problem(request, 404, "errors.customer_not_found", reason="customer_not_found")
    if isinstance(exc, CustomerIsArchived):
        return problem(request, 409, "errors.customer_archived", reason="customer_archived")
    if isinstance(exc, CustomerIsErased):
        return problem(request, 409, "errors.customer_erased", reason="customer_erased")
    if isinstance(exc, InvalidCustomerField):
        return problem(
            request,
            422,
            "errors.customer_field_invalid",
            reason="customer_field_invalid",
            field=exc.field,
        )
    return exc


async def create_customer(
    administration_id: uuid.UUID,
    body: CustomerBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-006. The VAT number is checked against VIES if there is one, and
    a VIES outage does not fail the save (NFR-026) - the verdict is a field.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.create(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            details=_details(body, request),
        )
    except (CustomerNotFound, InvalidCustomerField) as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


async def list_customers(
    administration_id: uuid.UUID,
    request: Request,
    q: str | None = None,
    include_archived: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> list[dict[str, object]]:
    """FR-FRM-000's search shape, applied to customers: exact, then prefix,
    then substring, so type-then-Enter lands on what was meant.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    customers = await service.search(
        administration_id=administration_id,
        actor_user_id=tenant.user_id,
        query=q,
        include_archived=include_archived,
        limit=limit,
    )
    return [_customer_json(customer) for customer in customers]


async def get_customer(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.get(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


async def update_customer(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    body: CustomerBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """PUT, because a customer is edited as one form and sent back whole.

    Editing a customer changes NOTHING about invoices already issued to them -
    those carry a frozen snapshot (migration 0037/0039). Correcting a document
    already sent is a credit note (FR-AR-001).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.update(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
            details=_details(body, request),
        )
    except (CustomerNotFound, CustomerIsArchived, InvalidCustomerField) as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


async def archive_customer(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """There is no DELETE route, and migration 0039 grants no DELETE either.

    A customer row is pointed at by statutory documents CMP-001 keeps for seven
    years. Archiving takes them out of pickers and changes nothing about the
    documents.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.set_archived(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
            archived=True,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


async def restore_customer(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Its own route rather than a flag on the update body, so that resuming
    trade with a customer is a deliberate act somebody performed - and reads as
    one in the audit log (IAM-090) - rather than a side effect of saving a
    form.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.set_archived(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
            archived=False,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


async def request_customer_erasure(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """PRIV-022/PRIV-023.

    Always 200: the master record is anonymised unconditionally, whatever the
    outcome. `outcome` in the body is `erased` (nothing of this customer's
    remains under statutory retention) or `restricted` (some invoices still
    are), and `explanation` names them - PRIV-022's "clear, specific
    explanation of what was retained and why".
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        decision = await service.request_erasure(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return decision.as_dict()


async def validate_customer_vat_number(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-ONB-003: consult VIES for this customer's number, on demand.

    Always 200, whatever VIES said - including when it said nothing. The
    verdict is in `vat_number_status`, and a 5xx for an upstream outage would
    make an unavailable register look like a broken API and invite a retry
    storm against a rate-limited public service (NFR-026).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer = await service.validate_vat_number(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return _customer_json(customer)


def _discovery_json(result: DiscoveryResult) -> dict[str, object]:
    return {
        # `not_configured` is NOT `not_registered`, and a client must not
        # collapse them - the first means nobody looked. See
        # api.customers.peppol.
        "status": result.status.value,
        "is_deliverable": result.status.is_deliverable,
        "participant_id": (
            result.participant_id.value if result.participant_id is not None else None
        ),
        "tried": [candidate.value for candidate in result.tried],
    }


async def get_kvk_lookup_service() -> KvkLookupService:
    """Built per request from configuration, the same reason
    `get_customer_service` builds its VIES validator and Peppol directory
    the same way rather than as a module-level singleton.
    """
    return build_kvk_lookup(settings.kvk_provider, api_key=settings.kvk_api_key)


class KvkLookupBody(BaseModel):
    kvk_number: str


def _kvk_lookup_json(result: KvkLookupResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "kvk_number": result.kvk_number,
        "legal_name": result.legal_name,
        "trade_name": result.trade_name,
        "address_line1": result.address_line1,
        "postal_code": result.postal_code,
        "city": result.city,
    }


async def lookup_kvk_number(
    administration_id: uuid.UUID,
    body: KvkLookupBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    lookup: KvkLookupService = Depends(get_kvk_lookup_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """SI-03: auto-fill a NEW customer's name and address from the KvK
    register, before anything is saved - nothing here is persisted (see
    api.customers.kvk's module docstring).

    Always 200, whatever the register said - including nothing. The same
    reasoning `validate_customer_vat_number` documents: an unavailable
    register is a verdict (NFR-026), and a 5xx would make a rate-limited
    public register look like a broken API and invite a retry storm.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    result = await lookup.lookup(body.kvk_number)
    return _kvk_lookup_json(result)


async def discover_peppol_participant(
    administration_id: uuid.UUID,
    customer_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CustomerService = Depends(get_customer_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "customer",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-AR-006's "Peppol participant ID discovery" - a STUB until P2.

    Returns 200 with `status: "not_configured"`. Not a 501: the route works,
    the answer is simply that no directory is configured, and that answer is
    itself information a client needs in order to fall back to email without
    concluding the customer is absent from the network. See
    api.customers.peppol for why those two must never be conflated.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        customer, result = await service.discover_peppol_participant(
            administration_id=administration_id,
            customer_id=customer_id,
            actor_user_id=tenant.user_id,
        )
    except CustomerNotFound as exc:
        raise _refuse(request, exc) from exc
    return {"customer": _customer_json(customer), "discovery": _discovery_json(result)}

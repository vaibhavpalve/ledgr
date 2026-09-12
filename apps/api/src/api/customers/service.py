"""The customer master - FR-AR-006, FR-ONB-003.

--- One permission, and it is not invented ---

    manage customer

`api.authz.matrix.MANAGE_CUSTOMER` already exists. Appendix A has no row for
customers, so the capability is declared in EXTENSION_CAPABILITIES with its
reasoning attached - IAM-101's "Invoice and capture" profile and §8.4's Invoicer
("create and send sales invoices, manage customers") both name it. ADR-012's
rule against invented permissions is therefore satisfied without adding
anything.

READS use the same permission, and that is a real consequence worth stating
rather than hiding: there is no `view customer`, so a Viewer cannot list
customers. Inventing one would be exactly the drift ADR-012 forbids, and the
honest fix is a PRD change to Appendix A, not a permission added here. See
ADR-038.

--- What the service refuses, and what it merely records ---

Two categories, and they behave differently on purpose:

    REFUSED   a value the database would reject anyway (a malformed KvK
              number, a negative credit limit, an email channel with no
              address). Caught here so the caller gets the FIELD name rather
              than a constraint name.
    RECORDED  a VIES verdict. Never a refusal. NFR-026: the customer saves
              whether or not the Commission's service answered, and the
              verdict is a column - see api.customers.vies.

The second is the one that would be easy to get wrong. A save that failed
because VIES was down would make an unrelated outage look like the user's
mistake, and the user's only recourse would be to clear a field that was
correct.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.customers.address import PostalAddress, format_address
from api.customers.model import (
    Customer,
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerIsErased,
    CustomerNotFound,
    DeliveryChannel,
    InvalidCustomerField,
    NotAuthorizedToManageCustomers,
    RetainedInvoiceRef,
)
from api.customers.peppol import (
    BIS_BILLING_INVOICE,
    DiscoveryResult,
    PeppolDirectory,
    candidate_identifiers,
)
from api.customers.vat_number import normalise
from api.customers.vat_number import parse as parse_vat_number
from api.customers.vies import ViesResult, ViesStatus, ViesValidator
from api.i18n.language import Language
from api.privacy.model import ErasureDecision, ErasureOutcome

#: The Appendix A extension row, reused rather than invented (ADR-012).
MANAGE_CUSTOMER = ("manage", "customer")

#: FR-AR-006's KvK number: eight digits, mirroring 0039's CHECK. Restated here
#: so a person gets a sentence naming the field instead of a constraint
#: violation; the database remains the one that cannot be bypassed.
_KVK_DIGITS = 8


@dataclass(frozen=True, slots=True)
class CustomerDetails:
    """Everything a caller may set. One structure for create and update, so the
    two cannot drift into accepting different fields.

    Not `Customer`: that carries the VIES verdict, the timestamps and the id,
    none of which a caller supplies. A single type used for both would make
    `vat_number_status` look settable, and a caller that set it would be
    asserting a consultation that never happened.
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
    credit_limit: Decimal | None = None
    delivery_channel: DeliveryChannel = DeliveryChannel.EMAIL
    invoice_email: str | None = None
    language: Language = Language.NL
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceCustomerSnapshot:
    """What migration 0037's snapshot columns are filled from.

    Produced here rather than in `api.invoicing`, because the rule that a
    customer master POPULATES an invoice and never joins to it (0039's header)
    is a rule about this package's data, and the invoicing service should not
    have to know how an address is assembled.
    """

    customer_id: uuid.UUID
    name: str
    address: str
    country: str
    vat_number: str | None
    language: Language
    due_date: date


class CustomerRepository(Protocol):
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        details: CustomerDetails,
        user_id: uuid.UUID,
    ) -> Customer: ...

    async def get(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Customer | None: ...

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        details: CustomerDetails,
    ) -> Customer: ...

    async def search(
        self,
        *,
        administration_id: uuid.UUID,
        query: str | None,
        include_archived: bool,
        limit: int,
    ) -> Sequence[Customer]: ...

    async def set_archived(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, archived: bool
    ) -> Customer: ...

    async def record_vies_result(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        result: ViesResult,
    ) -> Customer: ...

    async def record_peppol_id(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        participant_id: str | None,
    ) -> Customer: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def erase(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> Customer: ...

    async def retained_invoices(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, today: date
    ) -> Sequence[RetainedInvoiceRef]: ...

    async def record_erasure_request(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        resource_id: uuid.UUID,
        requested_by_user_id: uuid.UUID,
        decision: str,
        explanation: str,
        retained_until: date | None,
    ) -> None: ...


class CustomerService:
    def __init__(
        self,
        repository: CustomerRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
        vies: ViesValidator,
        peppol: PeppolDirectory,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log
        self._vies = vies
        self._peppol = peppol

    # -- FR-AR-006: create and edit -----------------------------------------

    async def create(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        details: CustomerDetails,
        validate_vat_number: bool = True,
        correlation_id: str | None = None,
    ) -> Customer:
        """A new customer, with the VAT number checked if there is one.

        `validate_vat_number` exists for a bulk import, which would otherwise
        make one VIES call per row against a free public service and be
        throttled into failure. It defaults to True so that the safe behaviour
        is what a caller gets by not thinking about it.
        """
        await self._require(actor_user_id, administration_id)
        organization_id = await self._organization_of(administration_id)

        details = self._validated(details)
        customer = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            details=details,
            user_id=actor_user_id,
        )

        if validate_vat_number and details.vat_number:
            customer = await self._check_vat_number(
                administration_id=administration_id,
                customer=customer,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_customer",
            resource_id=customer.id,
            correlation_id=correlation_id,
            detail={
                "name": customer.name,
                # The VERDICT, not the number. A VAT number is a tax
                # identifier and the audit log is exportable (IAM-094); the id
                # beside it is enough to find the row.
                "vat_number_status": customer.vat_number_status.value,
                "delivery_channel": customer.delivery_channel.value,
            },
        )
        return customer

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        details: CustomerDetails,
        correlation_id: str | None = None,
    ) -> Customer:
        """Edit the record. Documents already raised are untouched.

        That is worth saying explicitly because it is the opposite of what a
        person editing an address might expect: correcting a customer's address
        does NOT correct the address on invoices already issued to them, and it
        must not - see 0039's header. An issued invoice is corrected with a
        credit note (FR-AR-001).
        """
        await self._require(actor_user_id, administration_id)
        existing = await self._active_or_refuse(administration_id, customer_id)

        details = self._validated(details)
        customer = await self._repository.update(
            administration_id=administration_id,
            customer_id=customer_id,
            details=details,
        )

        # Re-checked only when the number actually changed. A VIES call on
        # every save of an unrelated field would be an outbound request per
        # keystroke-batch against a rate-limited public service, and it would
        # overwrite a good verdict with UNAVAILABLE the first time it failed.
        if normalise(details.vat_number or "") != (existing.vat_number or ""):
            customer = await self._check_vat_number(
                administration_id=administration_id,
                customer=customer,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
            )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="update_customer",
            resource_id=customer.id,
            correlation_id=correlation_id,
            detail={
                "name": customer.name,
                "vat_number_status": customer.vat_number_status.value,
                "delivery_channel": customer.delivery_channel.value,
            },
        )
        return customer

    async def set_archived(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        archived: bool,
        correlation_id: str | None = None,
    ) -> Customer:
        """Archive or restore. There is no delete - 0039 grants none.

        A customer row is pointed at by statutory documents CMP-001 keeps for
        seven years, so "who was this invoice made out to" has to stay
        answerable for all of them.
        """
        await self._require(actor_user_id, administration_id)
        await self._get_or_refuse(administration_id, customer_id)

        customer = await self._repository.set_archived(
            administration_id=administration_id,
            customer_id=customer_id,
            archived=archived,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="archive_customer" if archived else "restore_customer",
            resource_id=customer_id,
            correlation_id=correlation_id,
            detail={"name": customer.name},
        )
        return customer

    # -- PRIV-022/PRIV-023 ----------------------------------------------------

    async def request_erasure(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
        today: date | None = None,
    ) -> ErasureDecision:
        """PRIV-022/PRIV-023.

        The customer MASTER record carries nothing CMP-001 requires kept -
        0039's header is explicit that an invoice snapshots what it needs and
        never reads back through this row - so it is anonymised outright,
        every time, via `_repository.erase`. What that cannot touch is an
        ISSUED invoice's own frozen snapshot (0037/0039's freeze trigger
        forbids changing it, and CMP-001 requires it for seven years from the
        end of its fiscal year): while any of this customer's invoices are
        still inside that window, the request is only PARTIALLY honoured, and
        `explanation` names which ones and until when - PRIV-022's "clear,
        specific explanation of what was retained and why".
        """
        await self._require(actor_user_id, administration_id)
        await self._get_or_refuse(administration_id, customer_id)

        as_of = today if today is not None else date.today()
        decided_at = datetime.now(UTC)

        retained = await self._repository.retained_invoices(
            administration_id=administration_id, customer_id=customer_id, today=as_of
        )
        # Anonymised regardless of what `retained` holds: the master record
        # is never itself the fiscally-protected artifact, so there is no
        # "restricted" state for it to be in.
        await self._repository.erase(administration_id=administration_id, customer_id=customer_id)

        if retained:
            outcome = ErasureOutcome.RESTRICTED
            retained_until = max(ref.retained_until for ref in retained)
            named = "; ".join(
                f"invoice {ref.invoice_reference} of {ref.invoice_date.isoformat()}, "
                f"retained until {ref.retained_until.isoformat()}"
                for ref in retained
            )
            explanation = (
                "the customer record itself has been anonymised. "
                f"{len(retained)} issued invoice(s) remain under Dutch fiscal "
                "retention (CMP-001) and their content - including this customer's "
                f"name and address as they appeared on the document - cannot be "
                f"altered before retention ends: {named}. Those invoices are "
                "excluded from ordinary customer search; when a product-analytics "
                "pipeline exists over invoice data it must apply the same "
                "exclusion. Each is deleted automatically once its own retention "
                "period ends."
            )
        else:
            outcome = ErasureOutcome.ERASED
            retained_until = None
            explanation = (
                "no invoice for this customer remains under statutory retention; "
                "the erasure request has been fully honoured."
            )

        organization_id = await self._organization_of(administration_id)
        await self._repository.record_erasure_request(
            organization_id=organization_id,
            administration_id=administration_id,
            resource_id=customer_id,
            requested_by_user_id=actor_user_id,
            decision=outcome.value,
            explanation=explanation,
            retained_until=retained_until,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="request_customer_erasure",
            resource_id=customer_id,
            correlation_id=correlation_id,
            detail={"outcome": outcome.value, "retained_invoices": len(retained)},
        )

        return ErasureDecision(
            resource_type="customer",
            resource_id=customer_id,
            outcome=outcome,
            explanation=explanation,
            retained_until=retained_until,
            decided_at=decided_at,
        )

    # -- FR-ONB-003: VIES ----------------------------------------------------

    async def validate_vat_number(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Customer:
        """Re-consult VIES for this customer, on demand.

        Its own endpoint because an UNAVAILABLE verdict has to be retryable
        without editing the customer - the alternative is a person clearing and
        retyping a correct VAT number to trigger a re-check, which teaches
        exactly the wrong habit.
        """
        await self._require(actor_user_id, administration_id)
        customer = await self._get_or_refuse(administration_id, customer_id)

        return await self._check_vat_number(
            administration_id=administration_id,
            customer=customer,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

    async def _check_vat_number(
        self,
        *,
        administration_id: uuid.UUID,
        customer: Customer,
        actor_user_id: uuid.UUID,
        correlation_id: str | None,
    ) -> Customer:
        result = await self._vies.validate(customer.vat_number)
        updated = await self._repository.record_vies_result(
            administration_id=administration_id,
            customer_id=customer.id,
            result=result,
        )

        # IAM-090: recorded whatever the answer. An INVALID verdict on a
        # customer being zero-rated for an intra-Community supply is exactly
        # the sequence an auditor reconstructs afterwards, and an UNAVAILABLE
        # one explains why a number was never confirmed.
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="validate_customer_vat_number",
            resource_id=customer.id,
            correlation_id=correlation_id,
            outcome=(
                AuditOutcome.SUCCESS
                if result.status is not ViesStatus.UNAVAILABLE
                else AuditOutcome.FAILURE
            ),
            detail={
                "vat_number_status": result.status.value,
                "consultation_number": result.consultation_number,
                # Operator-facing; never rendered to a user (FR-UX-007).
                "detail": result.detail,
            },
        )
        return updated

    # -- FR-AR-006: Peppol participant discovery (P2 stub) -------------------

    async def discover_peppol_participant(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> tuple[Customer, DiscoveryResult]:
        """FR-AR-006's "Peppol participant ID discovery". A STUB today.

        Returns the customer unchanged and a result carrying
        `DiscoveryStatus.NOT_CONFIGURED` - see `api.customers.peppol` for why
        that is emphatically not `NOT_REGISTERED`. The endpoint exists now so
        that FR-AR-005's P2 work is a directory implementation rather than a
        new route, a new permission and a new audit action.

        The row is only written when a lookup actually SUCCEEDS, so the stub
        cannot leave a fabricated identifier behind.
        """
        await self._require(actor_user_id, administration_id)
        customer = await self._get_or_refuse(administration_id, customer_id)

        candidates = candidate_identifiers(
            kvk_number=customer.kvk_number, vat_number=customer.vat_number
        )
        result = await self._peppol.discover(candidates, document_type=BIS_BILLING_INVOICE)

        if result.status.is_deliverable and result.participant_id is not None:
            customer = await self._repository.record_peppol_id(
                administration_id=administration_id,
                customer_id=customer_id,
                participant_id=result.participant_id.value,
            )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="discover_peppol_participant",
            resource_id=customer_id,
            correlation_id=correlation_id,
            outcome=(
                AuditOutcome.SUCCESS if result.status.is_deliverable else AuditOutcome.FAILURE
            ),
            detail={
                "status": result.status.value,
                "tried": [candidate.value for candidate in result.tried],
            },
        )
        return customer, result

    # -- reading -------------------------------------------------------------

    async def get(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> Customer:
        await self._require(actor_user_id, administration_id)
        return await self._get_or_refuse(administration_id, customer_id)

    async def search(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        query: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> Sequence[Customer]:
        """FR-FRM-000's shape, applied to customers: type a few letters of the
        name, trade name or KvK number and get the one you meant first.

        Archived customers are excluded by default. They are still readable by
        id - every invoice ever raised for them points at the row - but a
        picker offering counterparties the business has stopped trading with is
        how an invoice goes to the wrong company.
        """
        await self._require(actor_user_id, administration_id)
        return await self._repository.search(
            administration_id=administration_id,
            query=query,
            include_archived=include_archived,
            # Bounded here as well as in the route: a service called from a
            # script has no query string to be clamped.
            limit=max(1, min(limit, 200)),
        )

    # -- populating an invoice (FR-AR-006 -> FR-AR-001) -----------------------

    async def snapshot_for_invoice(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        invoice_date: date,
    ) -> InvoiceCustomerSnapshot:
        """What migration 0037's customer columns get filled with.

        Read once, at draft creation, and then frozen on the document. The
        invoice never reads back through here - see 0039's header.

        Refuses an ARCHIVED customer: "we have stopped trading with them" and
        "raise them an invoice" are contradictory instructions, and silently
        honouring the second is how a dormant record comes back to life.
        """
        customer = await self._active_or_refuse(administration_id, customer_id)

        if not customer.address.is_complete:
            raise CustomerAddressIncomplete(customer.address.missing_statutory_parts)

        return InvoiceCustomerSnapshot(
            customer_id=customer.id,
            name=customer.name,
            address=format_address(customer.address),
            country=customer.address.country,
            vat_number=customer.vat_number,
            # FR-TPL-013: the legal wording goes on the document in the
            # RECIPIENT's language, which is this and not the caller's.
            language=customer.language,
            # FR-AR-006's payment terms, turned into the date the invoice
            # actually carries.
            due_date=customer.due_date_for(invoice_date),
        )

    # -- validation ----------------------------------------------------------

    def _validated(self, details: CustomerDetails) -> CustomerDetails:
        """Refuses what 0039 would refuse, but by field name.

        Every rule here is also a database constraint. This is the second line,
        not the only line - CLAUDE.md's first rule states the principle for
        tenancy and it holds for validation generally: the layer that cannot be
        bypassed is the one that has to be right, and this one exists so a
        person gets a sentence about `kvk_number` instead of
        `customer_kvk_number_check`.
        """
        name = details.name.strip()
        if not name:
            raise InvalidCustomerField("name", "a customer needs a name")

        kvk = (details.kvk_number or "").strip() or None
        if kvk is not None and (len(kvk) != _KVK_DIGITS or not kvk.isdigit()):
            raise InvalidCustomerField("kvk_number", f"a KvK number is {_KVK_DIGITS} digits")

        # Normalised, never rejected on shape: a VAT number that does not parse
        # is recorded with a SYNTAX_INVALID verdict rather than refused, because
        # a half-typed number in a field somebody is still filling in is not an
        # error worth losing the rest of the form over. `parse` decides the
        # verdict; this only decides the spelling stored.
        vat = normalise(details.vat_number or "") or None

        country = details.country.strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise InvalidCustomerField(
                "country", "a country is an ISO 3166-1 alpha-2 code, such as NL"
            )

        if not 0 <= details.payment_terms_days <= 365:
            raise InvalidCustomerField(
                "payment_terms_days", "payment terms are between 0 and 365 days"
            )

        if details.credit_limit is not None and details.credit_limit < 0:
            raise InvalidCustomerField("credit_limit", "a credit limit cannot be negative")

        email = (details.invoice_email or "").strip() or None
        if details.delivery_channel is DeliveryChannel.EMAIL and email is None:
            raise InvalidCustomerField(
                "invoice_email",
                "a customer invoiced by email needs an email address",
            )

        return CustomerDetails(
            name=name,
            trade_name=(details.trade_name or "").strip() or None,
            address_line1=(details.address_line1 or "").strip() or None,
            address_line2=(details.address_line2 or "").strip() or None,
            postal_code=(details.postal_code or "").strip() or None,
            city=(details.city or "").strip() or None,
            country=country,
            kvk_number=kvk,
            vat_number=vat,
            peppol_participant_id=(details.peppol_participant_id or "").strip() or None,
            payment_terms_days=details.payment_terms_days,
            credit_limit=details.credit_limit,
            delivery_channel=details.delivery_channel,
            invoice_email=email,
            language=details.language,
            notes=(details.notes or "").strip() or None,
        )

    # -- internals -----------------------------------------------------------

    async def _get_or_refuse(
        self, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Customer:
        customer = await self._repository.get(
            administration_id=administration_id, customer_id=customer_id
        )
        if customer is None:
            raise CustomerNotFound(f"customer {customer_id} not found")
        return customer

    async def _active_or_refuse(
        self, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> Customer:
        customer = await self._get_or_refuse(administration_id, customer_id)
        # Checked before archived, and unconditionally: there is no restore
        # path for an erasure, so this must never be reached by re-archiving.
        if customer.is_erased:
            raise CustomerIsErased(
                f"customer {customer_id} was erased (PRIV-022); it cannot be edited "
                "or invoiced"
            )
        if customer.is_archived:
            raise CustomerIsArchived(
                f"customer {customer_id} is archived; restore it before using it again"
            )
        return customer

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise CustomerNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = MANAGE_CUSTOMER
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToManageCustomers(
                action, resource_type, decision.detail or decision.reason
            )

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                # IAM-090. A customer record decides who a statutory document
                # is addressed to and on what terms; an auditor reading an
                # invoice backwards arrives here.
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="customer",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def vat_number_is_parseable(value: str | None) -> bool:
    """Whether `value` could be an EU VAT number at all.

    Exported for the routes layer, which wants to tell a caller that a number
    will be recorded as SYNTAX_INVALID before the round trip - without
    importing the parser and duplicating the judgment.
    """
    return parse_vat_number(value) is not None


def postal_address_of(details: CustomerDetails) -> PostalAddress:
    """The address parts of a `CustomerDetails`, as the value object.

    Here rather than a method on the dataclass so `CustomerDetails` stays a
    plain carrier of what a caller sent, with no derived state that could
    disagree with the columns.
    """
    return PostalAddress(
        address_line1=details.address_line1,
        address_line2=details.address_line2,
        postal_code=details.postal_code,
        city=details.city,
        country=details.country,
    )

"""Sales invoicing - FR-AR-001 .. FR-AR-004.

--- The shape of the thing ---

    draft ──edit──▶ draft ──issue──▶ issued ──credit──▶ a NEW issued invoice
                                                        pointing at the first

`issue` is the only interesting transition. It does six things in one
transaction, and the order matters:

  1. resolve the VAT rates that applied ON THE INVOICE DATE (CMP-014)
  2. run FR-AR-003's statutory gate, and refuse if anything is missing
  3. resolve the posting: accounts, journal, period, debtor (FR-GL-006)
  4. allocate FR-AR-004's number - the database does this, in a trigger
  5. freeze the VAT totals
  6. post the entry, render the PDF, store it and link it (FR-TPL-017)

If the gate ran after the number was allocated, a refused invoice would have
burned a number out of a series that is supposed to be gapless. If the rates
were resolved after issue, a document already numbered could turn out to be
unpriceable. Step 3 joins them for the same reason: an administration with no
revenue account mapped must not discover that after taking a number. So
everything that can fail happens before the one thing that cannot be undone.

Step 6 is inside the same transaction, so "issued" and "in the books" are not
two states an invoice can be in separately. See `api.invoicing.posting` for why
that is not the split the expense side makes, and for why `send sales_invoice`
authorises the posting rather than `post journal_entry`.

--- Two permissions, and they are Appendix A's ---

    create sales_invoice   drafting and editing
    send sales_invoice     issuing and crediting

ADR-012: no invented permissions. Appendix A already carries both rows, and
§8.4 gives the Invoicer exactly them. Issuing is `send` rather than `create`
because it is the act that makes a claim to somebody outside the business -
the same reason posting an expense needs `post journal_entry` and not
`submit expense`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.service import AuthorizationService
from api.customers.service import InvoiceCustomerSnapshot
from api.i18n.language import DEFAULT_LANGUAGE, Language
from api.invoicing.model import (
    AlreadyCredited,
    CreditNoteMismatch,
    CustomerDetailsConflict,
    CustomerDetailsMissing,
    InvoiceAlreadyIssued,
    InvoiceLine,
    InvoiceNotFound,
    InvoiceNotIssued,
    InvoiceStatus,
    InvoiceView,
    NotAuthorizedToInvoice,
    NotStatutoryCompliant,
    SalesInvoice,
)
from api.invoicing.posting import SalesPostingService
from api.invoicing.statutory import (
    InvoiceForIssue,
    LineForIssue,
    SupplierDetails,
    check,
)
from api.invoicing.vat import InvoiceLineAmounts, totals_for
from api.invoicing.wording import unreviewed_wording, wording_for
from api.vat.rules import TreatmentRole

#: Appendix A rows, reused rather than invented (ADR-012).
CREATE_INVOICE = ("create", "sales_invoice")
SEND_INVOICE = ("send", "sales_invoice")


@dataclass(frozen=True, slots=True)
class NewLine:
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_treatment: str
    discount_percent: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Who the invoice is for, resolved from ONE of the two sources.

    Distinct from `api.customers.service.InvoiceCustomerSnapshot`, which always
    carries a due date because a customer master always has payment terms. A
    one-off customer typed straight onto the invoice may have no due date at
    all, so this one allows None - and keeping them apart is what stops that
    optionality leaking back into the customer master, where it would mean
    "this customer has no terms" rather than "this invoice names no date".
    """

    customer_id: uuid.UUID | None
    name: str
    address: str
    country: str
    vat_number: str | None
    language: Language
    due_date: date | None


class InvoiceRepository(Protocol):
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        invoice_date: date,
        customer_name: str,
        customer_address: str,
        customer_country: str,
        customer_vat_number: str | None,
        customer_id: uuid.UUID | None,
        customer_language: Language,
        supply_date: date | None,
        due_date: date | None,
        notes: str | None,
        user_id: uuid.UUID,
        credits_invoice_id: uuid.UUID | None,
    ) -> SalesInvoice: ...

    async def get(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice | None: ...

    async def replace_lines(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        lines: Sequence[NewLine],
    ) -> Sequence[InvoiceLine]: ...

    async def mark_issued(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice: ...

    async def write_vat_totals(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        groups: Sequence[Any],
    ) -> None: ...

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def rates_on(
        self, *, treatments: Sequence[str], on_date: date
    ) -> dict[str, Decimal | None]: ...

    async def roles_for(self, *, treatments: Sequence[str]) -> dict[str, TreatmentRole]: ...

    async def credit_note_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None: ...

    async def delete_draft(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> None: ...

    async def list_invoices(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[SalesInvoice]:
        """MOB-005's View tab: newest first, bounded, no cursor - the same
        shape `api.expenses.form.ExpenseFormRepository.list_by_status` and
        `api.customers.routes.list_customers` both take. Includes drafts, so a
        half-finished mobile-created invoice is resumable rather than
        disappearing until issued.
        """
        ...


class CustomerSnapshotSource(Protocol):
    """The narrow slice of the customer master FR-AR-001 needs.

    A Protocol rather than a direct dependency on `CustomerService`, so
    invoicing depends on ONE method and not on everything a customer can do -
    and so a test can supply a snapshot without a repository, an authorization
    service and a VIES client behind it.
    """

    async def snapshot_for_invoice(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        invoice_date: date,
    ) -> InvoiceCustomerSnapshot: ...


class InvoicingService:
    def __init__(
        self,
        repository: InvoiceRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
        customers: CustomerSnapshotSource,
        posting: SalesPostingService,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log
        self._customers = customers
        # Required, not optional. An issued invoice that reached no books is the
        # gap ADR-037 recorded and this closes; a constructor that let a caller
        # omit the posting service would make that gap reachable again by
        # forgetting an argument.
        self._posting = posting

    # -- FR-AR-001: create and edit -----------------------------------------

    async def create_draft(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        invoice_date: date,
        customer_id: uuid.UUID | None = None,
        customer_name: str | None = None,
        customer_address: str | None = None,
        customer_country: str | None = None,
        customer_vat_number: str | None = None,
        customer_language: Language | None = None,
        supply_date: date | None = None,
        due_date: date | None = None,
        notes: str | None = None,
        lines: Sequence[NewLine] = (),
        correlation_id: str | None = None,
    ) -> InvoiceView:
        """FR-AR-001's draft, from a customer record or from typed-in details.

        Both paths are permanently supported. FR-AR-006's master is a
        convenience that FILLS IN 0037's snapshot columns; it is not a
        prerequisite for invoicing somebody once (0039 keeps `customer_id`
        nullable for exactly that).

        Supplying both is refused rather than resolved - see
        `CustomerDetailsConflict`.
        """
        await self._require(CREATE_INVOICE, actor_user_id, administration_id)
        organization_id = await self._organization_of(administration_id)

        snapshot = await self._customer_snapshot(
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            invoice_date=invoice_date,
            customer_id=customer_id,
            customer_name=customer_name,
            customer_address=customer_address,
            customer_country=customer_country,
            customer_vat_number=customer_vat_number,
            customer_language=customer_language,
            due_date=due_date,
        )

        invoice = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            invoice_date=invoice_date,
            customer_name=snapshot.name,
            customer_address=snapshot.address,
            customer_country=snapshot.country,
            customer_vat_number=snapshot.vat_number,
            customer_id=snapshot.customer_id,
            customer_language=snapshot.language,
            supply_date=supply_date,
            # FR-AR-006's payment terms, already turned into a date by the
            # customer master. An explicit `due_date` still wins - see
            # _customer_snapshot.
            due_date=snapshot.due_date,
            notes=notes,
            user_id=actor_user_id,
            credits_invoice_id=None,
        )
        if lines:
            await self._repository.replace_lines(
                administration_id=administration_id, invoice_id=invoice.id, lines=lines
            )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_sales_invoice",
            resource_id=invoice.id,
            correlation_id=correlation_id,
            detail={"lines": len(lines)},
        )
        return await self.view(
            administration_id=administration_id,
            invoice_id=invoice.id,
            actor_user_id=actor_user_id,
        )

    async def set_lines(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        lines: Sequence[NewLine],
    ) -> InvoiceView:
        """FR-AR-001's edit. Drafts only - 0037 refuses the rest in a trigger."""
        await self._require(CREATE_INVOICE, actor_user_id, administration_id)
        invoice = await self._draft_or_refuse(administration_id, invoice_id)

        await self._repository.replace_lines(
            administration_id=administration_id, invoice_id=invoice.id, lines=lines
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="edit_sales_invoice",
            resource_id=invoice.id,
            detail={"lines": len(lines)},
        )
        return await self.view(
            administration_id=administration_id,
            invoice_id=invoice.id,
            actor_user_id=actor_user_id,
        )

    async def discard_draft(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None:
        """A draft carries no number, so nothing is lost from the series."""
        await self._require(CREATE_INVOICE, actor_user_id, administration_id)
        await self._draft_or_refuse(administration_id, invoice_id)
        await self._repository.delete_draft(
            administration_id=administration_id, invoice_id=invoice_id
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="discard_sales_invoice_draft",
            resource_id=invoice_id,
            detail={},
        )

    # -- FR-AR-003 / FR-AR-004: issue ---------------------------------------

    async def issue(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> InvoiceView:
        """Number the invoice, freeze it, post it, and store the PDF as issued.

        Five things, in one transaction, and the order is the whole design:

          1. FR-AR-003's statutory gate            can fail
          2. resolve the posting (accounts, period, journal, debtor)  can fail
          3. allocate FR-AR-004's number           CANNOT BE UNDONE
          4. freeze the VAT totals
          5. post the entry, render the PDF, store and link it

        Both things that can fail happen before the one that cannot. ADR-037
        established that ordering for the statutory gate; step 2 joins it,
        because an administration with no revenue account mapped must not burn
        a number discovering that. The rollback would take the number back
        anyway - 0037 allocates from a table rather than a sequence precisely so
        it does - but a refusal that never touches the allocator is better than
        one that relies on undoing it.

        Step 5 is inside the same transaction as steps 3 and 4, so an invoice
        that is issued and an invoice that is in the books are the same invoice.
        An issued invoice the ledger does not know about would be a receivable
        nobody chases and turnover missing from the aangifte; there is no state
        in which one exists without the other, which is why
        `invoicing.unposted_invoices()` should always be empty.

        Takes no `language`: FR-TPL-013's legal wording is rendered in the
        RECIPIENT's language, which the invoice carries as a frozen snapshot
        (`customer_language`, migration 0039). Whoever is clicking issue does
        not decide what language the customer's document is in.

        Authorised by `send sales_invoice` alone, NOT by `post journal_entry` -
        see `api.invoicing.posting`'s module docstring, which is where that
        reading of Appendix A is argued.
        """
        await self._require(SEND_INVOICE, actor_user_id, administration_id)
        invoice = await self._draft_or_refuse(administration_id, invoice_id)

        view = await self._build_view(invoice)
        if view.statutory_failures:
            await self._record(
                administration_id=administration_id,
                user_id=actor_user_id,
                action="issue_sales_invoice",
                resource_id=invoice.id,
                outcome=AuditOutcome.FAILURE,
                detail={"missing": [f.field.value for f in view.statutory_failures]},
            )
            raise NotStatutoryCompliant(view.statutory_failures)

        # Step 2. Everything the posting needs, resolved while a refusal is
        # still free.
        plan = await self._posting.prepare(view, actor_user_id=actor_user_id)

        issued = await self._repository.mark_issued(
            administration_id=administration_id, invoice_id=invoice.id
        )
        # Frozen after the number, because a total written against an invoice
        # that then failed to issue would describe a document that does not
        # exist.
        await self._repository.write_vat_totals(
            administration_id=administration_id, invoice_id=invoice.id, groups=view.groups
        )

        # Rebuilt on the ISSUED row, because the reference only exists now - and
        # it goes on the entry, in the PDF, and in the filename. Rendering the
        # draft would produce a document with no number on it.
        issued_view = await self._build_view(issued)
        result = await self._posting.commit(
            issued_view, plan, actor_user_id=actor_user_id, correlation_id=correlation_id
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="issue_sales_invoice",
            resource_id=invoice.id,
            correlation_id=correlation_id,
            detail={
                "invoice_reference": issued.invoice_reference,
                "invoice_number": issued.invoice_number,
                "net": str(view.net),
                "vat": str(view.vat),
                "gross": str(view.gross),
                "treatments": [group.treatment for group in view.groups],
                # The two facts that make this invoice findable from the books
                # and from the archive. The ledger writes its own POSTING event
                # (IAM-090); this records that the INVOICE reached them.
                "journal_entry_id": str(result.entry.id),
                "entry_number": result.entry.entry_number,
                "document_id": str(result.document.id),
                # FR-TPL-017: the hash of the bytes the customer received. An
                # auditor asking "is the PDF in the archive the one that was
                # issued" gets an answer that does not depend on the archive
                # being the only witness to itself.
                "document_sha256": result.document.content_hash.hex(),
            },
        )
        return await self.view(
            administration_id=administration_id,
            invoice_id=invoice.id,
            actor_user_id=actor_user_id,
        )

    # -- FR-AR-001: credit --------------------------------------------------

    async def credit(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> InvoiceView:
        """A credit note for the whole of an issued invoice.

        A NEW document with its own number from the same series, carrying the
        original's lines with the quantities negated. The original is not
        touched - CLAUDE.md's second rule, one layer up from the ledger: the
        record of what was claimed has to survive the correction of it.
        """
        await self._require(SEND_INVOICE, actor_user_id, administration_id)
        original = await self._get_or_refuse(administration_id, invoice_id)

        if original.status is not InvoiceStatus.ISSUED:
            raise InvoiceNotIssued(
                f"invoice {invoice_id} is a draft; there is nothing to credit. "
                f"Edit or discard it instead (FR-AR-001)."
            )
        if original.is_credit_note:
            raise CreditNoteMismatch(f"{invoice_id} is itself a credit note")
        existing = await self._repository.credit_note_for(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if existing is not None:
            raise AlreadyCredited(
                f"invoice {invoice_id} has already been credited by {existing}; a "
                f"second credit note would double the correction"
            )

        organization_id = await self._organization_of(administration_id)
        note = await self._repository.create(
            organization_id=organization_id,
            administration_id=administration_id,
            fiscal_year_id=original.fiscal_year_id,
            invoice_date=original.invoice_date,
            # Copied from the ORIGINAL, never re-read from the customer master.
            # A credit note corrects a specific document, so it has to be
            # addressed exactly as that document was - even if the customer has
            # since moved, changed name or switched language.
            customer_name=original.customer_name,
            customer_address=original.customer_address,
            customer_country=original.customer_country,
            customer_vat_number=original.customer_vat_number,
            customer_id=original.customer_id,
            customer_language=original.customer_language,
            supply_date=original.supply_date,
            due_date=None,
            notes=original.notes,
            user_id=actor_user_id,
            credits_invoice_id=original.id,
        )
        await self._repository.replace_lines(
            administration_id=administration_id,
            invoice_id=note.id,
            lines=[
                NewLine(
                    description=line.description,
                    # Negated quantity rather than negated price: the price is
                    # what was charged and stays legible on the credit note,
                    # which is what a customer reconciles against.
                    quantity=-line.quantity,
                    unit_price=line.unit_price,
                    vat_treatment=line.vat_treatment,
                    discount_percent=line.discount_percent,
                )
                for line in original.lines
            ],
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="create_credit_note",
            resource_id=note.id,
            correlation_id=correlation_id,
            detail={"credits": str(original.id), "credits_reference": original.invoice_reference},
        )
        # Issued immediately: a credit note that sat as a draft would leave the
        # original standing uncorrected while appearing to have been dealt with.
        return await self.issue(
            administration_id=administration_id,
            invoice_id=note.id,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

    # -- reading -------------------------------------------------------------

    async def view(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> InvoiceView:
        await self._require(CREATE_INVOICE, actor_user_id, administration_id)
        invoice = await self._get_or_refuse(administration_id, invoice_id)
        return await self._build_view(invoice)

    async def list_invoices(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID, limit: int
    ) -> Sequence[SalesInvoice]:
        """MOB-005's View tab. Authorised by `create sales_invoice`, the same
        permission `view`/`create`/`edit` already require - reading back what
        this administration may create is not a separate capability
        (ADR-012).

        Returns bare `SalesInvoice` rows rather than a full `InvoiceView` per
        row: the VAT breakdown and statutory-failure check are per-invoice
        reads a person needs only once they open a specific one (`GET
        .../sales-invoices/{id}`, which already computes them) - doing that
        for every row here would be a rates lookup per invoice in the list.
        """
        await self._require(CREATE_INVOICE, actor_user_id, administration_id)
        return await self._repository.list_invoices(
            administration_id=administration_id, limit=limit
        )

    async def _build_view(self, invoice: SalesInvoice) -> InvoiceView:
        treatments = sorted({line.vat_treatment for line in invoice.lines})
        rates = await self._repository.rates_on(treatments=treatments, on_date=invoice.invoice_date)

        amounts = [
            InvoiceLineAmounts(treatment=line.vat_treatment, role=line.role, net=line.line_net)
            for line in invoice.lines
        ]
        totals = totals_for(amounts, rate_for=rates)

        # An administration that does not exist supplies nothing, and every
        # statutory field is then reported missing rather than the view
        # failing. The gate refuses the issue either way; this way the caller
        # sees why.
        supplier = await self._repository.supplier(
            administration_id=invoice.administration_id
        ) or SupplierDetails(
            legal_name=None,
            address_line1=None,
            postal_code=None,
            city=None,
            vat_number=None,
            kvk_number=None,
        )

        # A rate-bearing treatment the ruleset does not cover on this date. The
        # gate needs this because computing VAT from a missing rate would put a
        # wrong figure on both the invoice and the return.
        unresolvable = frozenset(
            treatment
            for treatment, rate in rates.items()
            if rate is None
            and any(
                line.role not in _NO_RATE_ROLES
                for line in invoice.lines
                if line.vat_treatment == treatment
            )
        )

        failures = check(
            InvoiceForIssue(
                supplier=supplier,
                customer_name=invoice.customer_name,
                customer_address=invoice.customer_address,
                customer_vat_number=invoice.customer_vat_number,
                invoice_date=invoice.invoice_date,
                lines=[
                    LineForIssue(
                        position=line.position,
                        description=line.description,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        treatment=line.vat_treatment,
                        role=line.role,
                        net=line.line_net,
                    )
                    for line in invoice.lines
                ],
                unresolvable_treatments=unresolvable,
            )
        )

        # FR-TPL-013: the RECIPIENT's language, taken from the document's own
        # snapshot rather than from whoever is looking at it. A Dutch
        # bookkeeper invoicing a German customer in English gets English
        # wording on the invoice while their own screen stays Dutch - and an
        # invoice already issued keeps the words it was issued with, because
        # `customer_language` is frozen (0039).
        wording = {
            group.treatment: wording_for(group.role, invoice.customer_language).text
            for group in totals.groups
        }
        provisional = any(group.role in unreviewed_wording() for group in totals.groups)

        return InvoiceView(
            invoice=invoice,
            groups=totals.groups,
            net=totals.net,
            vat=totals.vat,
            gross=totals.gross,
            wording=wording,
            statutory_failures=failures,
            wording_is_provisional=provisional,
        )

    # -- internals -----------------------------------------------------------

    async def _customer_snapshot(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        invoice_date: date,
        customer_id: uuid.UUID | None,
        customer_name: str | None,
        customer_address: str | None,
        customer_country: str | None,
        customer_vat_number: str | None,
        customer_language: Language | None,
        due_date: date | None,
    ) -> _Snapshot:
        """Resolve who the invoice is for, from one source or the other.

        The two paths never blend. A request that names a customer AND types
        details out by hand is refused (`CustomerDetailsConflict`), because
        whichever this code silently preferred would be wrong for somebody, on
        a document that outlives the request.
        """
        supplied = tuple(
            field
            for field, value in (
                ("customer_name", customer_name),
                ("customer_address", customer_address),
                ("customer_country", customer_country),
                ("customer_vat_number", customer_vat_number),
                ("customer_language", customer_language),
            )
            if value is not None
        )

        if customer_id is not None:
            if supplied:
                raise CustomerDetailsConflict(supplied)
            master = await self._customers.snapshot_for_invoice(
                administration_id=administration_id,
                customer_id=customer_id,
                actor_user_id=actor_user_id,
                invoice_date=invoice_date,
            )
            return _Snapshot(
                customer_id=master.customer_id,
                name=master.name,
                address=master.address,
                country=master.country,
                vat_number=master.vat_number,
                language=master.language,
                # An explicit due date wins over the customer's standard terms.
                # A one-off arrangement on a single invoice is ordinary, and
                # the alternative - edit the customer, raise the invoice, edit
                # the customer back - is how a standing term gets left wrong.
                due_date=due_date or master.due_date,
            )

        if customer_name is None or customer_address is None:
            # 0037 makes both NOT NULL, and it is right to: a document that
            # cannot say who it is addressed to is not an invoice. Refused here
            # so the caller gets the field names rather than a constraint name.
            #
            # Written as an `or` rather than a comprehension over the pair so
            # the type checker narrows both to `str` below - the alternative
            # needs a cast, and a cast is how a None reaches a NOT NULL column.
            raise CustomerDetailsMissing(
                tuple(
                    field
                    for field, value in (
                        ("customer_name", customer_name),
                        ("customer_address", customer_address),
                    )
                    if value is None
                )
            )

        return _Snapshot(
            customer_id=None,
            name=customer_name,
            address=customer_address,
            country=customer_country or "NL",
            vat_number=customer_vat_number,
            # No customer record, so nothing knows better than the default.
            # FR-TPL-013's recipient language falls back to the product's
            # default (api.i18n.language.DEFAULT_LANGUAGE) rather than to the
            # caller's own: the words go on the customer's document, and the
            # caller's UI language says nothing about what the customer reads.
            language=customer_language or DEFAULT_LANGUAGE,
            due_date=due_date,
        )

    async def _get_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice:
        invoice = await self._repository.get(
            administration_id=administration_id, invoice_id=invoice_id
        )
        if invoice is None:
            raise InvoiceNotFound(f"sales invoice {invoice_id} not found")
        return invoice

    async def _draft_or_refuse(
        self, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice:
        invoice = await self._get_or_refuse(administration_id, invoice_id)
        if invoice.status is not InvoiceStatus.DRAFT:
            raise InvoiceAlreadyIssued(
                f"invoice {invoice.invoice_reference} has been issued and cannot be "
                f"changed; correct it with a credit note (FR-AR-001)"
            )
        return invoice

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise InvoiceNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(
        self,
        permission: tuple[str, str],
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> None:
        action, resource_type = permission
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
            raise NotAuthorizedToInvoice(action, resource_type, decision.detail or decision.reason)

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
                # IAM-090. An invoice is a claim made to somebody outside the
                # business and a number in a gapless statutory series; an
                # auditor reads the issue events as that series.
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="sales_invoice",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


#: Roles that never carry a rate, so a missing one is not a data gap.
_NO_RATE_ROLES = frozenset(
    {
        TreatmentRole.EXEMPT,
        TreatmentRole.REVERSE_CHARGE,
        TreatmentRole.INTRA_COMMUNITY,
        TreatmentRole.EXPORT,
        TreatmentRole.MARGIN,
    }
)

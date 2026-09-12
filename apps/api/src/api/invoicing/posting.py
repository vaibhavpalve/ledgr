"""An issued invoice reaching the books - FR-GL-006, FR-AR-001.

--- Through the ledger's own API, never around it ---

CLAUDE.md's first non-negotiable, and the same opening `api.expenses.posting`
makes for the purchase side: this module builds an `EntryInput` and hands it to
`LedgerService.post`. It holds no SQL against `journal_entry` or `journal_line`,
and `ledgr_app` has no grant that would let it if it tried.

--- The entry ---

    Dr  Debiteuren (AR control)      gross     <- carries the sub-ledger party
    Cr  Omzet <treatment>            taxable   } one pair per VAT group
    Cr  Te betalen omzetbelasting    vat       }

The credit side is per VAT GROUP, not per line. Art. 226 asks an invoice to show
the taxable amount and the VAT per rate; `sales_invoice_vat_total` (0037) freezes
exactly those figures at issue, and posting a different decomposition would put
one set of numbers on the document and another in the books - which is the
specific failure FR-VAT-001 would then inherit, because the rubriek reads the
ledger.

It balances because `net + vat == gross` is an identity `InvoiceTotals` asserts
on construction, not an arithmetic hope. The ledger checks it again at COMMIT,
which is where the guarantee actually lives (FR-GL-001).

--- A credit note exchanges the sides; it does not negate the amounts ---

`journal_line` refuses negative amounts - 0020: "amounts are unsigned; direction
is carried by which of debit/credit the amount is on". A credit note's lines
carry negative quantities (ADR-037 §"credit"), so its totals are negative, and
the posting takes their absolute value with debit and credit exchanged:

    Dr  Omzet / Btw            Cr  Debiteuren

That is also the correct accounting independently of the constraint. A negative
debit would balance and would break every report that sums a column.

--- Who may cause this posting, and why it is not `post journal_entry` ---

Appendix A grades "Post journal entries" and "Send sales invoices" differently,
and the Invoicer is the row where they part: F for sending, — for posting. So
requiring `post journal_entry` here would mean an Invoicer cannot issue an
invoice, contradicting the matrix cell that says they can.

The resolution is that posting is not a second discretionary act. Issuing an
invoice IS the accounting event - revenue is recognised at the invoice date and
the customer owes the money from that moment - so the entry is a mechanical
consequence of an authority the actor already holds, not an additional one they
must also hold. `send sales_invoice` authorises the whole of it, and
`api.invoicing.service.issue` is where that check lives.

The actor is still recorded (FR-GL-004, IAM-060): the entry names the person who
issued the invoice, because we know exactly who caused it. It is not an
`ActorType.SYSTEM` posting like a bank import - nobody would be able to answer
"who did this" of those if we pretended a person had not.

--- Why issuing and posting are one transaction ---

An issued invoice that the books do not know about is a receivable nobody will
chase and turnover missing from the aangifte. The expense side splits capture
from posting (ADR-031/ADR-033) because a submitter may not post - an SoD
boundary that genuinely exists there. There is no such boundary here, per the
section above, so the split would buy nothing and cost the guarantee.

The cost is real and is stated in ADR-039: an administration with no revenue
account mapped cannot issue an invoice at all. `prepare()` is why that is
tolerable - every configuration failure is raised BEFORE the number is
allocated, so a misconfigured administration gets a message naming what is
missing rather than a hole in FR-AR-004's gapless series.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from api.documents.model import Document
from api.documents.retention import RetentionBasis
from api.documents.service import DocumentService
from api.invoicing.model import InvoiceView, SalesInvoice
from api.invoicing.rendering import InvoiceRenderer, default_template
from api.invoicing.statutory import SupplierDetails
from api.invoicing.vat import VatGroup
from api.ledger.model import ZERO, EntryInput, LineInput, PartyKind, PostedEntry
from api.ledger.service import LedgerService
from api.templates.model import DocumentType, InvoiceTemplate

__all__ = [
    "SalesPostingRepository",
    "SalesPostingService",
    "PostingConfigurationMissing",
    "NoOpenPeriod",
    "NoSalesJournal",
    "InvoicePostingResult",
]

#: Purposes in `sales_posting_account` (migration 0040). The AR control account
#: is deliberately absent - FR-GL-006 already designates it and 0020 already
#: makes it unique per administration, so a mapping row would be a second answer
#: to a question that has one. See 0040's header.
REVENUE = "revenue"
VAT_OUTPUT = "vat_output"


class PostingConfigurationMissing(Exception):
    """No account is mapped for something this posting needs.

    Names what is missing rather than falling back to an account that looked
    close - `api.expenses.posting`'s argument, and it bites harder here: revenue
    in the wrong account is revenue in the wrong rubriek on the aangifte, which
    is wrong in a way the trial balance cannot show because it still balances.
    """

    def __init__(self, purpose: str, detail: str) -> None:
        self.purpose = purpose
        super().__init__(detail)


class NoSalesJournal(Exception):
    """FR-GL-002's journal for this kind of entry is missing or ambiguous."""


class NoOpenPeriod(Exception):
    """The invoice's date falls in no open period.

    Refused rather than moved to a period that is open. An invoice dated inside
    a locked or VAT-filed period belongs there, and posting the turnover
    somewhere else would put it in the wrong return to avoid an inconvenience -
    FR-VAT-005's suppletie is the route when the period is filed.
    """


@dataclass(frozen=True, slots=True)
class GroupAccounts:
    """Where one VAT group's two credit lines go."""

    treatment: str
    revenue_account_id: uuid.UUID
    #: None where the group carries no VAT - five of FR-AR-002's eight
    #: treatments charge the customer nothing, so no output-VAT line is written
    #: and no output-VAT account needs mapping for them.
    vat_account_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class PostingPlan:
    """Everything the entry needs, resolved and refused BEFORE any number is
    allocated. Passed as one value so the entry builder cannot reach for
    configuration halfway through and post a half-configured invoice.
    """

    journal_id: uuid.UUID
    period_id: uuid.UUID
    receivable_account_id: uuid.UUID
    subledger_party_id: uuid.UUID
    groups: tuple[GroupAccounts, ...]


@dataclass(frozen=True, slots=True)
class InvoicePostingResult:
    entry: PostedEntry
    document: Document


class SalesPostingRepository(Protocol):
    async def sales_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        """The OPEN period containing `on`, or None - which covers "no period
        exists" and "it is locked" alike.
        """
        ...

    async def receivable_control_account(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        """FR-GL-006's AR control account. At most one per administration, by
        `ledger_account_one_control_per_kind_idx`.
        """
        ...

    async def posting_account(
        self, *, administration_id: uuid.UUID, purpose: str, vat_treatment: str
    ) -> uuid.UUID | None:
        """The mapped account: the exact treatment match, falling back to the
        administration's fallback row (migration 0040).
        """
        ...

    async def customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID | None: ...

    async def link_customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, party_id: uuid.UUID
    ) -> None: ...

    async def mark_posted(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        entry_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None: ...

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None: ...

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def default_invoice_template(
        self, *, administration_id: uuid.UUID
    ) -> InvoiceTemplate | None:
        """FR-TPL-008/017: the administration's `is_default` template, if it
        has saved one - `None` when it has not, which
        `SalesPostingService._store_rendering` maps to the same built-in look
        `api.invoicing.rendering.default_template` gives a brand-new template,
        so issuing an invoice never depends on the designer having been used
        first.
        """
        ...


class SalesPostingService:
    """FR-AR-001's last step, and FR-TPL-017's only one."""

    def __init__(
        self,
        repository: SalesPostingRepository,
        ledger: LedgerService,
        documents: DocumentService,
        renderer: InvoiceRenderer,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._documents = documents
        self._renderer = renderer

    # -- phase one: everything that can fail on configuration ---------------

    async def prepare(self, view: InvoiceView, *, actor_user_id: uuid.UUID) -> PostingPlan:
        """Resolve the accounts, the period, the journal and the debtor.

        Called BEFORE `mark_issued`, which is the whole point of splitting this
        from `commit`. FR-AR-004 wants the invoice series gapless; a number
        allocated to an invoice that then fails on a missing account mapping
        would be a hole an administrator has to explain to an inspector. The
        rollback would in fact take the number back with it (0037 allocates from
        a table, not a sequence, precisely so it does) - but a refusal that never
        touches the allocator is better than one that relies on undoing it.
        """
        invoice = view.invoice
        administration_id = invoice.administration_id

        journal_id = await self._repository.sales_journal(administration_id=administration_id)
        if journal_id is None:
            raise NoSalesJournal(
                f"administration {administration_id} has no single active sales "
                f"journal to post an invoice into (FR-GL-002)"
            )

        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=invoice.invoice_date
        )
        if period_id is None:
            raise NoOpenPeriod(
                f"{invoice.invoice_date} falls in no open period. An invoice is "
                f"posted in the period it belongs to; if that period is filed, the "
                f"route is a suppletie (FR-VAT-005), not another period."
            )

        receivable = await self._repository.receivable_control_account(
            administration_id=administration_id
        )
        if receivable is None:
            raise PostingConfigurationMissing(
                "accounts_receivable",
                "this administration's chart has no accounts-receivable control "
                "account, so there is nothing to debit. FR-GL-006 makes the "
                "receivable a control account with a sub-ledger; the RGS profile "
                "seeds one as 'Debiteuren'.",
            )

        groups = tuple([await self._accounts_for(view, group) for group in view.groups])
        party_id = await self._party_for(invoice, actor_user_id=actor_user_id)

        return PostingPlan(
            journal_id=journal_id,
            period_id=period_id,
            receivable_account_id=receivable,
            subledger_party_id=party_id,
            groups=groups,
        )

    # -- phase two: what follows from the number existing --------------------

    async def commit(
        self,
        view: InvoiceView,
        plan: PostingPlan,
        *,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> InvoicePostingResult:
        """Post the entry, render the invoice, store it, and link the two.

        `view` must be the ISSUED view - it carries the invoice reference, which
        goes on the entry and onto the document. Everything here happens in the
        caller's transaction, so a failure at any step takes the posting, the
        document row, the number and the status back with it.

        The order inside is deliberate. The entry is posted first so the
        document has something to be linked TO; the rendering follows, and the
        `mark_posted` write is last because it is the only step that asserts
        both halves happened.
        """
        invoice = view.invoice
        entry = await self._ledger.post(
            self._entry(view, plan, actor_user_id=actor_user_id),
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

        document = await self._store_rendering(
            view, actor_user_id=actor_user_id, correlation_id=correlation_id
        )
        # FR-DOC-003's bidirectional link, in the one case where the "source
        # document" is a document this system produced rather than received.
        # It is still the evidence for the entry, which is what the link means.
        await self._documents.link_to_posting(
            administration_id=invoice.administration_id,
            document_id=document.id,
            journal_entry_id=entry.id,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )

        await self._repository.mark_posted(
            administration_id=invoice.administration_id,
            invoice_id=invoice.id,
            entry_id=entry.id,
            document_id=document.id,
        )
        return InvoicePostingResult(entry=entry, document=document)

    # -- FR-TPL-017 ---------------------------------------------------------

    async def _store_rendering(
        self, view: InvoiceView, *, actor_user_id: uuid.UUID, correlation_id: str | None
    ) -> Document:
        """Render once, store the bytes, and never render again.

        Stored through `DocumentService.upload` rather than written anywhere of
        this module's own, so the invoice PDF inherits every guarantee FR-DOC
        already provides: encryption under the administration's own key, a hash
        taken of the original bytes and verified on every read, write-once
        storage, and CMP-001's seven-year retention derived by the database.
        Re-implementing any of that here would be a second archive with none of
        it.

        The upload authorises as `upload document`, which every role that may
        `send sales_invoice` already holds (Owner, Accountant, Bookkeeper,
        Invoicer - see `api.authz.matrix.ROLES`). That is a coincidence worth
        checking rather than relying on, and
        `tests/invoicing/test_posting.py` checks it.
        """
        invoice = view.invoice
        administration_id = invoice.administration_id

        supplier = await self._repository.supplier(administration_id=administration_id)
        if supplier is None:  # pragma: no cover - the statutory gate refuses first
            raise PostingConfigurationMissing(
                "supplier",
                f"administration {administration_id} does not exist",
            )
        locale = await self._repository.formatting_locale(administration_id=administration_id)

        template = await self._repository.default_invoice_template(
            administration_id=administration_id
        )
        if template is None:
            organization_id = await self._repository.organization_of(
                administration_id=administration_id
            )
            assert (
                organization_id is not None
            )  # pragma: no cover - the statutory gate refuses first
            template = default_template(
                organization_id=organization_id, administration_id=administration_id
            )
        # FR-TPL-012: which of the document family this is - a parameter, not
        # a fact the template itself carries. `invoice.is_credit_note` is the
        # one place this bit already lives (0037).
        document_type = DocumentType.CREDIT_NOTE if invoice.is_credit_note else DocumentType.INVOICE

        rendered = await self._renderer.render(
            view,
            supplier=supplier,
            formatting_locale=locale,
            template=template,
            document_type=document_type,
        )
        return await self._documents.upload(
            administration_id=administration_id,
            fiscal_year_id=invoice.fiscal_year_id,
            actor_user_id=actor_user_id,
            data=rendered.content,
            original_filename=rendered.filename,
            declared_content_type=rendered.content_type,
            # A sales invoice is ordinary business administratie: seven years
            # (AWR art. 52). The ten-year basis is for immovable property, which
            # is a judgement about the transaction and not something the
            # renderer could know - the same posture `DocumentService.upload`
            # takes about every other upload.
            retention_basis=RetentionBasis.STANDARD,
            correlation_id=correlation_id,
        )

    # -- the entry ----------------------------------------------------------

    def _entry(
        self, view: InvoiceView, plan: PostingPlan, *, actor_user_id: uuid.UUID
    ) -> EntryInput:
        invoice = view.invoice
        return EntryInput(
            administration_id=invoice.administration_id,
            journal_id=plan.journal_id,
            period_id=plan.period_id,
            entry_date=invoice.invoice_date,
            description=_description(invoice),
            lines=_lines(view, plan),
            # What a person reconciling the ledger against a filing cabinet
            # looks for. The invoice reference, not the id.
            document_reference=invoice.invoice_reference,
            posted_by_user_id=actor_user_id,
            source_system="invoicing",
            # NFR-032: the invoice's own id. A retry posts once, and
            # `sales_invoice_journal_entry_idx` (0040) catches the case where two
            # requests get past this at the same moment.
            idempotency_key=f"sales_invoice:{invoice.id}",
        )

    # -- internals ----------------------------------------------------------

    async def _accounts_for(self, view: InvoiceView, group: VatGroup) -> GroupAccounts:
        treatment = group.treatment
        vat_amount = group.vat
        administration_id = view.invoice.administration_id

        revenue = await self._repository.posting_account(
            administration_id=administration_id, purpose=REVENUE, vat_treatment=treatment
        )
        if revenue is None:
            raise PostingConfigurationMissing(
                REVENUE,
                f"no revenue account is mapped for the VAT treatment {treatment!r}, "
                f"and this administration has no fallback revenue account. Map one "
                f"before issuing: turnover in the wrong account is turnover in the "
                f"wrong rubriek on the aangifte.",
            )

        # Only where there is VAT to post. An administration that only ever
        # invoices reverse-charged or exempt supplies never needs an output-VAT
        # account mapped, and demanding one would refuse invoices it can post
        # perfectly well.
        vat_account: uuid.UUID | None = None
        if vat_amount != Decimal("0.00"):
            vat_account = await self._repository.posting_account(
                administration_id=administration_id,
                purpose=VAT_OUTPUT,
                vat_treatment=treatment,
            )
            if vat_account is None:
                raise PostingConfigurationMissing(
                    VAT_OUTPUT,
                    f"no output-VAT account is mapped for {treatment!r}, and this "
                    f"invoice charges {vat_amount} of BTW. Map one before issuing.",
                )

        return GroupAccounts(
            treatment=treatment,
            revenue_account_id=revenue,
            vat_account_id=vat_account,
        )

    async def _party_for(self, invoice: SalesInvoice, *, actor_user_id: uuid.UUID) -> uuid.UUID:
        """The debtor this receivable is owed by - FR-GL-006.

        Two paths, and the difference between them is the argument for keeping a
        customer master at all:

          * a MASTERED customer (FR-AR-006) maps to one party, created on first
            posting and reused forever. FR-AR-012's per-customer statement of
            account is one party's lines, which is what makes it a statement.
          * a ONE-OFF customer typed straight onto the invoice gets a party of
            its own, named from the invoice's own snapshot. That is honest -
            somebody does owe the money and the sub-ledger has to say who - and
            it is a real cost: invoice the same one-off name twice and the aged
            receivables shows two debtors. The fix is to make them a customer,
            which is exactly the trade FR-AR-006 exists to offer.
        """
        administration_id = invoice.administration_id

        if invoice.customer_id is not None:
            existing = await self._repository.customer_party(
                administration_id=administration_id, customer_id=invoice.customer_id
            )
            if existing is not None:
                return existing

            party = await self._ledger.create_party(
                administration_id=administration_id,
                party_kind=PartyKind.CUSTOMER,
                name=invoice.customer_name,
                # The customer master row this party is. Lets somebody reading
                # the sub-ledger get back to the record without a join through
                # a table the ledger does not know about.
                external_reference=f"customer:{invoice.customer_id}",
            )
            await self._repository.link_customer_party(
                administration_id=administration_id,
                customer_id=invoice.customer_id,
                party_id=party.id,
            )
            return party.id

        party = await self._ledger.create_party(
            administration_id=administration_id,
            party_kind=PartyKind.CUSTOMER,
            name=invoice.customer_name,
            external_reference=f"sales_invoice:{invoice.id}",
        )
        return party.id


def _description(invoice: SalesInvoice) -> str:
    """FR-GL-004 requires one, and it is what a person scanning a journal reads.

    The reference and the customer, because those identify the document; the
    amount is in the columns beside it.
    """
    reference = invoice.invoice_reference or "concept"
    return f"{reference} - {invoice.customer_name}"


def _lines(view: InvoiceView, plan: PostingPlan) -> list[LineInput]:
    """The entry's lines.

    `credit_note` flips every side. See the module docstring: the amounts stay
    unsigned because `journal_line` requires it and because a negative debit
    would break every report that sums a column.
    """
    invoice = view.invoice
    credit_note = invoice.is_credit_note
    by_treatment = {group.treatment: group for group in plan.groups}

    receivable_debit, receivable_credit = _side(abs(view.gross), debit=not credit_note)
    receivable = LineInput(
        account_id=plan.receivable_account_id,
        debit=receivable_debit,
        credit=receivable_credit,
        # FR-GL-006: mandatory on a control account, and the biconditional in
        # `journal_line_validate()` refuses the line without it. This is the AR
        # sub-ledger - there is no separately maintained balance to drift.
        subledger_party_id=plan.subledger_party_id,
        description=_description(invoice),
        # No treatment on the receivable: it is the money owed, not the
        # turnover, and a code here would put the debtor balance in a rubriek
        # beside the sale.
    )

    lines = [receivable]
    for group in view.groups:
        accounts = by_treatment[group.treatment]
        if group.taxable:
            revenue_debit, revenue_credit = _side(abs(group.taxable), debit=credit_note)
            lines.append(
                LineInput(
                    account_id=accounts.revenue_account_id,
                    debit=revenue_debit,
                    credit=revenue_credit,
                    description=_description(invoice),
                    # FR-VAT-001 reads the TURNOVER from this line and the tax
                    # from the one below, so both carry the treatment.
                    vat_treatment=group.treatment,
                )
            )
        if group.vat and accounts.vat_account_id is not None:
            vat_debit, vat_credit = _side(abs(group.vat), debit=credit_note)
            lines.append(
                LineInput(
                    account_id=accounts.vat_account_id,
                    debit=vat_debit,
                    credit=vat_credit,
                    description=_description(invoice),
                    vat_treatment=group.treatment,
                )
            )
    return lines


def _side(amount: Decimal, *, debit: bool) -> tuple[Decimal, Decimal]:
    """Put an unsigned amount on one side, as `(debit, credit)`.

    A helper rather than a conditional at three call sites, because getting one
    of them backwards produces an entry that still balances - two flipped lines
    cancel - and reverses the sign of a customer's balance.
    """
    return (amount, ZERO) if debit else (ZERO, amount)

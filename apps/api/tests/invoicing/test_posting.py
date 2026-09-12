"""api.invoicing.posting - FR-GL-006, FR-TPL-017.

The real `LedgerService` over `InMemoryLedgerRepository`, the real renderer,
and a fake only for the SQL beneath. What is under test is the shape of the
entry - which account each amount lands on, which side it lands on, and that
the receivable names a debtor - because those are the facts a trial balance and
a VAT return are built from and none of them is checkable by reading the code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from api.documents.model import Document
from api.i18n.language import Language
from api.invoicing.model import InvoiceStatus, InvoiceView, SalesInvoice
from api.invoicing.posting import (
    NoOpenPeriod,
    NoSalesJournal,
    PostingConfigurationMissing,
    SalesPostingService,
)
from api.invoicing.rendering import TemplatedPdfRenderer
from api.invoicing.statutory import SupplierDetails
from api.invoicing.vat import VatGroup
from api.ledger.model import Party, PartyKind
from api.vat.rules import TreatmentRole

pytestmark = pytest.mark.anyio

ADMIN = uuid.uuid4()
ORG = uuid.uuid4()
USER = uuid.uuid4()
YEAR = uuid.uuid4()

AR = uuid.uuid4()
REVENUE_21 = uuid.uuid4()
REVENUE_ICP = uuid.uuid4()
VAT_OUT = uuid.uuid4()
JOURNAL = uuid.uuid4()
PERIOD = uuid.uuid4()

SUPPLIER = SupplierDetails(
    legal_name="Bakker Consultancy B.V.",
    address_line1="Damrak 70",
    address_line2=None,
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
    vat_number="NL123456789B01",
    kvk_number="12345678",
)


# --- fakes -------------------------------------------------------------------


@dataclass
class FakePostingRepository:
    """Migration 0040's tables, in memory. Field names match the columns."""

    journal: uuid.UUID | None = JOURNAL
    period: uuid.UUID | None = PERIOD
    receivable: uuid.UUID | None = AR
    #: (purpose, treatment | None) -> account. None is 0040's fallback row.
    accounts: dict[tuple[str, str | None], uuid.UUID] = field(default_factory=dict)
    customer_parties: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    posted: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = field(default_factory=dict)
    locale: str = "nl-NL"

    async def sales_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.journal

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        return self.period

    async def receivable_control_account(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.receivable

    async def posting_account(
        self, *, administration_id: uuid.UUID, purpose: str, vat_treatment: str
    ) -> uuid.UUID | None:
        # 0040's lookup: the exact treatment match, then the fallback row.
        exact = self.accounts.get((purpose, vat_treatment))
        return exact if exact is not None else self.accounts.get((purpose, None))

    async def customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID | None:
        return self.customer_parties.get(customer_id)

    async def link_customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, party_id: uuid.UUID
    ) -> None:
        # `customer_party_link_is_final_trg`: set once, never repointed.
        self.customer_parties.setdefault(customer_id, party_id)

    async def mark_posted(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        entry_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        assert invoice_id not in self.posted, (
            "sales_invoice_journal_entry_idx: one entry per invoice"
        )
        self.posted[invoice_id] = (entry_id, document_id)

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None:
        return SUPPLIER

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        return self.locale

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return ORG

    async def default_invoice_template(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        # None: this administration has never saved one, exercising
        # `SalesPostingService._store_rendering`'s
        # `api.invoicing.rendering.default_template` fallback - the case
        # every existing test in this file issues an invoice under.
        return None


@dataclass
class RecordingLedger:
    """Captures what was handed to `LedgerService.post`, and creates parties.

    A recorder rather than the real service, because what these tests assert is
    the ENTRY this module builds - `tests/ledger` already covers what the ledger
    does with one, and `tests/integration/test_sales_invoice_posting.py` runs
    both together against Postgres.
    """

    entries: list[object] = field(default_factory=list)
    parties: list[Party] = field(default_factory=list)

    async def post(self, entry, *, actor_user_id=None, correlation_id=None):  # type: ignore[no-untyped-def]
        # The invariant the real ledger enforces at COMMIT, asserted here so a
        # malformed entry fails in the test that built it.
        entry.assert_balanced()
        self.entries.append(entry)
        return _posted(entry)

    async def create_party(
        self,
        *,
        administration_id: uuid.UUID,
        party_kind: PartyKind,
        name: str,
        external_reference: str | None = None,
    ) -> Party:
        party = Party(
            id=uuid.uuid4(),
            administration_id=administration_id,
            party_kind=party_kind,
            name=name,
            external_reference=external_reference,
        )
        self.parties.append(party)
        return party


def _posted(entry):  # type: ignore[no-untyped-def]
    from api.ledger.model import PostedEntry

    return PostedEntry(
        id=uuid.uuid4(),
        organization_id=ORG,
        administration_id=entry.administration_id,
        fiscal_year_id=YEAR,
        period_id=entry.period_id,
        journal_id=entry.journal_id,
        entry_number=1,
        entry_date=entry.entry_date,
        description=entry.description,
        source_system=entry.source_system,
        posted_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        document_reference=entry.document_reference,
        posted_by_user_id=entry.posted_by_user_id,
        idempotency_key=entry.idempotency_key,
    )


@dataclass
class RecordingDocuments:
    """`DocumentService`'s two methods this module uses."""

    uploaded: list[bytes] = field(default_factory=list)
    links: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)

    async def upload(self, *, data: bytes, original_filename: str | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
        self.uploaded.append(data)
        self.filenames.append(original_filename or "")
        return _document(data)

    async def link_to_posting(
        self,
        *,
        administration_id: uuid.UUID,
        document_id: uuid.UUID,
        journal_entry_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ):  # type: ignore[no-untyped-def]
        self.links.append((document_id, journal_entry_id))
        return None


def _document(data: bytes) -> Document:
    import hashlib

    from api.documents.content_type import DocumentContentType
    from api.documents.model import DocumentStatus
    from api.documents.retention import RetentionBasis
    from api.documents.scanning import ScanStatus

    return Document(
        id=uuid.uuid4(),
        organization_id=ORG,
        administration_id=ADMIN,
        storage_key="k",
        content_hash=hashlib.sha256(data).digest(),
        byte_size=len(data),
        content_type=DocumentContentType.PDF,
        fiscal_year_id=YEAR,
        retention_basis=RetentionBasis.STANDARD,
        retention_until=date(2033, 12, 31),
        status=DocumentStatus.ACTIVE,
        scan_status=ScanStatus.CLEAN,
    )


# --- builders ----------------------------------------------------------------


def invoice(
    *,
    credit_of: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    reference: str = "2026-1",
) -> SalesInvoice:
    return SalesInvoice(
        id=uuid.uuid4(),
        organization_id=ORG,
        administration_id=ADMIN,
        fiscal_year_id=YEAR,
        status=InvoiceStatus.ISSUED,
        invoice_number=1,
        number_prefix="",
        invoice_reference=reference,
        invoice_date=date(2026, 9, 9),
        supply_date=None,
        due_date=date(2026, 10, 9),
        customer_name="De Vries Holding B.V.",
        customer_address="Damrak 70\n1012 LM  Amsterdam",
        customer_country="NL",
        customer_vat_number="NL987654321B01",
        customer_id=customer_id,
        customer_language=Language.NL,
        credits_invoice_id=credit_of,
        notes=None,
        issued_at=None,
    )


def view(
    *,
    groups: tuple[VatGroup, ...] | None = None,
    credit_of: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
) -> InvoiceView:
    if groups is None:
        groups = (
            VatGroup(
                treatment="btw_21",
                role=TreatmentRole.STANDARD,
                rate=Decimal("21.00"),
                taxable=Decimal("1000.00"),
                vat=Decimal("210.00"),
            ),
        )
    net = sum((g.taxable for g in groups), Decimal("0.00"))
    vat = sum((g.vat for g in groups), Decimal("0.00"))
    return InvoiceView(
        invoice=invoice(credit_of=credit_of, customer_id=customer_id),
        groups=groups,
        net=net,
        vat=vat,
        gross=net + vat,
        wording={g.treatment: "Btw verlegd" for g in groups},
    )


@dataclass
class Harness:
    service: SalesPostingService
    repository: FakePostingRepository
    ledger: RecordingLedger
    documents: RecordingDocuments


def harness(**repo_kwargs: object) -> Harness:
    repository = FakePostingRepository(**repo_kwargs)  # type: ignore[arg-type]
    repository.accounts.setdefault(("revenue", "btw_21"), REVENUE_21)
    repository.accounts.setdefault(("revenue", "btw_icp"), REVENUE_ICP)
    repository.accounts.setdefault(("vat_output", None), VAT_OUT)

    ledger = RecordingLedger()
    documents = RecordingDocuments()
    return Harness(
        service=SalesPostingService(
            repository=repository,  # type: ignore[arg-type]
            ledger=ledger,  # type: ignore[arg-type]
            documents=documents,  # type: ignore[arg-type]
            renderer=TemplatedPdfRenderer(),
        ),
        repository=repository,
        ledger=ledger,
        documents=documents,
    )


async def post(h: Harness, v: InvoiceView):  # type: ignore[no-untyped-def]
    plan = await h.service.prepare(v, actor_user_id=USER)
    return await h.service.commit(v, plan, actor_user_id=USER)


def lines_of(h: Harness):  # type: ignore[no-untyped-def]
    return h.ledger.entries[0].lines  # type: ignore[attr-defined]


# --- the entry shape ---------------------------------------------------------


async def test_an_invoice_debits_receivables_and_credits_revenue_and_vat() -> None:
    h = harness()
    await post(h, view())

    lines = lines_of(h)
    by_account = {line.account_id: line for line in lines}

    assert by_account[AR].debit == Decimal("1210.00")
    assert by_account[AR].credit == Decimal("0.00")
    assert by_account[REVENUE_21].credit == Decimal("1000.00")
    assert by_account[VAT_OUT].credit == Decimal("210.00")


async def test_the_entry_balances() -> None:
    """FR-GL-001. The real ledger re-derives this at COMMIT; asserting it here
    means a malformed entry fails in the test that built it.
    """
    h = harness()
    await post(h, view())

    entry = h.ledger.entries[0]
    assert entry.total_debit == entry.total_credit == Decimal("1210.00")  # type: ignore[attr-defined]


async def test_the_receivable_carries_the_subledger_party() -> None:
    """FR-GL-006. `journal_line_validate()` refuses a control-account line with
    no party, so an entry without this would be rejected by the database - but
    the point is the AR sub-ledger, not the constraint: this line IS the
    debtor's balance.
    """
    h = harness()
    await post(h, view())

    receivable = next(line for line in lines_of(h) if line.account_id == AR)
    assert receivable.subledger_party_id is not None


async def test_only_the_receivable_carries_a_party() -> None:
    """FR-GL-006's biconditional: required on a control account, forbidden
    anywhere else. A party on the revenue line would be refused.
    """
    h = harness()
    await post(h, view())

    for line in lines_of(h):
        if line.account_id != AR:
            assert line.subledger_party_id is None


async def test_the_revenue_and_vat_lines_carry_the_treatment() -> None:
    """FR-VAT-001 reads the turnover from the revenue line and the tax from the
    VAT line, so both need the code. Without it on the revenue line the rubriek
    has no taxable amount.
    """
    h = harness()
    await post(h, view())

    for line in lines_of(h):
        if line.account_id in (REVENUE_21, VAT_OUT):
            assert line.vat_treatment == "btw_21"


async def test_the_receivable_carries_no_treatment() -> None:
    """It is the money owed, not the turnover. A code here would put the debtor
    balance in a rubriek beside the sale and double-count the supply.
    """
    h = harness()
    await post(h, view())

    receivable = next(line for line in lines_of(h) if line.account_id == AR)
    assert receivable.vat_treatment is None


async def test_one_pair_of_lines_per_vat_group() -> None:
    """Art. 226 asks for the taxable amount and the VAT per rate, and 0037
    freezes exactly those figures. Posting a different decomposition would put
    one set of numbers on the document and another in the books.
    """
    h = harness()
    await post(
        h,
        view(
            groups=(
                VatGroup(
                    "btw_21",
                    TreatmentRole.STANDARD,
                    Decimal("21.00"),
                    Decimal("1000.00"),
                    Decimal("210.00"),
                ),
                VatGroup(
                    "btw_icp",
                    TreatmentRole.INTRA_COMMUNITY,
                    None,
                    Decimal("500.00"),
                    Decimal("0.00"),
                ),
            )
        ),
    )

    lines = lines_of(h)
    # AR + revenue(21) + vat(21) + revenue(icp). No VAT line for the ICP group.
    assert len(lines) == 4
    assert {line.account_id for line in lines} == {AR, REVENUE_21, VAT_OUT, REVENUE_ICP}


async def test_a_zero_vat_group_writes_no_vat_line() -> None:
    """Five of FR-AR-002's eight treatments charge the customer nothing. A
    0.00 line on the output-VAT account would be noise in the account and in
    every report that lists its movements.
    """
    h = harness()
    await post(
        h,
        view(
            groups=(
                VatGroup(
                    "btw_icp",
                    TreatmentRole.INTRA_COMMUNITY,
                    None,
                    Decimal("500.00"),
                    Decimal("0.00"),
                ),
            )
        ),
    )

    assert VAT_OUT not in {line.account_id for line in lines_of(h)}


# --- credit notes ------------------------------------------------------------


async def test_a_credit_note_exchanges_the_sides() -> None:
    """Not an entry with negative amounts: `journal_line` refuses those, and a
    negative debit would break every report that sums a column.
    """
    h = harness()
    await post(
        h,
        view(
            credit_of=uuid.uuid4(),
            groups=(
                VatGroup(
                    "btw_21",
                    TreatmentRole.STANDARD,
                    Decimal("21.00"),
                    Decimal("-1000.00"),
                    Decimal("-210.00"),
                ),
            ),
        ),
    )

    by_account = {line.account_id: line for line in lines_of(h)}
    assert by_account[AR].credit == Decimal("1210.00")
    assert by_account[AR].debit == Decimal("0.00")
    assert by_account[REVENUE_21].debit == Decimal("1000.00")
    assert by_account[VAT_OUT].debit == Decimal("210.00")


async def test_a_credit_note_posts_no_negative_amount() -> None:
    h = harness()
    await post(
        h,
        view(
            credit_of=uuid.uuid4(),
            groups=(
                VatGroup(
                    "btw_21",
                    TreatmentRole.STANDARD,
                    Decimal("21.00"),
                    Decimal("-1000.00"),
                    Decimal("-210.00"),
                ),
            ),
        ),
    )

    for line in lines_of(h):
        assert line.debit >= 0 and line.credit >= 0


async def test_a_credit_note_balances_too() -> None:
    h = harness()
    await post(
        h,
        view(
            credit_of=uuid.uuid4(),
            groups=(
                VatGroup(
                    "btw_21",
                    TreatmentRole.STANDARD,
                    Decimal("21.00"),
                    Decimal("-1000.00"),
                    Decimal("-210.00"),
                ),
            ),
        ),
    )

    entry = h.ledger.entries[0]
    assert entry.total_debit == entry.total_credit  # type: ignore[attr-defined]


# --- the AR sub-ledger party -------------------------------------------------


async def test_a_mastered_customer_gets_one_party_and_keeps_it() -> None:
    """FR-AR-012's per-customer statement is one party's lines. Two parties for
    one customer would show half the balance on each and neither would be the
    statement.
    """
    customer = uuid.uuid4()
    h = harness()

    await post(h, view(customer_id=customer))
    first = h.repository.customer_parties[customer]

    h.ledger.entries.clear()
    await post(h, view(customer_id=customer))
    second = next(line for line in lines_of(h) if line.account_id == AR)

    assert second.subledger_party_id == first
    assert len(h.ledger.parties) == 1, "a second party was created for one customer"


async def test_the_party_points_back_at_the_customer_record() -> None:
    customer = uuid.uuid4()
    h = harness()
    await post(h, view(customer_id=customer))

    assert h.ledger.parties[0].external_reference == f"customer:{customer}"
    assert h.ledger.parties[0].party_kind is PartyKind.CUSTOMER


async def test_a_one_off_customer_still_gets_a_debtor() -> None:
    """Somebody does owe the money and the sub-ledger has to say who. The cost
    is that two invoices to the same one-off name are two debtors - which is
    exactly the trade FR-AR-006's customer master exists to offer.
    """
    h = harness()
    invoice_view = view(customer_id=None)
    await post(h, invoice_view)

    receivable = next(line for line in lines_of(h) if line.account_id == AR)
    assert receivable.subledger_party_id is not None
    assert h.ledger.parties[0].external_reference == f"sales_invoice:{invoice_view.invoice.id}"


# --- configuration refusals --------------------------------------------------


async def test_a_missing_revenue_mapping_is_refused_by_name() -> None:
    h = harness()
    h.repository.accounts.clear()
    h.repository.accounts[("vat_output", None)] = VAT_OUT

    with pytest.raises(PostingConfigurationMissing) as caught:
        await h.service.prepare(view(), actor_user_id=USER)

    assert caught.value.purpose == "revenue"
    assert "btw_21" in str(caught.value)


async def test_a_missing_output_vat_mapping_is_refused_when_there_is_vat() -> None:
    h = harness()
    del h.repository.accounts[("vat_output", None)]

    with pytest.raises(PostingConfigurationMissing) as caught:
        await h.service.prepare(view(), actor_user_id=USER)

    assert caught.value.purpose == "vat_output"


async def test_no_output_vat_mapping_is_needed_when_no_vat_is_charged() -> None:
    """An administration that only ever invoices reverse-charged or exempt
    supplies never needs one, and demanding it would refuse invoices it can post
    perfectly well.
    """
    h = harness()
    del h.repository.accounts[("vat_output", None)]

    await post(
        h,
        view(
            groups=(
                VatGroup(
                    "btw_icp",
                    TreatmentRole.INTRA_COMMUNITY,
                    None,
                    Decimal("500.00"),
                    Decimal("0.00"),
                ),
            )
        ),
    )

    assert h.ledger.entries


async def test_the_fallback_revenue_row_covers_an_unmapped_treatment() -> None:
    """0040's NULL key. Having one keeps a newly recognised treatment from
    blocking every invoice; an administration that would rather refuse simply
    does not create it.
    """
    fallback = uuid.uuid4()
    h = harness()
    h.repository.accounts.clear()
    h.repository.accounts[("revenue", None)] = fallback
    h.repository.accounts[("vat_output", None)] = VAT_OUT

    await post(h, view())

    assert fallback in {line.account_id for line in lines_of(h)}


async def test_an_exact_treatment_mapping_beats_the_fallback() -> None:
    h = harness()
    h.repository.accounts[("revenue", None)] = uuid.uuid4()

    await post(h, view())

    assert REVENUE_21 in {line.account_id for line in lines_of(h)}


async def test_a_missing_receivable_control_account_is_refused() -> None:
    h = harness(receivable=None)

    with pytest.raises(PostingConfigurationMissing) as caught:
        await h.service.prepare(view(), actor_user_id=USER)

    assert caught.value.purpose == "accounts_receivable"


async def test_no_sales_journal_is_refused() -> None:
    h = harness(journal=None)

    with pytest.raises(NoSalesJournal):
        await h.service.prepare(view(), actor_user_id=USER)


async def test_no_open_period_is_refused_rather_than_relocated() -> None:
    """An invoice dated inside a locked period belongs there. Posting the
    turnover somewhere else would put it in the wrong VAT return.
    """
    h = harness(period=None)

    with pytest.raises(NoOpenPeriod, match="suppletie"):
        await h.service.prepare(view(), actor_user_id=USER)


async def test_nothing_is_posted_when_preparation_fails() -> None:
    """`prepare` runs before the invoice number is allocated, which is the
    reason it exists as its own phase.
    """
    h = harness(journal=None)

    with pytest.raises(NoSalesJournal):
        await h.service.prepare(view(), actor_user_id=USER)

    assert h.ledger.entries == []
    assert h.documents.uploaded == []


# --- FR-TPL-017 --------------------------------------------------------------


async def test_the_pdf_is_stored_and_linked_to_the_posting() -> None:
    h = harness()
    result = await post(h, view())

    assert len(h.documents.uploaded) == 1
    assert h.documents.uploaded[0].startswith(b"%PDF-")
    assert h.documents.links == [(result.document.id, result.entry.id)]


async def test_the_invoice_records_both_links() -> None:
    h = harness()
    v = view()
    result = await post(h, v)

    assert h.repository.posted[v.invoice.id] == (result.entry.id, result.document.id)


async def test_the_stored_filename_carries_the_invoice_reference() -> None:
    """A person saving three invoices wants to tell them apart in a folder."""
    h = harness()
    await post(h, view())

    assert h.documents.filenames[0] == "2026-1.pdf"


async def test_the_same_invoice_renders_to_the_same_bytes() -> None:
    """FR-TPL-017 stores the rendering once. This is the property that makes
    the stored bytes a fact about the INVOICE rather than about the moment -
    and it is what lets the archive's content hash mean anything.
    """
    first = harness()
    second = harness()
    v = view()

    await post(first, v)
    await post(second, v)

    assert first.documents.uploaded[0] == second.documents.uploaded[0]


# --- the entry's own fields --------------------------------------------------


async def test_the_entry_is_keyed_on_the_invoice_for_idempotency() -> None:
    """NFR-032. A retry posts once, and 0040's unique index catches the case
    where two requests get past this at the same moment.
    """
    h = harness()
    v = view()
    await post(h, v)

    assert h.ledger.entries[0].idempotency_key == f"sales_invoice:{v.invoice.id}"  # type: ignore[attr-defined]


async def test_the_entry_names_the_invoice_reference_and_the_issuer() -> None:
    h = harness()
    await post(h, view())

    entry = h.ledger.entries[0]
    assert entry.document_reference == "2026-1"  # type: ignore[attr-defined]
    assert entry.posted_by_user_id == USER  # type: ignore[attr-defined]
    assert entry.source_system == "invoicing"  # type: ignore[attr-defined]


async def test_the_entry_is_dated_the_invoice_date() -> None:
    """Revenue is recognised when the invoice is raised, not when it is
    entered - and the VAT return reads the entry date.
    """
    h = harness()
    await post(h, view())

    assert h.ledger.entries[0].entry_date == date(2026, 9, 9)  # type: ignore[attr-defined]


# --- the permission coincidence ----------------------------------------------


def test_every_role_that_may_send_an_invoice_may_also_upload_a_document() -> None:
    """The PDF is stored through `DocumentService.upload`, which authorises as
    `upload document`. That works only because every role holding
    `send sales_invoice` also holds `upload document` - which is true today by
    coincidence rather than by design, so it is checked rather than relied on.

    If this ever fails, the fix is NOT to bypass the authorization check: it is
    to decide, deliberately, whether that role should be able to issue at all.
    """
    from api.authz.matrix import ROLES, permissions_for_role

    checked = 0
    for role in ROLES:
        permissions = set(permissions_for_role(role))
        if ("send", "sales_invoice") not in permissions:
            continue
        checked += 1
        assert ("upload", "document") in permissions, (
            f"{role.name} may send an invoice but may not upload a document, so "
            f"storing the rendered PDF (FR-TPL-017) would be refused for them"
        )

    # Without this the loop passes vacuously if the permission key ever changes
    # shape - which is exactly when the check is most needed.
    assert checked >= 4, (
        f"only {checked} roles can send an invoice; expected at least Owner, "
        f"Accountant, Bookkeeper and Invoicer"
    )

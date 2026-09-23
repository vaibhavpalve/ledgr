"""MOB-005's View tab list - `InvoicingService.list_invoices`.

A narrow unit test against a minimal fake repository: `list_invoices` calls
only `_require` (authorization) and `repository.list_invoices`, so the fake
here implements just that method rather than the whole `InvoiceRepository`
protocol - the same "smallest fake that exercises the real code path" the
expense form's tests already use.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.list_status import InvoiceListFacts
from api.invoicing.model import InvoiceStatus, NotAuthorizedToInvoice, SalesInvoice
from api.invoicing.service import InvoicingService
from api.invoicing.vat import InvoiceLineAmounts
from api.vat.rules import TreatmentRole
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository


def _invoice(
    administration_id: uuid.UUID, organization_id: uuid.UUID, **overrides: object
) -> SalesInvoice:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=organization_id,
        administration_id=administration_id,
        fiscal_year_id=uuid.uuid4(),
        status=InvoiceStatus.DRAFT,
        invoice_number=None,
        number_prefix=None,
        invoice_reference=None,
        invoice_date=date(2026, 1, 1),
        supply_date=None,
        due_date=None,
        customer_name="Acme B.V.",
        customer_address="Damrak 1",
        customer_country="NL",
        customer_vat_number=None,
        customer_id=None,
        customer_language=Language.NL,
        credits_invoice_id=None,
        notes=None,
        issued_at=None,
    )
    defaults.update(overrides)
    return SalesInvoice(**defaults)  # type: ignore[arg-type]


@dataclass
class FakeListRepository:
    invoices: list[SalesInvoice] = field(default_factory=list)
    #: Keyed by administration_id, for the denied-request audit path, which
    #: reads it before recording (`_require` -> `_record` -> `_organization_of`).
    organizations: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    async def list_invoices(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> list[SalesInvoice]:
        matching = [i for i in self.invoices if i.administration_id == administration_id]
        return matching[:limit]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organizations.get(administration_id)

    #: invoice id -> its lines' (treatment, role, net), for `gross_amounts`.
    lines: dict[uuid.UUID, list[InvoiceLineAmounts]] = field(default_factory=dict)
    rates: dict[str, Decimal | None] = field(default_factory=dict)
    facts: dict[uuid.UUID, InvoiceListFacts] = field(default_factory=dict)

    async def list_facts(
        self, *, administration_id: uuid.UUID, invoice_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, InvoiceListFacts]:
        return {i: self.facts[i] for i in invoice_ids if i in self.facts}

    async def line_amounts_for(
        self, *, administration_id: uuid.UUID, invoice_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[InvoiceLineAmounts]]:
        return {i: self.lines[i] for i in invoice_ids if i in self.lines}

    async def rates_on(
        self, *, treatments: Sequence[str], on_date: date
    ) -> dict[str, Decimal | None]:
        return {t: self.rates.get(t) for t in treatments}


def _service(
    repository: FakeListRepository, *, role: str = "Bookkeeper"
) -> tuple[InvoicingService, uuid.UUID, uuid.UUID]:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository.organizations[world.acme_books] = world.acme
    service = InvoicingService(
        repository=repository,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(InMemoryAuditRepository()),
        customers=None,  # type: ignore[arg-type]
        posting=None,  # type: ignore[arg-type]
    )
    return service, world.user, world.acme_books


async def test_list_invoices_is_scoped_to_the_administration() -> None:
    repository = FakeListRepository()
    service, user, administration = _service(repository)

    mine = _invoice(administration, uuid.uuid4())
    elsewhere = _invoice(uuid.uuid4(), uuid.uuid4())
    repository.invoices.extend([mine, elsewhere])

    result = await service.list_invoices(
        administration_id=administration, actor_user_id=user, limit=50
    )

    assert [invoice.id for invoice in result] == [mine.id]


async def test_list_invoices_includes_drafts() -> None:
    """MOB-005: a half-finished mobile-created invoice must be resumable."""
    repository = FakeListRepository()
    service, user, administration = _service(repository)

    draft = _invoice(administration, uuid.uuid4(), status=InvoiceStatus.DRAFT)
    issued = _invoice(administration, uuid.uuid4(), status=InvoiceStatus.ISSUED, invoice_number=1)
    repository.invoices.extend([draft, issued])

    result = await service.list_invoices(
        administration_id=administration, actor_user_id=user, limit=50
    )

    assert {invoice.status for invoice in result} == {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED}


async def test_gross_amount_is_net_plus_vat_per_invoice() -> None:
    """The list shows the same total the detail screen does: 100.00 + 21% = 121.00,
    two lines in one treatment summed before VAT is rounded, an invoice with no
    lines is 0.00, and a rate the ruleset lacks gives None rather than a guess.
    """
    repository = FakeListRepository(rates={"NL_STANDARD": Decimal("21"), "NL_GAP": None})
    service, _, administration = _service(repository)
    organization = uuid.uuid4()
    priced = _invoice(administration, organization)
    empty = _invoice(administration, organization)
    unrated = _invoice(administration, organization)
    repository.lines[priced.id] = [
        InvoiceLineAmounts(
            treatment="NL_STANDARD", role=TreatmentRole.STANDARD, net=Decimal("60.00")
        ),
        InvoiceLineAmounts(
            treatment="NL_STANDARD", role=TreatmentRole.STANDARD, net=Decimal("40.00")
        ),
    ]
    repository.lines[unrated.id] = [
        InvoiceLineAmounts(treatment="NL_GAP", role=TreatmentRole.STANDARD, net=Decimal("10.00")),
    ]

    gross = await service.gross_amounts(
        administration_id=administration, invoices=[priced, empty, unrated]
    )

    assert gross == {priced.id: Decimal("121.00"), empty.id: Decimal("0.00"), unrated.id: None}


async def test_a_role_without_create_sales_invoice_cannot_list() -> None:
    repository = FakeListRepository()
    service, user, administration = _service(repository, role="Expense Submitter")

    with pytest.raises(NotAuthorizedToInvoice):
        await service.list_invoices(administration_id=administration, actor_user_id=user, limit=50)

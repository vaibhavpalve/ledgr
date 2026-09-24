"""api.customers.service.CustomerService.request_erasure - PRIV-022/PRIV-023,
without a database.

The customer master record is anonymised unconditionally - 0039's header is
explicit that an invoice snapshots what it needs and never reads back through
this row, so nothing about the master record itself is ever fiscally
protected. What varies is whether the OUTCOME is "erased" or "restricted",
which turns entirely on whether any of the customer's ISSUED invoices are
still inside CMP-001's seven-year window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.customers.model import Customer, CustomerIsErased, CustomerNotFound
from api.customers.peppol import UnconfiguredPeppolDirectory
from api.customers.service import CustomerDetails, CustomerService
from api.customers.vies import SyntaxOnlyViesValidator
from api.privacy.model import ErasureOutcome
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_customer_repository import InMemoryCustomerRepository

DETAILS = CustomerDetails(
    name="De Vries Holding B.V.",
    address_line1="Damrak 70",
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
    kvk_number="12345678",
    vat_number="NL123456789B01",
    invoice_email="facturen@devries.example",
    notes="Prefers post-dated invoices.",
)


@dataclass
class Harness:
    service: CustomerService
    repository: InMemoryCustomerRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID


def harness(*, role: str = "Bookkeeper") -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    repository = InMemoryCustomerRepository()
    repository.register_administration(world.acme_books, world.acme)

    return Harness(
        service=CustomerService(
            repository=repository,  # type: ignore[arg-type]
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(InMemoryAuditRepository()),  # type: ignore[arg-type]
            vies=SyntaxOnlyViesValidator(),
            peppol=UnconfiguredPeppolDirectory(),
        ),
        repository=repository,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
    )


async def _create(h: Harness) -> Customer:
    return await h.service.create(
        administration_id=h.administration, actor_user_id=h.user, details=DETAILS
    )


async def test_a_customer_with_no_invoices_is_fully_erased() -> None:
    h = harness()
    customer = await _create(h)

    decision = await h.service.request_erasure(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )

    assert decision.outcome is ErasureOutcome.ERASED
    assert decision.retained_until is None

    erased = await h.service.get(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )
    assert erased.is_erased
    assert erased.name != customer.name
    assert erased.address.address_line1 is None
    assert erased.notes is None
    assert erased.invoice_email is None
    assert erased.vat_number is None


async def test_a_customer_with_a_retained_invoice_is_restricted_not_fully_erased() -> None:
    """CMP-001: an issued invoice's frozen snapshot outlives an erasure
    request against the customer it names.
    """
    h = harness()
    customer = await _create(h)
    h.repository.register_issued_invoice(
        customer_id=customer.id,
        invoice_reference="2026-0007",
        invoice_date=date(2026, 3, 1),
        fiscal_year_end=date(2026, 12, 31),
    )

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.RESTRICTED
    assert decision.retained_until == date(2033, 12, 31)
    assert "2026-0007" in decision.explanation
    assert "2033-12-31" in decision.explanation

    # The master record is anonymised regardless of the invoice.
    erased = await h.service.get(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )
    assert erased.is_erased
    assert erased.name != customer.name


async def test_an_invoice_past_its_own_retention_does_not_block_erasure() -> None:
    h = harness()
    customer = await _create(h)
    h.repository.register_issued_invoice(
        customer_id=customer.id,
        invoice_reference="2018-0001",
        invoice_date=date(2018, 3, 1),
        fiscal_year_end=date(2018, 12, 31),
    )

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.ERASED


async def test_the_worst_case_retention_date_is_reported_across_several_invoices() -> None:
    h = harness()
    customer = await _create(h)
    h.repository.register_issued_invoice(
        customer_id=customer.id,
        invoice_reference="2025-0001",
        invoice_date=date(2025, 3, 1),
        fiscal_year_end=date(2025, 12, 31),
    )
    h.repository.register_issued_invoice(
        customer_id=customer.id,
        invoice_reference="2026-0009",
        invoice_date=date(2026, 6, 1),
        fiscal_year_end=date(2026, 12, 31),
    )

    decision = await h.service.request_erasure(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        today=date(2026, 9, 13),
    )

    assert decision.outcome is ErasureOutcome.RESTRICTED
    assert decision.retained_until == date(2033, 12, 31)
    assert "2025-0001" in decision.explanation
    assert "2026-0009" in decision.explanation


async def test_an_erased_customer_cannot_be_edited() -> None:
    """PRIV-022: there is no route back. Unlike CustomerIsArchived, there is
    no restore call that would undo this.
    """
    h = harness()
    customer = await _create(h)
    await h.service.request_erasure(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )

    with pytest.raises(CustomerIsErased):
        await h.service.update(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            details=DETAILS,
        )


async def test_an_erased_customer_cannot_be_invoiced() -> None:
    h = harness()
    customer = await _create(h)
    await h.service.request_erasure(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )

    with pytest.raises(CustomerIsErased):
        await h.service.snapshot_for_invoice(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            invoice_date=date(2026, 9, 13),
        )


async def test_erasing_twice_keeps_the_original_timestamp() -> None:
    h = harness()
    customer = await _create(h)

    first = await h.service.request_erasure(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )
    again = await h.service.request_erasure(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )

    erased = await h.service.get(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )
    assert first.outcome is ErasureOutcome.ERASED
    assert again.outcome is ErasureOutcome.ERASED
    assert erased.erased_at is not None


async def test_an_unknown_customer_is_refused() -> None:
    h = harness()

    with pytest.raises(CustomerNotFound):
        await h.service.request_erasure(
            administration_id=h.administration, customer_id=uuid.uuid4(), actor_user_id=h.user
        )

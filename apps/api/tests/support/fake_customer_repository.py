"""An in-memory CustomerRepository that REIMPLEMENTS migration 0039's rules.

The same bargain `tests/support/fake_vat_rules_repository.py` makes with 0028.
A fake that simply stored whatever it was handed would let the service tests
prove the service calls the repository and nothing about the properties that
matter - so the constraints with consequences are enforced here too:

  * `customer_vat_verdict_is_dated` and `customer_vat_verdict_needs_a_number`
  * clearing or changing the VAT number resets the verdict (the CASE ladder in
    SqlCustomerRepository.update)
  * archiving twice keeps the ORIGINAL timestamp
  * there is no delete - 0039 grants none

`tests/integration/test_customer_master.py` runs the equivalent assertions
against Postgres, so the two implementations are checked against each other
rather than only against the tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

from api.customers.address import PostalAddress
from api.customers.model import Customer, RetainedInvoiceRef
from api.customers.service import CustomerDetails
from api.customers.vies import ViesResult, ViesStatus
from api.documents.retention import RetentionBasis
from api.documents.retention import retention_until as _document_retention_until


@dataclass
class InMemoryCustomerRepository:
    """Mirrors 0039's `customer`. Field names match the columns."""

    customers: dict[uuid.UUID, Customer] = field(default_factory=dict)
    #: administration_id -> organization_id, as `administration` would answer.
    organizations: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    #: customer_id -> issued invoices, as `retained_invoices` reads them.
    #: (invoice_id, invoice_reference, invoice_date, fiscal_year_end)
    issued_invoices: dict[uuid.UUID, list[tuple[uuid.UUID, str, date, date]]] = field(
        default_factory=dict
    )
    erasure_requests: list[dict[str, object]] = field(default_factory=list)

    def register_administration(
        self, administration_id: uuid.UUID, organization_id: uuid.UUID
    ) -> None:
        self.organizations[administration_id] = organization_id

    def register_issued_invoice(
        self,
        *,
        customer_id: uuid.UUID,
        invoice_reference: str,
        invoice_date: date,
        fiscal_year_end: date,
        invoice_id: uuid.UUID | None = None,
    ) -> None:
        """Stands in for a row of migration 0037's `sales_invoice`, joined to
        its fiscal year - just enough for `retained_invoices` to compute the
        same `documents.retention_until` a real join would.
        """
        self.issued_invoices.setdefault(customer_id, []).append(
            (invoice_id or uuid.uuid4(), invoice_reference, invoice_date, fiscal_year_end)
        )

    # -- CustomerRepository --------------------------------------------------

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        details: CustomerDetails,
        user_id: uuid.UUID,
    ) -> Customer:
        now = datetime.now(UTC)
        customer = Customer(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            name=details.name,
            trade_name=details.trade_name,
            address=PostalAddress(
                address_line1=details.address_line1,
                address_line2=details.address_line2,
                postal_code=details.postal_code,
                city=details.city,
                country=details.country,
            ),
            kvk_number=details.kvk_number,
            vat_number=details.vat_number,
            # 0039's default. A new customer has been checked by nobody, which
            # is not the same fact as having been rejected.
            vat_number_status=ViesStatus.UNCHECKED,
            vat_number_checked_at=None,
            vat_number_checked_name=None,
            vat_number_consultation_number=None,
            peppol_participant_id=details.peppol_participant_id,
            peppol_checked_at=None,
            payment_terms_days=details.payment_terms_days,
            credit_limit=details.credit_limit,
            delivery_channel=details.delivery_channel,
            invoice_email=details.invoice_email,
            language=details.language,
            notes=details.notes,
            archived_at=None,
            erased_at=None,
            created_at=now,
            updated_at=now,
        )
        self._assert_constraints(customer)
        self.customers[customer.id] = customer
        return customer

    async def get(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> Customer | None:
        customer = self.customers.get(customer_id)
        # RLS, in memory: a row of another administration is not visible, and
        # is indistinguishable from one that does not exist.
        if customer is None or customer.administration_id != administration_id:
            return None
        return customer

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        details: CustomerDetails,
    ) -> Customer:
        existing = self.customers[customer_id]

        # The CASE ladder in SqlCustomerRepository.update: a changed or cleared
        # number takes its verdict with it, or the row would carry an answer
        # about a number that is no longer there.
        number_changed = existing.vat_number != details.vat_number
        keeps_verdict = details.vat_number is not None and not number_changed

        updated = replace(
            existing,
            name=details.name,
            trade_name=details.trade_name,
            address=PostalAddress(
                address_line1=details.address_line1,
                address_line2=details.address_line2,
                postal_code=details.postal_code,
                city=details.city,
                country=details.country,
            ),
            kvk_number=details.kvk_number,
            vat_number=details.vat_number,
            vat_number_status=(
                existing.vat_number_status if keeps_verdict else ViesStatus.UNCHECKED
            ),
            vat_number_checked_at=existing.vat_number_checked_at if keeps_verdict else None,
            vat_number_checked_name=existing.vat_number_checked_name if keeps_verdict else None,
            vat_number_consultation_number=(
                existing.vat_number_consultation_number if keeps_verdict else None
            ),
            peppol_participant_id=details.peppol_participant_id,
            payment_terms_days=details.payment_terms_days,
            credit_limit=details.credit_limit,
            delivery_channel=details.delivery_channel,
            invoice_email=details.invoice_email,
            language=details.language,
            notes=details.notes,
            # 0039's customer_touch_updated_at trigger.
            updated_at=datetime.now(UTC),
        )
        self._assert_constraints(updated)
        self.customers[customer_id] = updated
        return updated

    async def search(
        self,
        *,
        administration_id: uuid.UUID,
        query: str | None,
        include_archived: bool,
        limit: int,
    ) -> Sequence[Customer]:
        rows = [
            customer
            for customer in self.customers.values()
            if customer.administration_id == administration_id
            and (include_archived or not customer.is_archived)
        ]

        if query is not None and query.strip():
            needle = query.strip().lower()
            if needle.isdigit():
                rows = [c for c in rows if (c.kvk_number or "").startswith(needle)]
            else:
                rows = [
                    c
                    for c in rows
                    if needle in c.name.lower() or needle in (c.trade_name or "").lower()
                ]
            rows.sort(key=lambda c: (_rank(c, needle), c.name))
        else:
            rows.sort(key=lambda c: c.name)

        return rows[:limit]

    async def set_archived(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, archived: bool
    ) -> Customer:
        existing = self.customers[customer_id]
        updated = replace(
            existing,
            # Idempotent: when trading stopped is a fact, and re-archiving is
            # not a new one.
            archived_at=(existing.archived_at or datetime.now(UTC)) if archived else None,
            updated_at=datetime.now(UTC),
        )
        self.customers[customer_id] = updated
        return updated

    async def record_vies_result(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, result: ViesResult
    ) -> Customer:
        existing = self.customers[customer_id]
        updated = replace(
            existing,
            vat_number_status=result.status,
            # From the RESULT, not from a clock reading taken here: it is the
            # moment the consultation happened, which is the evidence.
            vat_number_checked_at=result.checked_at,
            vat_number_checked_name=result.name,
            vat_number_consultation_number=result.consultation_number,
            updated_at=datetime.now(UTC),
        )
        self._assert_constraints(updated)
        self.customers[customer_id] = updated
        return updated

    async def record_peppol_id(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, participant_id: str | None
    ) -> Customer:
        existing = self.customers[customer_id]
        updated = replace(
            existing,
            peppol_participant_id=participant_id,
            peppol_checked_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.customers[customer_id] = updated
        return updated

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organizations.get(administration_id)

    async def erase(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> Customer:
        """Mirrors SqlCustomerRepository.erase: overwrite the identifying
        columns and set `erased_at`, keeping the original timestamp on a
        second call the way migration 0044's `coalesce` does.
        """
        existing = self.customers[customer_id]
        updated = replace(
            existing,
            name=f"Erased customer {customer_id}",
            trade_name=None,
            address=PostalAddress(
                address_line1=None, address_line2=None, postal_code=None, city=None,
                country=existing.address.country,
            ),
            kvk_number=None,
            vat_number=None,
            vat_number_status=ViesStatus.UNCHECKED,
            vat_number_checked_at=None,
            vat_number_checked_name=None,
            vat_number_consultation_number=None,
            peppol_participant_id=None,
            peppol_checked_at=None,
            invoice_email=None,
            notes=None,
            erased_at=existing.erased_at or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.customers[customer_id] = updated
        return updated

    async def retained_invoices(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, today: date
    ) -> Sequence[RetainedInvoiceRef]:
        return [
            RetainedInvoiceRef(
                invoice_id=invoice_id,
                invoice_reference=reference,
                invoice_date=invoice_date,
                retained_until=retained_until,
            )
            for invoice_id, reference, invoice_date, fiscal_year_end in self.issued_invoices.get(
                customer_id, []
            )
            if (retained_until := _document_retention_until(fiscal_year_end, RetentionBasis.STANDARD))
            >= today
        ]

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
    ) -> None:
        self.erasure_requests.append(
            {
                "organization_id": organization_id,
                "administration_id": administration_id,
                "resource_type": "customer",
                "resource_id": resource_id,
                "requested_by_user_id": requested_by_user_id,
                "decision": decision,
                "explanation": explanation,
                "retained_until": retained_until,
            }
        )

    # -- 0039's CHECK constraints, in memory ---------------------------------

    @staticmethod
    def _assert_constraints(customer: Customer) -> None:
        """Raises the way the database would, so a service change that writes
        an impossible row fails here as well as in Postgres.
        """
        if customer.vat_number_status.is_a_verdict:
            assert customer.vat_number_checked_at is not None, (
                "customer_vat_verdict_is_dated: a verdict without a date is unusable as evidence"
            )
        else:
            assert customer.vat_number_checked_at is None, (
                "customer_vat_verdict_is_dated: an unchecked number carries no date"
            )

        if customer.vat_number is None:
            assert customer.vat_number_status is ViesStatus.UNCHECKED, (
                "customer_vat_verdict_needs_a_number: a verdict about a number "
                "that is not there describes nothing"
            )

        if customer.delivery_channel.value == "email" and customer.erased_at is None:
            assert customer.invoice_email is not None, "customer_email_channel_has_an_address"


def _rank(customer: Customer, needle: str) -> int:
    """SqlCustomerRepository.search's ORDER BY: exact, then prefix, then the
    rest. Reimplemented so a test can assert the ordering is what the SQL
    produces rather than dictionary insertion order.
    """
    name = customer.name.lower()
    trade = (customer.trade_name or "").lower()
    if name == needle or trade == needle:
        return 0
    if name.startswith(needle) or trade.startswith(needle):
        return 1
    if (customer.kvk_number or "").startswith(needle):
        return 1
    return 2

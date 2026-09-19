"""SQL for sales invoicing - migration 0037.

Thin on purpose. Every rule with a consequence lives either in the database
(numbering, immutability, tenant coherence) or in a pure module (`vat`,
`statutory`, `wording`), so this file is statements and row mapping.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n.language import Language
from api.invoicing.duplicates import DuplicateCandidate, InvoiceFingerprint, LineFingerprint
from api.invoicing.model import InvoiceLine, InvoiceStatus, SalesInvoice
from api.invoicing.statutory import SupplierDetails
from api.vat.rules import EffectiveRules, TreatmentRole
from api.vat.rules_repository import SqlVatRulesRepository

_INVOICE_COLUMNS = """
    id, organization_id, administration_id, fiscal_year_id, status,
    invoice_number, number_prefix, invoice_reference, invoice_date, supply_date,
    due_date, customer_name, customer_address, customer_country,
    customer_vat_number, customer_id, customer_language, credits_invoice_id,
    notes, issued_at, journal_entry_id, document_id
"""


def _invoice(row: Any, lines: Sequence[InvoiceLine] = ()) -> SalesInvoice:
    return SalesInvoice(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        fiscal_year_id=row.fiscal_year_id,
        status=InvoiceStatus(row.status),
        invoice_number=int(row.invoice_number) if row.invoice_number is not None else None,
        number_prefix=row.number_prefix,
        invoice_reference=row.invoice_reference,
        invoice_date=row.invoice_date,
        supply_date=row.supply_date,
        due_date=row.due_date,
        customer_name=row.customer_name,
        customer_address=row.customer_address,
        customer_country=row.customer_country,
        customer_vat_number=row.customer_vat_number,
        customer_id=row.customer_id,
        customer_language=Language(row.customer_language),
        credits_invoice_id=row.credits_invoice_id,
        notes=row.notes,
        issued_at=row.issued_at,
        journal_entry_id=row.journal_entry_id,
        document_id=row.document_id,
        lines=tuple(lines),
    )


class SqlInvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
    ) -> SalesInvoice:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO sales_invoice (
                    organization_id, administration_id, fiscal_year_id,
                    invoice_date, supply_date, due_date, customer_name,
                    customer_address, customer_country, customer_vat_number,
                    customer_id, customer_language,
                    notes, created_by_user_id, credits_invoice_id
                ) VALUES (
                    :org, :admin, :year, :invoice_date, :supply_date, :due_date,
                    :name, :address, :country, :vat_number,
                    :customer_id, :customer_language, :notes, :user,
                    :credits
                )
                RETURNING {_INVOICE_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "year": str(fiscal_year_id),
                "invoice_date": invoice_date,
                "supply_date": supply_date,
                "due_date": due_date,
                "name": customer_name,
                "address": customer_address,
                "country": customer_country,
                "vat_number": customer_vat_number,
                "customer_id": str(customer_id) if customer_id else None,
                "customer_language": customer_language.value,
                "notes": notes,
                "user": str(user_id),
                "credits": str(credits_invoice_id) if credits_invoice_id else None,
            },
        )
        return _invoice(result.one())

    async def get(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice | None:
        result = await self._session.execute(
            text(
                f"SELECT {_INVOICE_COLUMNS} FROM sales_invoice "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        return _invoice(row, await self._lines(invoice_id))

    async def _lines(self, invoice_id: uuid.UUID) -> Sequence[InvoiceLine]:
        result = await self._session.execute(
            text(
                """
                SELECT l.id, l.position, l.description, l.quantity, l.unit_price,
                       l.discount_percent, l.vat_treatment, l.line_net, t.role
                  FROM sales_invoice_line l
                  JOIN vat_treatment t ON t.code = l.vat_treatment
                 WHERE l.invoice_id = :id
                 ORDER BY l.position
                """
            ),
            {"id": str(invoice_id)},
        )
        return [
            InvoiceLine(
                id=row.id,
                position=row.position,
                description=row.description,
                quantity=row.quantity,
                unit_price=row.unit_price,
                discount_percent=row.discount_percent,
                vat_treatment=row.vat_treatment,
                role=TreatmentRole(row.role),
                line_net=row.line_net,
            )
            for row in result
        ]

    async def replace_lines(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, lines: Sequence[Any]
    ) -> Sequence[InvoiceLine]:
        """Replace wholesale rather than diff.

        An invoice's lines are edited as a block on one screen, and a diff
        would have to decide what a moved line is. 0037 refuses this entirely
        once the invoice is issued, so the destructive shape is safe where it
        is reachable at all.
        """
        await self._session.execute(
            text("DELETE FROM sales_invoice_line WHERE invoice_id = :id"),
            {"id": str(invoice_id)},
        )
        for position, line in enumerate(lines, start=1):
            await self._session.execute(
                text(
                    """
                    INSERT INTO sales_invoice_line (
                        organization_id, administration_id, invoice_id, position,
                        description, quantity, unit_price, discount_percent,
                        vat_treatment
                    ) VALUES (
                        -- organization_id is overwritten by
                        -- sales_invoice_line_same_tenant(); the placeholder
                        -- exists only because the column is NOT NULL.
                        (SELECT organization_id FROM sales_invoice WHERE id = :invoice),
                        :admin, :invoice, :position, :description, :quantity,
                        :unit_price, :discount, :treatment
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "invoice": str(invoice_id),
                    "position": position,
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "discount": line.discount_percent,
                    "treatment": line.vat_treatment,
                },
            )
        return await self._lines(invoice_id)

    async def mark_issued(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice:
        """The transition. The NUMBER is allocated by 0037's trigger, not here.

        `status` is the only column this statement sets: FR-AR-004's number and
        `issued_at` are both derived in `sales_invoice_allocate_number()`, so a
        second write path could not get them wrong either - the argument
        `journal_entry_validate` makes for entry_number.
        """
        result = await self._session.execute(
            text(
                f"""
                UPDATE sales_invoice SET status = 'issued'
                 WHERE id = :id AND administration_id = :admin AND status = 'draft'
                RETURNING {_INVOICE_COLUMNS}
                """
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )
        return _invoice(result.one(), await self._lines(invoice_id))

    async def write_vat_totals(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID, groups: Sequence[Any]
    ) -> None:
        for group in groups:
            await self._session.execute(
                text(
                    """
                    INSERT INTO sales_invoice_vat_total (
                        invoice_id, organization_id, administration_id,
                        vat_treatment, rate, taxable_amount, vat_amount
                    ) VALUES (
                        :invoice,
                        (SELECT organization_id FROM sales_invoice WHERE id = :invoice),
                        :admin, :treatment, :rate, :taxable, :vat
                    )
                    """
                ),
                {
                    "invoice": str(invoice_id),
                    "admin": str(administration_id),
                    "treatment": group.treatment,
                    "rate": group.rate,
                    "taxable": group.taxable,
                    "vat": group.vat,
                },
            )

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None:
        result = await self._session.execute(
            text(
                "SELECT legal_name, kvk_number, vat_number, address_line1, "
                "       address_line2, postal_code, city, country, iban "
                "  FROM administration WHERE id = :id"
            ),
            {"id": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        return SupplierDetails(
            legal_name=row.legal_name,
            address_line1=row.address_line1,
            address_line2=row.address_line2,
            postal_code=row.postal_code,
            city=row.city,
            country=row.country,
            vat_number=row.vat_number,
            kvk_number=row.kvk_number,
            iban=row.iban,
        )

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def rates_on(
        self, *, treatments: Sequence[str], on_date: date
    ) -> dict[str, Decimal | None]:
        """The rate in force on the invoice date, per treatment (CMP-014)."""
        rates: dict[str, Decimal | None] = {}
        for treatment in treatments:
            result = await self._session.execute(
                text("SELECT vat.rate_on(:treatment, :on_date) AS rate"),
                {"treatment": treatment, "on_date": on_date},
            )
            row = result.first()
            rates[treatment] = None if row is None else row.rate
        return rates

    async def roles_for(self, *, treatments: Sequence[str]) -> dict[str, TreatmentRole]:
        result = await self._session.execute(
            text("SELECT code, role FROM vat_treatment WHERE code = ANY(:codes)"),
            {"codes": list(treatments)},
        )
        return {row.code: TreatmentRole(row.role) for row in result}

    async def credit_note_for(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM sales_invoice "
                "WHERE credits_invoice_id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.id

    async def delete_draft(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID) -> None:
        await self._session.execute(
            text(
                "DELETE FROM sales_invoice "
                "WHERE id = :id AND administration_id = :admin AND status = 'draft'"
            ),
            {"id": str(invoice_id), "admin": str(administration_id)},
        )

    async def effective_rules_on(self, *, on_date: date) -> EffectiveRules:
        """SI-13. Delegates to the VAT rules repository rather than repeating
        its three queries: one reader of `vat.rules_on`, so the preview and the
        return builder cannot come to disagree about what it returns."""
        return await SqlVatRulesRepository(self._session).rules_on(on_date=on_date)

    async def duplicate_candidates(
        self,
        *,
        administration_id: uuid.UUID,
        exclude_invoice_id: uuid.UUID,
        customer_id: uuid.UUID | None,
        customer_name: str,
        since: date,
        until: date,
    ) -> Sequence[DuplicateCandidate]:
        """SI-12's pool: this administration's invoices dated in [since, until]
        for the same customer, credit notes excluded.

        An OVER-approximation on purpose. The customer is matched here by id or
        by a trimmed, case-insensitive name, which is looser than nothing and
        narrower than `duplicates.normalise_name` (it does not strip edge
        punctuation) - so a name differing only by a trailing full stop is not
        fetched. `find_duplicates` then applies the exact rule to what came
        back. The window and the administration predicate use
        `sales_invoice_administration_idx`; the LIMIT bounds a customer billed
        many times in a month.
        """
        result = await self._session.execute(
            text(
                """
                SELECT id, status, invoice_reference, invoice_date,
                       customer_id, customer_name
                  FROM sales_invoice
                 WHERE administration_id = :admin
                   AND id <> :exclude
                   AND credits_invoice_id IS NULL
                   AND invoice_date BETWEEN :since AND :until
                   AND (
                        (CAST(:customer_id AS uuid) IS NOT NULL
                         AND customer_id = CAST(:customer_id AS uuid))
                     OR lower(btrim(customer_name)) = lower(btrim(:name))
                   )
                 ORDER BY invoice_date DESC
                 LIMIT 50
                """
            ),
            {
                "admin": str(administration_id),
                "exclude": str(exclude_invoice_id),
                "customer_id": str(customer_id) if customer_id else None,
                "name": customer_name,
                "since": since,
                "until": until,
            },
        )
        rows = result.all()
        if not rows:
            return []

        lines_by_invoice: dict[uuid.UUID, list[LineFingerprint]] = {}
        net_by_invoice: dict[uuid.UUID, Decimal] = {}
        line_rows = await self._session.execute(
            text(
                """
                SELECT invoice_id, description, quantity, unit_price,
                       discount_percent, line_net
                  FROM sales_invoice_line
                 WHERE invoice_id = ANY(CAST(:ids AS uuid[])) AND administration_id = :admin
                """
            ),
            {"ids": [str(row.id) for row in rows], "admin": str(administration_id)},
        )
        for line in line_rows:
            lines_by_invoice.setdefault(line.invoice_id, []).append(
                LineFingerprint(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    discount_percent=line.discount_percent,
                )
            )
            net_by_invoice[line.invoice_id] = (
                net_by_invoice.get(line.invoice_id, Decimal(0)) + line.line_net
            )

        return [
            DuplicateCandidate(
                invoice_id=row.id,
                status=row.status,
                invoice_reference=row.invoice_reference,
                fingerprint=InvoiceFingerprint(
                    customer_id=row.customer_id,
                    customer_name=row.customer_name,
                    invoice_date=row.invoice_date,
                    lines=tuple(lines_by_invoice.get(row.id, ())),
                    net_total=net_by_invoice.get(row.id, Decimal(0)),
                ),
            )
            for row in rows
        ]

    async def list_invoices(
        self, *, administration_id: uuid.UUID, limit: int
    ) -> Sequence[SalesInvoice]:
        """MOB-005's View tab: newest first, bounded, no cursor.

        Lines are not joined here - a list row does not render them, and
        fetching lines for every invoice in a bounded list would be an N+1 the
        single-invoice `get()` above has no reason to pay.
        """
        result = await self._session.execute(
            text(
                f"SELECT {_INVOICE_COLUMNS} FROM sales_invoice "
                f"WHERE administration_id = :admin "
                f"ORDER BY created_at DESC LIMIT :limit"
            ),
            {"admin": str(administration_id), "limit": limit},
        )
        return [_invoice(row) for row in result]

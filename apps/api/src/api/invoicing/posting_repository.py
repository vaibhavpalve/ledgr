"""SQL for sales-invoice posting - migration 0040.

Thin on purpose, the same bargain `api.invoicing.repository` makes with 0037.
Note what is absent: nothing here touches `journal_entry` or `journal_line`.
Those are written by `ledger.post_entry` through `LedgerService`, and
`ledgr_app` holds no grant that would let this file do otherwise (0020).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n.formatting import DEFAULT_FORMATTING_LOCALE
from api.invoicing.statutory import SupplierDetails
from api.templates.model import (
    BlockText,
    ColorScheme,
    ColumnLayout,
    ColumnSetting,
    ContentBlock,
    FontWeight,
    InvoiceTemplate,
    Layout,
    LetterSpacing,
    LineColumn,
    LineHeight,
    Logo,
    LogoPosition,
    LogoSize,
    TypeScale,
    Typography,
)

#: Selected columns for one `invoice_template` row - `api.templates.
#: repository`'s own `_TEMPLATE_COLUMNS`, duplicated rather than imported for
#: the same cross-package reason `supplier()` already gives in this file.
_TEMPLATE_COLUMNS = """
    id, organization_id, administration_id, name, is_default, version, layout,
    logo_asset_id, logo_position, logo_size, heading_font, body_font, figures_font,
    type_scale, font_weight, line_height, letter_spacing,
    accent_color, text_color, background_color,
    updated_by_user_id, created_at, updated_at
"""


class SqlSalesPostingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sales_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        """The administration's one active sales journal.

        Returns None when there is none OR when there is more than one, because
        both mean the same thing to the caller: there is no single journal this
        invoice obviously belongs in. Picking one of two arbitrarily would put
        half a year's invoices in each and split FR-GL-013's numbering series.
        """
        result = await self._session.execute(
            text(
                "SELECT id FROM ledger_journal "
                " WHERE administration_id = :admin "
                "   AND journal_type = 'sales' AND status = 'active'"
            ),
            {"admin": str(administration_id)},
        )
        rows = result.fetchall()
        return rows[0].id if len(rows) == 1 else None

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT id FROM period "
                " WHERE administration_id = :admin "
                "   AND :on BETWEEN start_date AND end_date "
                "   AND status = 'open'"
            ),
            {"admin": str(administration_id), "on": on},
        )
        row = result.first()
        return None if row is None else row.id

    async def receivable_control_account(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        """FR-GL-006's AR control account, found by what it IS rather than by a
        mapping row. `ledger_account_one_control_per_kind_idx` (0020) makes it
        unique per administration, so there is nothing to disambiguate.
        """
        result = await self._session.execute(
            text(
                "SELECT id FROM ledger_account "
                " WHERE administration_id = :admin "
                "   AND control_kind = 'accounts_receivable' AND status = 'active'"
            ),
            {"admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.id

    async def posting_account(
        self, *, administration_id: uuid.UUID, purpose: str, vat_treatment: str
    ) -> uuid.UUID | None:
        """The exact treatment match, else the fallback row.

        One statement rather than two round trips, ordered so the exact match
        wins - the shape `expense_posting_account`'s category lookup uses, with
        a treatment code in place of a category name.
        """
        result = await self._session.execute(
            text(
                "SELECT account_id FROM sales_posting_account "
                " WHERE administration_id = :admin AND purpose = :purpose "
                "   AND (vat_treatment_key = :treatment OR vat_treatment_key IS NULL) "
                " ORDER BY vat_treatment_key NULLS LAST "
                " LIMIT 1"
            ),
            {
                "admin": str(administration_id),
                "purpose": purpose,
                "treatment": vat_treatment,
            },
        )
        row = result.first()
        return None if row is None else row.account_id

    async def customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID | None:
        result = await self._session.execute(
            text(
                "SELECT subledger_party_id FROM customer "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.subledger_party_id

    async def link_customer_party(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, party_id: uuid.UUID
    ) -> None:
        """Set once. `customer_party_link_is_final_trg` (0040) refuses a second
        write, and the WHERE clause makes a concurrent one a no-op rather than
        an exception - two requests racing to post the first invoice for one
        customer should not turn into an error for whichever lost.
        """
        await self._session.execute(
            text(
                "UPDATE customer SET subledger_party_id = :party "
                " WHERE id = :id AND administration_id = :admin "
                "   AND subledger_party_id IS NULL"
            ),
            {"party": str(party_id), "id": str(customer_id), "admin": str(administration_id)},
        )

    async def mark_posted(
        self,
        *,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        entry_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        """The two links and the timestamp, in one statement.

        One statement because 0040's `sales_invoice_draft_is_unposted` and
        `sales_invoice_posted_at_recorded` constrain them together: writing the
        entry id without the timestamp would violate the second at the moment
        between two statements, and there is no instant here at which that is
        true.
        """
        await self._session.execute(
            text(
                "UPDATE sales_invoice "
                "   SET journal_entry_id = :entry, document_id = :document, "
                "       posted_at = now() "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {
                "entry": str(entry_id),
                "document": str(document_id),
                "id": str(invoice_id),
                "admin": str(administration_id),
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

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        """FR-LOC-002: how figures in THESE books are written, for every reader.

        Falls back to the product default rather than raising, because a missing
        locale must not be the thing that stops an invoice being issued - and
        the column is NOT NULL with a default anyway, so the fallback is for the
        administration that does not exist, which the statutory gate has already
        refused.
        """
        result = await self._session.execute(
            text("SELECT formatting_locale FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return DEFAULT_FORMATTING_LOCALE if row is None else str(row.formatting_locale)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def default_invoice_template(
        self, *, administration_id: uuid.UUID
    ) -> InvoiceTemplate | None:
        """FR-TPL-008/017's issue-time template - the `is_default` row for
        this administration, or `None` when it has never saved one
        (`SalesPostingService._store_rendering` maps that to
        `api.invoicing.rendering.default_template`).

        Reads `invoice_template` and its two child tables directly rather
        than through `api.templates.repository.SqlInvoiceTemplateRepository`:
        the same "each package owns its own queries against a shared table"
        posture `supplier()` above already takes, so `api.invoicing` never
        imports `api.templates`' repository or service layer.
        """
        result = await self._session.execute(
            text(
                f"SELECT {_TEMPLATE_COLUMNS} FROM invoice_template "
                "WHERE administration_id = :admin AND is_default = true"
            ),
            {"admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None

        columns_result = await self._session.execute(
            text(
                "SELECT column_key, visible, position FROM invoice_template_column "
                "WHERE template_id = :id ORDER BY position"
            ),
            {"id": str(row.id)},
        )
        columns = ColumnLayout(
            tuple(
                ColumnSetting(
                    column=LineColumn(r.column_key), visible=r.visible, position=r.position
                )
                for r in columns_result
            )
        )

        blocks_result = await self._session.execute(
            text(
                "SELECT block_key, text_nl, text_en FROM invoice_template_block "
                "WHERE template_id = :id ORDER BY block_key"
            ),
            {"id": str(row.id)},
        )
        blocks = tuple(
            BlockText(block=ContentBlock(r.block_key), text_nl=r.text_nl, text_en=r.text_en)
            for r in blocks_result
        )

        return InvoiceTemplate(
            id=row.id,
            organization_id=row.organization_id,
            administration_id=row.administration_id,
            name=row.name,
            is_default=row.is_default,
            version=row.version,
            layout=Layout(row.layout),
            logo=Logo(
                asset_id=row.logo_asset_id,
                position=LogoPosition(row.logo_position),
                size=LogoSize(row.logo_size),
            ),
            typography=Typography(
                heading_font=row.heading_font,
                body_font=row.body_font,
                figures_font=row.figures_font,
                type_scale=TypeScale(row.type_scale),
                font_weight=FontWeight(row.font_weight),
                line_height=LineHeight(row.line_height),
                letter_spacing=LetterSpacing(row.letter_spacing),
            ),
            colors=ColorScheme(
                accent=row.accent_color, text=row.text_color, background=row.background_color
            ),
            columns=columns,
            blocks=blocks,
            updated_by_user_id=row.updated_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

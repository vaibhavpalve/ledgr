"""SQL for invoice templates - migration 0042.

Thin on purpose, mirroring `api.invoicing.repository`: every rule with a
consequence lives either in the database (tenant coherence, the FR-TPL-009
CHECK constraints, the version-bump trigger) or in `api.templates.model` /
`api.templates.compliance`, so this file is statements and row mapping.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
    HeaderArrangement,
    InvoiceTemplate,
    Layout,
    LetterSpacing,
    LineColumn,
    LineHeight,
    Logo,
    LogoPosition,
    LogoSize,
    Margins,
    PageSize,
    TemplateConflict,
    TemplateNameConflict,
    TotalsPosition,
    TypeScale,
    Typography,
)

_TEMPLATE_COLUMNS = """
    id, organization_id, administration_id, name, is_default, version, layout,
    logo_asset_id, logo_position, logo_size, heading_font, body_font, figures_font,
    type_scale, font_weight, line_height, letter_spacing,
    accent_color, text_color, background_color,
    header_arrangement, totals_position, page_size, margins,
    updated_by_user_id, created_at, updated_at
"""


def _template(
    row: Any, columns: Sequence[ColumnSetting], blocks: Sequence[BlockText]
) -> InvoiceTemplate:
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
        columns=ColumnLayout(tuple(columns)),
        blocks=tuple(blocks),
        header_arrangement=HeaderArrangement(row.header_arrangement),
        totals_position=TotalsPosition(row.totals_position),
        page_size=PageSize(row.page_size),
        margins=Margins(row.margins),
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlInvoiceTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        name: str,
        is_default: bool,
        layout: Layout,
        logo: Logo,
        typography: Typography,
        colors: ColorScheme,
        columns: ColumnLayout,
        blocks: Sequence[BlockText],
        header_arrangement: HeaderArrangement = HeaderArrangement.SPLIT,
        totals_position: TotalsPosition = TotalsPosition.RIGHT,
        page_size: PageSize = PageSize.A4,
        margins: Margins = Margins.NORMAL,
        user_id: uuid.UUID,
    ) -> InvoiceTemplate:
        try:
            result = await self._session.execute(
                text(
                    f"""
                    INSERT INTO invoice_template (
                        organization_id, administration_id, name, is_default, layout,
                        logo_asset_id, logo_position, logo_size,
                        heading_font, body_font, figures_font,
                        type_scale, font_weight, line_height, letter_spacing,
                        accent_color, text_color, background_color,
                        header_arrangement, totals_position, page_size, margins,
                        updated_by_user_id
                    ) VALUES (
                        :org, :admin, :name, :is_default, :layout,
                        :logo_asset_id, :logo_position, :logo_size,
                        :heading_font, :body_font, :figures_font,
                        :type_scale, :font_weight, :line_height, :letter_spacing,
                        :accent_color, :text_color, :background_color,
                        :header_arrangement, :totals_position, :page_size, :margins,
                        :user
                    )
                    RETURNING {_TEMPLATE_COLUMNS}
                    """
                ),
                _template_params(
                    organization_id=organization_id,
                    administration_id=administration_id,
                    name=name,
                    is_default=is_default,
                    layout=layout,
                    logo=logo,
                    typography=typography,
                    colors=colors,
                    header_arrangement=header_arrangement,
                    totals_position=totals_position,
                    page_size=page_size,
                    margins=margins,
                    user_id=user_id,
                ),
            )
            row = result.one()
        except IntegrityError as exc:
            raise _conflict_from(exc, administration_id) from exc

        template_id: uuid.UUID = row.id
        await self._replace_columns(
            administration_id=administration_id, template_id=template_id, columns=columns
        )
        await self._replace_blocks(
            administration_id=administration_id, template_id=template_id, blocks=blocks
        )
        return _template(row, columns.settings, tuple(blocks))

    async def get(
        self, *, administration_id: uuid.UUID, template_id: uuid.UUID
    ) -> InvoiceTemplate | None:
        result = await self._session.execute(
            text(
                f"SELECT {_TEMPLATE_COLUMNS} FROM invoice_template "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(template_id), "admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return None
        columns = await self._columns(template_id)
        blocks = await self._blocks(template_id)
        return _template(row, columns, blocks)

    async def list_for_administration(
        self, *, administration_id: uuid.UUID
    ) -> tuple[InvoiceTemplate, ...]:
        result = await self._session.execute(
            text(
                f"SELECT {_TEMPLATE_COLUMNS} FROM invoice_template "
                "WHERE administration_id = :admin ORDER BY is_default DESC, name"
            ),
            {"admin": str(administration_id)},
        )
        templates: list[InvoiceTemplate] = []
        for row in result:
            template_id: uuid.UUID = row.id
            columns = await self._columns(template_id)
            blocks = await self._blocks(template_id)
            templates.append(_template(row, columns, blocks))
        return tuple(templates)

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        expected_version: int,
        name: str,
        is_default: bool,
        layout: Layout,
        logo: Logo,
        typography: Typography,
        colors: ColorScheme,
        columns: ColumnLayout,
        blocks: Sequence[BlockText],
        header_arrangement: HeaderArrangement = HeaderArrangement.SPLIT,
        totals_position: TotalsPosition = TotalsPosition.RIGHT,
        page_size: PageSize = PageSize.A4,
        margins: Margins = Margins.NORMAL,
        user_id: uuid.UUID,
    ) -> InvoiceTemplate | None:
        """Returns None when no row matched `id`, `administration_id` AND
        `expected_version` together - the caller (service layer) already knows
        which of "does not exist" or "somebody else saved first" applies,
        because it read the current row to get `expected_version` from.
        """
        params = _template_params(
            organization_id=None,
            administration_id=administration_id,
            name=name,
            is_default=is_default,
            layout=layout,
            logo=logo,
            typography=typography,
            colors=colors,
            header_arrangement=header_arrangement,
            totals_position=totals_position,
            page_size=page_size,
            margins=margins,
            user_id=user_id,
        )
        params["id"] = str(template_id)
        params["expected_version"] = expected_version

        try:
            result = await self._session.execute(
                text(
                    f"""
                    UPDATE invoice_template SET
                        name = :name, is_default = :is_default, layout = :layout,
                        logo_asset_id = :logo_asset_id, logo_position = :logo_position,
                        logo_size = :logo_size,
                        heading_font = :heading_font, body_font = :body_font,
                        figures_font = :figures_font,
                        type_scale = :type_scale, font_weight = :font_weight,
                        line_height = :line_height, letter_spacing = :letter_spacing,
                        accent_color = :accent_color, text_color = :text_color,
                        background_color = :background_color,
                        header_arrangement = :header_arrangement,
                        totals_position = :totals_position,
                        page_size = :page_size,
                        margins = :margins,
                        updated_by_user_id = :user
                    WHERE id = :id AND administration_id = :admin AND version = :expected_version
                    RETURNING {_TEMPLATE_COLUMNS}
                    """
                ),
                params,
            )
            row = result.first()
        except IntegrityError as exc:
            raise _conflict_from(exc, administration_id) from exc

        if row is None:
            return None

        await self._replace_columns(
            administration_id=administration_id, template_id=template_id, columns=columns
        )
        await self._replace_blocks(
            administration_id=administration_id, template_id=template_id, blocks=blocks
        )
        return _template(row, columns.settings, tuple(blocks))

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def supplier(self, *, administration_id: uuid.UUID) -> SupplierDetails | None:
        """The same query `api.invoicing.repository.SqlInvoiceRepository.
        supplier` runs, duplicated rather than shared: the two packages
        depend on `administration`'s shape independently, and a template
        preview reading through the invoicing package would be exactly the
        cross-package coupling CLAUDE.md's bounded-context rule warns against
        for the ledger, applied here to keep `api.templates` free-standing.
        """
        result = await self._session.execute(
            text(
                "SELECT legal_name, kvk_number, vat_number, address_line1, "
                "       address_line2, postal_code, city, country "
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
        )

    async def formatting_locale(self, *, administration_id: uuid.UUID) -> str:
        """FR-LOC-002, the same query and the same product-default fallback
        `api.invoicing.posting_repository.SqlSalesPostingRepository.
        formatting_locale` uses - duplicated across the package boundary
        rather than shared, per this module's own docstring.
        """
        result = await self._session.execute(
            text("SELECT formatting_locale FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return DEFAULT_FORMATTING_LOCALE if row is None else str(row.formatting_locale)

    # -- child rows -----------------------------------------------------------

    async def _replace_columns(
        self, *, administration_id: uuid.UUID, template_id: uuid.UUID, columns: ColumnLayout
    ) -> None:
        await self._session.execute(
            text("DELETE FROM invoice_template_column WHERE template_id = :id"),
            {"id": str(template_id)},
        )
        for setting in columns.settings:
            await self._session.execute(
                text(
                    """
                    INSERT INTO invoice_template_column (
                        organization_id, administration_id, template_id,
                        column_key, visible, position
                    ) VALUES (
                        -- organization_id is overwritten by
                        -- invoice_template_child_same_tenant(); the
                        -- placeholder exists only because the column is NOT
                        -- NULL - the same convention 0037's replace_lines uses.
                        (SELECT organization_id FROM invoice_template WHERE id = :template),
                        :admin, :template, :column_key, :visible, :position
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "template": str(template_id),
                    "column_key": setting.column.value,
                    "visible": setting.visible,
                    "position": setting.position,
                },
            )

    async def _columns(self, template_id: uuid.UUID) -> tuple[ColumnSetting, ...]:
        result = await self._session.execute(
            text(
                "SELECT column_key, visible, position FROM invoice_template_column "
                "WHERE template_id = :id ORDER BY position"
            ),
            {"id": str(template_id)},
        )
        return tuple(
            ColumnSetting(
                column=LineColumn(row.column_key), visible=row.visible, position=row.position
            )
            for row in result
        )

    async def _replace_blocks(
        self,
        *,
        administration_id: uuid.UUID,
        template_id: uuid.UUID,
        blocks: Sequence[BlockText],
    ) -> None:
        await self._session.execute(
            text("DELETE FROM invoice_template_block WHERE template_id = :id"),
            {"id": str(template_id)},
        )
        for block in blocks:
            await self._session.execute(
                text(
                    """
                    INSERT INTO invoice_template_block (
                        organization_id, administration_id, template_id,
                        block_key, text_nl, text_en
                    ) VALUES (
                        (SELECT organization_id FROM invoice_template WHERE id = :template),
                        :admin, :template, :block_key, :text_nl, :text_en
                    )
                    """
                ),
                {
                    "admin": str(administration_id),
                    "template": str(template_id),
                    "block_key": block.block.value,
                    "text_nl": block.text_nl,
                    "text_en": block.text_en,
                },
            )

    async def _blocks(self, template_id: uuid.UUID) -> tuple[BlockText, ...]:
        result = await self._session.execute(
            text(
                "SELECT block_key, text_nl, text_en FROM invoice_template_block "
                "WHERE template_id = :id ORDER BY block_key"
            ),
            {"id": str(template_id)},
        )
        return tuple(
            BlockText(block=ContentBlock(row.block_key), text_nl=row.text_nl, text_en=row.text_en)
            for row in result
        )


def _template_params(
    *,
    organization_id: uuid.UUID | None,
    administration_id: uuid.UUID,
    name: str,
    is_default: bool,
    layout: Layout,
    logo: Logo,
    typography: Typography,
    colors: ColorScheme,
    header_arrangement: HeaderArrangement,
    totals_position: TotalsPosition,
    page_size: PageSize,
    margins: Margins,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "admin": str(administration_id),
        "name": name,
        "is_default": is_default,
        "layout": layout.value,
        "logo_asset_id": str(logo.asset_id) if logo.asset_id else None,
        "logo_position": logo.position.value,
        "logo_size": logo.size.value,
        "heading_font": typography.heading_font,
        "body_font": typography.body_font,
        "figures_font": typography.figures_font,
        "type_scale": typography.type_scale.value,
        "font_weight": typography.font_weight.value,
        "line_height": typography.line_height.value,
        "letter_spacing": typography.letter_spacing.value,
        "accent_color": colors.accent,
        "text_color": colors.text,
        "background_color": colors.background,
        "header_arrangement": header_arrangement.value,
        "totals_position": totals_position.value,
        "page_size": page_size.value,
        "margins": margins.value,
        "user": str(user_id),
    }
    if organization_id is not None:
        params["org"] = str(organization_id)
    return params


def _conflict_from(
    exc: IntegrityError, administration_id: uuid.UUID
) -> TemplateConflict | TemplateNameConflict:
    """Two constraints can raise an `IntegrityError` on `invoice_template`
    now: 0042's partial unique index (FR-TPL-011, at most one default per
    administration) and 0043's `invoice_template_name_unique` (FR-TPL-019's
    duplicate needs a name collision to be a real, retryable possibility).
    Disambiguated by constraint name - `asyncpg` exposes it on the wrapped
    original exception - falling back to a substring search of the rendered
    error for a DBAPI that does not, rather than letting the wrong domain
    exception (and its wrong message) surface for either.
    """
    constraint = getattr(exc.orig, "constraint_name", None) or str(exc)
    if "invoice_template_name_unique" in str(constraint):
        return TemplateNameConflict(
            f"administration {administration_id} already has a template with this "
            f"name; choose a different name (FR-TPL-019)"
        )
    return TemplateConflict(
        f"administration {administration_id} already has a default template; "
        f"unset the existing default before making another one the default "
        f"(FR-TPL-011)"
    )

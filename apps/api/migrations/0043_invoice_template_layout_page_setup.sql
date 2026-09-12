-- 0043_invoice_template_layout_page_setup.sql
-- FR-TPL-005, FR-TPL-010 (PRD §6.3.1). See
-- docs/decisions/ADR-045-invoice-template-layout-and-page-setup.md.
--
--   FR-TPL-005  Layout: at least 4 starting layouts, each adjustable for
--               header arrangement, logo/address block placement, line-item
--               column selection and order, totals block position, and
--               footer content.
--   FR-TPL-010  Page setup: A4 and US Letter, margins, page numbering, and
--               correct multi-page behaviour with repeating headers and
--               carried-forward subtotals.
--
-- ===========================================================================
-- Four new curated-step columns, additive and backward compatible (NFR-044)
-- ===========================================================================
--
-- Every existing row gets these four columns via their DEFAULT, and every
-- default is exactly the value that reproduces 0042's pre-existing rendered
-- output unchanged - `split` header arrangement, `right` totals position,
-- `a4` page size, `normal` (56pt) margins are what `api.invoicing.rendering`
-- already draws today. No backfill, no rewrite of existing rows' meaning: a
-- template saved before this migration keeps drawing exactly as it did.
--
-- Same three-layer discipline 0042's header describes for FR-TPL-009's
-- columns: an `enum.Enum` in `api.templates.model` refuses a bad value before
-- it is ever constructed, and the CHECK constraints below refuse it again no
-- matter what writes the row.
alter table invoice_template
    add column header_arrangement text not null default 'split'
        check (header_arrangement in ('split', 'stacked', 'centered')),
    add column totals_position   text not null default 'right'
        check (totals_position in ('right', 'left', 'full_width')),
    add column page_size         text not null default 'a4'
        check (page_size in ('a4', 'letter')),
    add column margins           text not null default 'normal'
        check (margins in ('narrow', 'normal', 'wide'));

comment on column invoice_template.header_arrangement is
    'FR-TPL-005: split (default, today''s only geometry), stacked or centered. '
    'Independent of layout - see api.templates.model.HeaderArrangement.';
comment on column invoice_template.totals_position is
    'FR-TPL-005: right (default, today''s only placement), left or '
    'full_width. See api.templates.model.TotalsPosition.';
comment on column invoice_template.page_size is
    'FR-TPL-010: a4 (default, today''s only size, 595x842pt) or letter '
    '(612x792pt). See api.templates.model.PageSize.';
comment on column invoice_template.margins is
    'FR-TPL-010: narrow (40pt), normal (default, today''s existing 56pt) or '
    'wide (72pt) - curated steps, not a point/mm number field, the same '
    'philosophy every other control in this table already follows. See '
    'api.templates.model.Margins.';

-- ===========================================================================
-- FR-TPL-019: one-click duplicate needs a name to retry against
-- ===========================================================================
--
-- `api.templates.service.InvoiceTemplateService.duplicate` tries
-- "Copy of {name}", then "Copy of {name} (2)", "(3)", ... until one does not
-- collide. That retry loop is only meaningful if a name collision is a real,
-- database-enforced possibility within one administration - otherwise two
-- templates named identically would coexist silently and "duplicate" would
-- never need to retry at all. No two templates in the same administration
-- may share a name.
alter table invoice_template
    add constraint invoice_template_name_unique unique (administration_id, name);

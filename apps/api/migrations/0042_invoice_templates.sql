-- 0042_invoice_templates.sql
-- FR-TPL-001, FR-TPL-002, FR-TPL-003, FR-TPL-004, FR-TPL-005, FR-TPL-006,
-- FR-TPL-007, FR-TPL-009, FR-TPL-012, FR-TPL-013 (PRD §6.3.1).
-- See docs/decisions/ADR-041-invoice-template-model.md.
--
--   FR-TPL-001  Logo upload (PNG, JPEG, SVG), with position, size control and
--               a transparent-background preview against the paper colour.
--   FR-TPL-002  Typography: a curated set of at least 8 licensed,
--               PDF-embeddable typefaces covering serif, sans and mono.
--               Separate selection for headings, body and figures.
--   FR-TPL-003  Type controls exposed as a small number of sensible steps,
--               not free numeric entry.
--   FR-TPL-004  Colour: accent, text, background, with an automatic contrast
--               check.
--   FR-TPL-005  Layout: at least 4 starting layouts, each adjustable.
--   FR-TPL-006  Column control on line items: show, hide and reorder. Hiding
--               a column never hides legally required information.
--   FR-TPL-007  Editable content blocks with merge tag support.
--   FR-TPL-009  Statutory fields cannot be removed or hidden; the template
--               cannot be saved in a non-compliant state.
--   FR-TPL-012  Templates apply to the whole document family via a
--               parameter, not per-document-type rows.
--   FR-TPL-013  A template holds both Dutch and English content for its text
--               blocks; rendering follows the recipient's language.
--
-- ===========================================================================
-- FR-TPL-006 is six columns here, not seven, and that is a deliberate
-- deviation from the PRD's literal wording
-- ===========================================================================
--
-- FR-TPL-006 names seven columns including "VAT amount". Migration 0037
-- (ADR-037 decision #3) deliberately gives `sales_invoice_line` NO per-line
-- VAT amount: VAT is computed and rounded per TREATMENT GROUP, never per
-- line, because rounding twelve lines of a few cents each and summing them
-- can differ from rounding the group total by a cent - and the group total is
-- both what EU VAT Directive Art. 226 asks an invoice to show and what
-- reaches the aangifte. A per-line VAT amount column would invite a client to
-- sum a figure that disagrees with the invoice it sits on.
--
-- So `invoice_template_column` offers exactly the six columns that have a
-- well-defined per-line value: quantity, unit, unit_price, discount,
-- vat_rate, line_total. There is no toggle for a number the system does not
-- compute, and there never was one to hide - FR-TPL-009's "hiding a column
-- never hides required information" already implied it could not be offered
-- if it does not exist as a per-line fact.
--
-- ===========================================================================
-- FR-TPL-009 is enforced three times, cheapest bypass first
-- ===========================================================================
--
--   1. the designer UI does not offer the control (not in this migration)
--   2. api.templates.compliance.check() refuses to save a non-compliant
--      template, with a message a person can act on (FR-TPL-009's "reason
--      shown inline, not as a generic error")
--   3. THIS SCHEMA refuses it outright, in a CHECK constraint, no matter what
--      wrote the row
--
-- Layer 3 is what this file adds. `invoice_template_column` has
--
--     check (visible or column_key not in ('quantity', 'unit_price', 'vat_rate'))
--
-- and `invoice_template_block` requires the two statutory merge tags to
-- survive in BOTH languages of the one free-text block that is allowed to
-- carry them (`legal_identity`). Neither constraint trusts the layer above it
-- - the same posture 0037's triggers take toward the service layer, and the
-- same argument: an admin console, a bulk import or a future write path that
-- never goes through `api.templates.compliance` still cannot produce a row
-- that hides a legally required figure.
--
-- ===========================================================================
-- Templates apply to the whole document family (FR-TPL-012), and that is a
-- PARAMETER, not a set of rows
-- ===========================================================================
--
-- There is no `document_type` column here. FR-TPL-012 wants one look set once
-- for invoice, credit note, quote, order confirmation, reminder and
-- statement - and a row per document type would mean six templates to keep in
-- step by hand, which is the opposite of "set once". `api.templates.model`
-- carries `DocumentType` as an enum the RENDERER takes as an argument (which
-- heading, which merge tags resolve), not as a foreign key anywhere in this
-- schema.
--
-- ===========================================================================
-- Curated typefaces and curated steps, not free entry (FR-TPL-002, FR-TPL-003)
-- ===========================================================================
--
-- `template_font` is a shared lookup table, exactly the shape `vat_treatment`
-- (0028) already is: no RLS, because it names something true for every
-- tenant rather than belonging to one. It is seeded below with nine
-- typefaces - Inter, Source Sans 3, IBM Plex Sans and Public Sans for sans;
-- Source Serif 4, IBM Plex Serif and PT Serif for serif; IBM Plex Mono and
-- Source Code Pro for mono.
--
-- The SIL Open Font License 1.1 claim was independently VERIFIED this
-- session by fetching each project's actual license file, not merely
-- asserted from memory: Inter (github.com/rsms/inter/LICENSE.txt), IBM Plex
-- Sans/Serif/Mono (github.com/IBM/plex/LICENSE.txt - one license file covers
-- all three IBM Plex faces), Public Sans
-- (github.com/uswds/public-sans/LICENSE.md), Source Sans 3/Source Serif 4/
-- Source Code Pro (github.com/adobe-fonts/{source-sans,source-serif,
-- source-code-pro}/LICENSE.md), PT Serif
-- (github.com/google/fonts/ofl/ptserif/OFL.txt). All nine carry the
-- identical clause permitting bundling and redistribution with software
-- provided the license and copyright notice travel with it - which is what
-- makes embedding these families in a customer's generated PDF (once real
-- font binaries are supplied - see this file's own note below and ADR-042)
-- licensed, not merely "probably fine." See ADR-043 for the full per-family
-- table and the citations above, verbatim.
--
-- Tabular (lining) figures and full Latin-1 coverage are NOT independently
-- re-verified against these families' actual glyph tables this session - no
-- `.ttf` binary exists anywhere in this repository to inspect (ADR-042's own
-- hard constraint). The claim rests on established, well-documented
-- characteristics of these specific families instead: IBM Plex and Adobe's
-- Source superfamily were both explicitly engineered with full OpenType
-- figure-set support, and IBM Plex Mono/Source Code Pro are tabular by
-- construction as monospace fonts. Stated honestly here as what it is - a
-- property of the chosen families, not a measurement of files this
-- repository has actually opened.
--
-- Naming the family here is NOT the same claim as embedding it. Every other
-- "curated steps" field (logo size, type scale, font weight, line height,
-- letter spacing, colour) is a CHECK against a fixed small set for the same
-- reason FR-TPL-003 gives explicitly: a user choosing from six named options
-- cannot produce an unreadable invoice the way free numeric entry could.
--
-- `api.invoicing.rendering.MinimalPdfRenderer` - what actually produces a PDF
-- today - draws with the base-14 fonts every reader already has and embeds
-- nothing (see that module's docstring on FR-TPL-002/FR-TPL-020). This table
-- is the curated catalogue the designer offers; wiring real embedding of
-- these families into the PDF writer is untouched by this migration and is
-- named as a known gap in ADR-041, exactly as `rendering.py` already names it
-- for the font question generally.
--
-- ===========================================================================
-- One row per administration, and `version` is for concurrency, not for
-- re-rendering an issued document
-- ===========================================================================
--
-- FR-TPL-017 (unchanged by this migration) already makes an issued invoice's
-- stored PDF immutable regardless of what a template does later - editing a
-- template never touches a document already issued, because nothing re-reads
-- the template at render time for an issued invoice. `version`, bumped by
-- trigger on every UPDATE, exists for a narrower reason: so a client editing
-- a template in one tab can detect that another tab (or another user) saved
-- over it first, the same optimistic-concurrency use `journal_entry`-style
-- derived columns serve elsewhere - not to control what an already-issued PDF
-- looks like.
--
-- `logo_asset_id` -> `template_asset`, not `document`. A logo is user content
-- the business replaces at will, with no retention or immutability
-- obligation - the opposite of FR-DOC-002's seven-year, not-deletable-by-users
-- archive that `document` (0031) exists to be. Storing it there would either
-- weaken `document`'s guarantee for every OTHER row in it, or force a special
-- case into every place that reads `document` to skip logos. A second, purpose
-- -built table costs one migration and keeps both guarantees intact.

-- ===========================================================================
-- template_font - FR-TPL-002. Shared lookup, not tenant-scoped.
-- ===========================================================================
create table template_font (
    code        text primary key,
    family_name text not null,
    category    text not null check (category in ('serif', 'sans', 'mono')),
    created_at  timestamptz not null default now()
);

comment on table template_font is
    'FR-TPL-002''s curated, PDF-embeddable typeface catalogue. Shared across '
    'every tenant, like vat_treatment (0028) - no RLS. Naming a family here '
    'is not the same claim as embedding it; see this file''s header comment '
    'and ADR-041''s known gaps.';

insert into template_font (code, family_name, category) values
    ('inter',           'Inter',            'sans'),
    ('source_sans',     'Source Sans 3',    'sans'),
    ('ibm_plex_sans',   'IBM Plex Sans',    'sans'),
    ('public_sans',     'Public Sans',      'sans'),
    ('source_serif',    'Source Serif 4',   'serif'),
    ('ibm_plex_serif',  'IBM Plex Serif',   'serif'),
    ('pt_serif',        'PT Serif',         'serif'),
    ('ibm_plex_mono',   'IBM Plex Mono',    'mono'),
    ('source_code_pro', 'Source Code Pro',  'mono');

-- ===========================================================================
-- template_asset - FR-TPL-001, FR-TPL-018. NOT the `document` table.
-- ===========================================================================
create table template_asset (
    id                 uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1.
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),

    storage_key        text not null,

    -- FR-TPL-001's three accepted formats. Exhaustive by construction, the
    -- same closed-set posture as sales_invoice.status.
    content_type       text not null
                            check (content_type in ('image/png', 'image/jpeg', 'image/svg+xml')),

    -- FR-TPL-018: SVG is stripped of scripts and external references before
    -- storage. False until that pipeline has run; a row that is not yet
    -- sanitized must not be usable as a template's logo (enforced by the
    -- application, not repeated here as a second CHECK against
    -- invoice_template - see the trigger below).
    sanitized          boolean not null default false,

    created_at         timestamptz not null default now()
);

create index template_asset_administration_idx on template_asset(administration_id);
create index template_asset_organization_idx on template_asset(organization_id);

comment on table template_asset is
    'FR-TPL-001''s uploaded logos. Deliberately not a row in `document` '
    '(0031): FR-DOC-002''s seven-year, not-deletable-by-users retention is '
    'the wrong policy for a logo a user replaces whenever they like, and '
    'giving `document` an exception would weaken that guarantee for every '
    'other row in it.';

-- ===========================================================================
-- invoice_template - FR-TPL-001..005, FR-TPL-012
-- ===========================================================================
create table invoice_template (
    id                  uuid primary key default gen_random_uuid(),

    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    name                text not null check (length(btrim(name)) > 0),

    -- FR-TPL-011 (P1): one default per administration, enforced below by a
    -- partial unique index rather than a boolean-flip service method, so the
    -- invariant holds no matter what writes the row.
    is_default          boolean not null default false,

    -- Optimistic concurrency (see this file's header), NOT a re-render
    -- trigger. Bumped by trigger on every UPDATE; never supplied by a caller.
    version             integer not null default 1 check (version >= 1),

    -- FR-TPL-005: at least 4 starting layouts.
    layout              text not null default 'classic'
                            check (layout in ('classic', 'modern', 'compact', 'minimal')),

    -- FR-TPL-001: logo position and size as curated steps, not free numeric
    -- entry - the same "sensible steps" philosophy FR-TPL-003 states
    -- explicitly, applied here too.
    logo_asset_id       uuid references template_asset(id),
    logo_position       text not null default 'left'
                            check (logo_position in ('left', 'centre', 'right')),
    logo_size           text not null default 'medium'
                            check (logo_size in ('small', 'medium', 'large')),

    -- FR-TPL-002: separate selection for headings, body and figures, each
    -- constrained to the curated catalogue above.
    heading_font        text not null references template_font(code),
    body_font           text not null references template_font(code),
    figures_font        text not null references template_font(code),

    -- FR-TPL-003: size scale, weight, line height and letter spacing, each a
    -- small closed set.
    type_scale          text not null default 'medium'
                            check (type_scale in ('small', 'medium', 'large', 'extra_large')),
    font_weight         text not null default 'regular'
                            check (font_weight in ('regular', 'medium', 'bold')),
    line_height         text not null default 'normal'
                            check (line_height in ('tight', 'normal', 'relaxed')),
    letter_spacing      text not null default 'normal'
                            check (letter_spacing in ('tight', 'normal', 'wide')),

    -- FR-TPL-004: accent, text and background/paper colour. '#rrggbb' only -
    -- lower-case hex, matching what a colour picker emits and what the PDF
    -- writer parses without a second normalisation step. The automatic
    -- contrast check itself is advisory (api.templates.model.ColorScheme)
    -- and lives in application code, not here: a CHECK constraint can reject
    -- a malformed colour but has no business refusing a LEGAL one a user
    -- chose deliberately (e.g. for a print-only palette).
    accent_color        text not null default '#0f172a' check (accent_color ~ '^#[0-9a-f]{6}$'),
    text_color          text not null default '#111827' check (text_color ~ '^#[0-9a-f]{6}$'),
    background_color    text not null default '#ffffff' check (background_color ~ '^#[0-9a-f]{6}$'),

    updated_by_user_id  uuid references users(id),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index invoice_template_administration_idx on invoice_template(administration_id);
create index invoice_template_organization_idx on invoice_template(organization_id);

-- FR-TPL-011: at most one default per administration. Partial, because a
-- non-default row does not participate in the uniqueness at all - the same
-- shape sales_invoice_number_unique's NULL-sharing draft rows use, applied to
-- a boolean instead of a NULL.
create unique index invoice_template_one_default_idx
    on invoice_template(administration_id) where is_default;

comment on table invoice_template is
    'FR-TPL-001..005, FR-TPL-012. One row per named template. Applies to the '
    'whole document family (invoice, credit note, quote, order confirmation, '
    'reminder, statement) via a DocumentType parameter the renderer takes - '
    'there is no document_type column here, on purpose. See ADR-041.';

-- ===========================================================================
-- invoice_template_column - FR-TPL-006, FR-TPL-009
-- ===========================================================================
create table invoice_template_column (
    id                 uuid primary key default gen_random_uuid(),

    template_id        uuid not null references invoice_template(id) on delete cascade,
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),

    -- Six, not seven - see this file's header comment on the ADR-037
    -- deviation. There is no vat_amount key because there is no per-line VAT
    -- amount to show or hide.
    column_key         text not null
                            check (column_key in
                                ('quantity', 'unit', 'unit_price', 'discount', 'vat_rate', 'line_total')),

    visible            boolean not null default true,
    position           integer not null check (position >= 1),

    constraint invoice_template_column_unique unique (template_id, column_key),
    constraint invoice_template_column_position_unique unique (template_id, position),

    -- FR-TPL-009's last line of defense, layer 3 of the three described in
    -- this file's header. Quantity, unit price and VAT rate are the three of
    -- the six columns that art. 35a(1) requires an invoice to show per line;
    -- discount, unit and line_total are not independently statutory (a line
    -- total is derivable, a unit is descriptive, a discount is disclosed by
    -- the resulting price) and may be hidden freely.
    constraint invoice_template_column_statutory_visible check (
        visible or column_key not in ('quantity', 'unit_price', 'vat_rate')
    )
);

create index invoice_template_column_template_idx on invoice_template_column(template_id);
create index invoice_template_column_administration_idx
    on invoice_template_column(administration_id);
create index invoice_template_column_organization_idx
    on invoice_template_column(organization_id);

-- ===========================================================================
-- invoice_template_block - FR-TPL-007, FR-TPL-009, FR-TPL-013
-- ===========================================================================
create table invoice_template_block (
    id                 uuid primary key default gen_random_uuid(),

    template_id        uuid not null references invoice_template(id) on delete cascade,
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),

    block_key          text not null
                            check (block_key in
                                ('header', 'intro', 'payment_terms', 'footer', 'legal_identity')),

    -- FR-TPL-013: both languages held on the one row, so rendering follows
    -- the RECIPIENT's language independent of the designer's own UI
    -- language - the same split sales_invoice.customer_language makes at the
    -- document level, made here at the template level.
    text_nl            text not null default '',
    text_en            text not null default '',

    constraint invoice_template_block_unique unique (template_id, block_key),

    -- FR-TPL-009 applied to FREE TEXT, not just to a column toggle. The
    -- `legal_identity` block is FR-TPL-007's "free block for chamber of
    -- commerce number, VAT number, IBAN and general terms reference", and
    -- Wet OB art. 35a(1)(c) and (e) make the supplier's VAT number and (via
    -- the Handelsregisterwet) KvK number mandatory on every invoice. A user
    -- editing this block's prose could otherwise delete the very sentence
    -- that carries them. The merge tag literally has to survive - in BOTH
    -- languages - which is checkable with LIKE because the tag syntax
    -- ({{supplier_vat_number}}, {{supplier_kvk_number}}) is a fixed string,
    -- unlike the surrounding prose.
    constraint invoice_template_block_legal_identity_tags check (
        block_key <> 'legal_identity'
        or (
            text_nl like '%{{supplier_vat_number}}%' and
            text_nl like '%{{supplier_kvk_number}}%' and
            text_en like '%{{supplier_vat_number}}%' and
            text_en like '%{{supplier_kvk_number}}%'
        )
    )
);

create index invoice_template_block_template_idx on invoice_template_block(template_id);
create index invoice_template_block_administration_idx
    on invoice_template_block(administration_id);
create index invoice_template_block_organization_idx
    on invoice_template_block(organization_id);

-- ===========================================================================
-- Tenant coherence: a column or block belongs to its template's tenant
-- ===========================================================================
-- Mirrors sales_invoice_line_same_tenant() (0037) exactly, for the same
-- reason: a child row carrying a different administration_id from its parent
-- would be visible to one tenant and could be counted in another's template,
-- which RLS alone does not stop if a write path skips the check.
create or replace function invoice_template_child_same_tenant() returns trigger as $$
declare
    v_template_admin uuid;
    v_template_org   uuid;
begin
    select administration_id, organization_id
      into v_template_admin, v_template_org
      from invoice_template where id = new.template_id;

    if not found then
        raise exception 'invoice template % does not exist', new.template_id;
    end if;
    if v_template_admin is distinct from new.administration_id then
        raise exception
            'template row belongs to administration %, but its template belongs '
            'to % (IAM-001)', new.administration_id, v_template_admin;
    end if;

    new.organization_id := v_template_org;
    return new;
end;
$$ language plpgsql;

create trigger invoice_template_column_same_tenant_trg
    before insert or update on invoice_template_column
    for each row execute function invoice_template_child_same_tenant();

create trigger invoice_template_block_same_tenant_trg
    before insert or update on invoice_template_block
    for each row execute function invoice_template_child_same_tenant();

-- A logo asset attached to a template must belong to the SAME
-- administration. Not enforceable as a plain FK (there is no composite key to
-- reference), so it is a trigger - the same "checked rather than trusted"
-- posture as the child-row guard above, applied to a cross-table reference
-- instead of a parent-child one.
create or replace function invoice_template_logo_same_tenant() returns trigger as $$
declare
    v_asset_admin uuid;
begin
    if new.logo_asset_id is null then
        return new;
    end if;

    select administration_id into v_asset_admin
      from template_asset where id = new.logo_asset_id;

    if not found then
        raise exception 'template asset % does not exist', new.logo_asset_id;
    end if;
    if v_asset_admin is distinct from new.administration_id then
        raise exception
            'logo asset % belongs to administration %, but this template belongs '
            'to % (IAM-001)', new.logo_asset_id, v_asset_admin, new.administration_id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger invoice_template_logo_same_tenant_trg
    before insert or update on invoice_template
    for each row execute function invoice_template_logo_same_tenant();

-- ===========================================================================
-- version - bumped by trigger, never supplied by a caller
-- ===========================================================================
-- Derive-don't-trust, the same posture sales_invoice_allocate_number takes
-- for FR-AR-004's number: a caller that could set its own version could make
-- a stale edit look current, defeating the optimistic-concurrency check this
-- column exists for.
create or replace function invoice_template_bump_version() returns trigger as $$
begin
    new.version := old.version + 1;
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger invoice_template_bump_version_trg
    before update on invoice_template
    for each row execute function invoice_template_bump_version();

-- ===========================================================================
-- Row-level security - IAM-001, IAM-005
-- ===========================================================================
-- template_font carries NO RLS: it is a shared catalogue, like vat_treatment
-- (0028), true for every tenant rather than belonging to one.
alter table invoice_template        enable row level security;
alter table invoice_template        force  row level security;
alter table invoice_template_column enable row level security;
alter table invoice_template_column force  row level security;
alter table invoice_template_block  enable row level security;
alter table invoice_template_block  force  row level security;
alter table template_asset          enable row level security;
alter table template_asset          force  row level security;

create policy invoice_template_select on invoice_template
    for select using (app.has_administration_access(administration_id));
create policy invoice_template_insert on invoice_template
    for insert with check (app.has_administration_access(administration_id));
create policy invoice_template_update on invoice_template
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy invoice_template_delete on invoice_template
    for delete using (app.has_administration_access(administration_id));

create policy invoice_template_column_select on invoice_template_column
    for select using (app.has_administration_access(administration_id));
create policy invoice_template_column_insert on invoice_template_column
    for insert with check (app.has_administration_access(administration_id));
create policy invoice_template_column_update on invoice_template_column
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy invoice_template_column_delete on invoice_template_column
    for delete using (app.has_administration_access(administration_id));

create policy invoice_template_block_select on invoice_template_block
    for select using (app.has_administration_access(administration_id));
create policy invoice_template_block_insert on invoice_template_block
    for insert with check (app.has_administration_access(administration_id));
create policy invoice_template_block_update on invoice_template_block
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy invoice_template_block_delete on invoice_template_block
    for delete using (app.has_administration_access(administration_id));

create policy template_asset_select on template_asset
    for select using (app.has_administration_access(administration_id));
create policy template_asset_insert on template_asset
    for insert with check (app.has_administration_access(administration_id));
create policy template_asset_delete on template_asset
    for delete using (app.has_administration_access(administration_id));

grant select on template_font to ledgr_app;
grant select, insert, update, delete on invoice_template        to ledgr_app;
grant select, insert, update, delete on invoice_template_column to ledgr_app;
grant select, insert, update, delete on invoice_template_block  to ledgr_app;
grant select, insert, delete         on template_asset          to ledgr_app;

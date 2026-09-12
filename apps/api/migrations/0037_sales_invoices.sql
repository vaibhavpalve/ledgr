-- 0037_sales_invoices.sql
-- FR-AR-001, FR-AR-002, FR-AR-003, FR-AR-004 (PRD 6.3). See
-- docs/decisions/ADR-037-sales-invoices.md.
--
--   FR-AR-001  Create, edit, send and credit sales invoices with line items,
--              quantities, unit prices, discounts and per-line VAT treatment.
--   FR-AR-002  VAT handling: 21%, 9%, 0%, exempt, reverse charge domestic,
--              intra-Community supply, export, margin scheme. Correct legal
--              wording rendered per treatment.
--   FR-AR-003  Invoices comply with Dutch statutory invoice content
--              requirements; the system blocks sending if a mandatory field is
--              absent.
--   FR-AR-004  Sequential, gapless invoice numbering per year, configurable
--              prefix; issued numbers cannot be reused.
--
-- ===========================================================================
-- A draft is a document; an ISSUED invoice is a statutory record
-- ===========================================================================
--
-- The two halves of this schema answer to different rules, and the boundary
-- between them is the moment a number is allocated.
--
--   draft    freely editable, has NO number, and may be deleted. It is a
--            working document and nothing has been asserted to anybody.
--   issued   frozen. It carries a number that can never be reused, its lines
--            and amounts cannot change, and it cannot be deleted. A mistake is
--            corrected by a CREDIT NOTE, never by an edit.
--
-- That is CLAUDE.md's second rule - corrections happen via reversing entries,
-- never mutation - applied one layer above the ledger. An invoice is a claim
-- made to somebody outside the business; once it has been made, the record of
-- what was claimed has to survive the correction of it.
--
-- ===========================================================================
-- Why the number is allocated at ISSUE and not at creation
-- ===========================================================================
--
-- FR-AR-004 wants the series gapless. A number handed out when a draft is
-- created is a number that disappears when the draft is abandoned, and every
-- abandoned draft is then a hole somebody has to explain to an inspector. So
-- `invoice_number` is NULL for a draft and allocated in the transition to
-- issued.
--
-- The allocation is `update ... returning`, exactly as journal_sequence does
-- for FR-GL-013, and for the same two reasons:
--
--   * it takes a row lock, so two concurrent issues serialise rather than
--     colliding on the unique constraint;
--   * it is a TABLE UPDATE, not a sequence. A rolled-back transaction takes
--     the increment back with it. A Postgres sequence would not, and
--     `nextval` is exactly how a gapless series acquires gaps.
--
-- The cost is honest: issuing is serialised per administration per year. That
-- is the correct trade - the requirement is gaplessness, and an invoice issue
-- is not a hot path.
--
-- ===========================================================================
-- VAT is grouped by treatment, not summed per line
-- ===========================================================================
--
-- `sales_invoice_vat_total` holds one row per treatment on the invoice, with
-- the taxable amount and the VAT on it. That is the shape EU VAT Directive
-- Art. 226 requires an invoice to show, and it is deliberately NOT the sum of
-- a per-line VAT column: rounding each line and adding them up can differ by
-- cents from rounding the group total, and the figure that has to be right is
-- the one the customer checks and the one that reaches the aangifte.
--
-- The rows are computed and frozen at issue rather than derived on read, for
-- the reason 0033 gives about storing `vat_rate`: CMP-014 keeps historical
-- periods on the rules that applied at the time, and a total recomputed after
-- a later ruleset load would silently restate a document already in somebody
-- else's hands.

-- ===========================================================================
-- invoice_sequence - FR-AR-004
-- ===========================================================================
create table invoice_sequence (
    organization_id   uuid not null references organization(id),
    administration_id uuid not null references administration(id),
    fiscal_year_id    uuid not null references fiscal_year(id),

    -- FR-AR-004's "configurable prefix". Held per administration per YEAR,
    -- because that is the grain at which a business changes it ("2026-"), and
    -- because a prefix that could change mid-series would make two different
    -- documents both plausibly "number 7".
    prefix            text not null default '',

    next_number       bigint not null default 1 check (next_number >= 1),

    primary key (administration_id, fiscal_year_id)
);

create index invoice_sequence_organization_idx on invoice_sequence(organization_id);

-- ===========================================================================
-- sales_invoice - FR-AR-001
-- ===========================================================================
create table sales_invoice (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1. Both, on every row, and RLS reads administration_id.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    fiscal_year_id      uuid not null references fiscal_year(id),

    status              text not null default 'draft'
                            check (status in ('draft', 'issued')),

    -- FR-AR-004. NULL until issued; never changed once set; never reused.
    invoice_number      bigint check (invoice_number >= 1),
    -- Snapshotted beside the number rather than joined from invoice_sequence,
    -- so the reference on a document already sent cannot change when next
    -- year's prefix is configured.
    number_prefix       text,
    -- What the customer sees and quotes back. Generated, so the two halves
    -- cannot drift apart.
    invoice_reference   text generated always as (
                            case when invoice_number is null then null
                                 else coalesce(number_prefix, '') || invoice_number::text
                            end
                        ) stored,

    invoice_date        date not null,
    -- FR-AR-003: the date of supply, where it differs from the invoice date.
    supply_date         date,
    due_date            date,

    -- ---------------------------------------------------------------------
    -- The customer, SNAPSHOTTED - FR-AR-003
    -- ---------------------------------------------------------------------
    -- Copied onto the invoice rather than joined from a customer master
    -- (FR-AR-006, not built). Not a shortcut: an invoice is a statutory record
    -- of what was asserted on a date, and a customer who moves next year must
    -- not silently rewrite the address on a document already filed. When
    -- FR-AR-006 lands it POPULATES these columns; it does not replace them.
    customer_name       text not null check (length(btrim(customer_name)) > 0),
    customer_address    text not null check (length(btrim(customer_address)) > 0),
    customer_country    text not null default 'NL'
                            check (customer_country ~ '^[A-Z]{2}$'),
    -- Statutory on a reverse-charge or intra-Community invoice, and checked
    -- there rather than here: an ordinary domestic invoice needs none.
    customer_vat_number text,

    -- FR-AR-001's "credit". A credit note is a sales_invoice in its own right,
    -- with its own number from the same series, pointing at what it credits.
    -- The original is never touched - the same shape as journal_entry's
    -- reverses_entry_id, and for the same reason.
    credits_invoice_id  uuid references sales_invoice(id),

    notes               text,

    -- NFR-032.
    idempotency_key     text,

    created_by_user_id  uuid references users(id),
    created_at          timestamptz not null default now(),
    issued_at           timestamptz,

    -- FR-AR-004: unique per administration per year. Partial, because drafts
    -- share a NULL and NULLs do not collide.
    constraint sales_invoice_number_unique
        unique (administration_id, fiscal_year_id, invoice_number),

    -- An issued invoice has everything an issued invoice has. Stated as one
    -- constraint so no code path can produce a half-issued row.
    constraint sales_invoice_issued_is_numbered check (
        (status = 'draft'  and invoice_number is null and issued_at is null)
        or
        (status = 'issued' and invoice_number is not null and issued_at is not null)
    ),

    constraint sales_invoice_due_after_invoice check (
        due_date is null or due_date >= invoice_date
    )
);

create index sales_invoice_administration_idx
    on sales_invoice(administration_id, invoice_date desc);
create index sales_invoice_organization_idx on sales_invoice(organization_id);
create index sales_invoice_year_number_idx
    on sales_invoice(administration_id, fiscal_year_id, invoice_number);
create unique index sales_invoice_idempotency_idx
    on sales_invoice(administration_id, idempotency_key)
    where idempotency_key is not null;

-- FR-AR-001: an invoice may be credited at most once by a given credit note,
-- and a credit note credits exactly one invoice. A second full credit note
-- against the same invoice would double the correction - the argument 0020
-- makes about journal_entry_reverses_once_idx.
create unique index sales_invoice_credited_once_idx
    on sales_invoice(credits_invoice_id) where credits_invoice_id is not null;

-- ===========================================================================
-- sales_invoice_line - FR-AR-001
-- ===========================================================================
create table sales_invoice_line (
    id                uuid primary key default gen_random_uuid(),

    organization_id   uuid not null references organization(id),
    administration_id uuid not null references administration(id),
    invoice_id        uuid not null references sales_invoice(id) on delete cascade,

    -- The order the customer reads them in. Gapless is not required here and
    -- is not claimed: a line is not a statutory series.
    position          integer not null check (position >= 1),

    description       text not null check (length(btrim(description)) > 0),

    -- FR-AR-001's quantities and unit prices. Four decimals on both, because a
    -- unit price of EUR 0.0350 per item is ordinary and rounding it to the cent
    -- at entry would lose the price rather than the total. NFR-031: numeric
    -- throughout, never a float.
    quantity          numeric(19,4) not null,
    unit_price        numeric(19,4) not null,

    -- FR-AR-001's discounts, as a percentage of the line. See ADR-037 for why
    -- a percentage rather than an amount.
    discount_percent  numeric(9,4) not null default 0
                          check (discount_percent >= 0 and discount_percent <= 100),

    -- FR-AR-001's per-line VAT treatment. All eight of FR-AR-002's are legal
    -- values because vat_treatment already holds exactly those (0028).
    vat_treatment     text not null references vat_treatment(code),

    -- The line total excluding VAT, rounded to the cent. GENERATED, so it is
    -- not a figure any writer supplies and therefore cannot disagree with the
    -- quantity and price beside it - the device 0033 uses for net_amount.
    line_net          numeric(19,2) generated always as (
                          round(quantity * unit_price * (1 - discount_percent / 100), 2)
                      ) stored,

    constraint sales_invoice_line_position_unique unique (invoice_id, position)
);

create index sales_invoice_line_invoice_idx on sales_invoice_line(invoice_id, position);
create index sales_invoice_line_organization_idx on sales_invoice_line(organization_id);
create index sales_invoice_line_administration_idx on sales_invoice_line(administration_id);

-- ===========================================================================
-- sales_invoice_vat_total - FR-AR-002
-- ===========================================================================
-- One row per treatment appearing on the invoice. Written at issue and frozen.
create table sales_invoice_vat_total (
    invoice_id        uuid not null references sales_invoice(id) on delete cascade,
    organization_id   uuid not null references organization(id),
    administration_id uuid not null references administration(id),

    vat_treatment     text not null references vat_treatment(code),

    -- The rate that applied on the invoice date, resolved at issue and STORED.
    -- CMP-014: a later ruleset load must not restate a document already sent.
    -- NULL where the treatment has no rate concept at all (margin scheme).
    rate              numeric(7,3),

    taxable_amount    numeric(19,2) not null,
    vat_amount        numeric(19,2) not null,

    primary key (invoice_id, vat_treatment)
);

create index sales_invoice_vat_total_administration_idx
    on sales_invoice_vat_total(administration_id);
create index sales_invoice_vat_total_organization_idx
    on sales_invoice_vat_total(organization_id);

-- ===========================================================================
-- Tenant coherence: a line belongs to its invoice's administration
-- ===========================================================================
-- Checked rather than trusted. A line carrying a different administration_id
-- from its invoice would be visible to one tenant and counted in another's
-- totals - the failure RLS exists to make impossible, arriving through a
-- column nobody was watching. The same guard 0032 puts on capture_page.
create or replace function sales_invoice_line_same_tenant() returns trigger as $$
declare
    v_invoice_admin uuid;
    v_invoice_org   uuid;
    v_status        text;
begin
    select administration_id, organization_id, status
      into v_invoice_admin, v_invoice_org, v_status
      from sales_invoice where id = new.invoice_id;

    if not found then
        raise exception 'invoice % does not exist', new.invoice_id;
    end if;
    if v_invoice_admin is distinct from new.administration_id then
        raise exception
            'line belongs to administration %, but its invoice belongs to % '
            '(IAM-001)', new.administration_id, v_invoice_admin;
    end if;

    -- Derived rather than trusted, so the two can never disagree.
    new.organization_id := v_invoice_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_line_same_tenant_trg
    before insert or update on sales_invoice_line
    for each row execute function sales_invoice_line_same_tenant();

create or replace function sales_invoice_vat_total_same_tenant() returns trigger as $$
declare
    v_invoice_admin uuid;
    v_invoice_org   uuid;
begin
    select administration_id, organization_id into v_invoice_admin, v_invoice_org
      from sales_invoice where id = new.invoice_id;
    if not found then
        raise exception 'invoice % does not exist', new.invoice_id;
    end if;
    if v_invoice_admin is distinct from new.administration_id then
        raise exception
            'VAT total belongs to administration %, but its invoice belongs to % '
            '(IAM-001)', new.administration_id, v_invoice_admin;
    end if;
    new.organization_id := v_invoice_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_vat_total_same_tenant_trg
    before insert or update on sales_invoice_vat_total
    for each row execute function sales_invoice_vat_total_same_tenant();

-- ===========================================================================
-- FR-AR-004: the number, allocated once, at issue, and never again
-- ===========================================================================
create or replace function sales_invoice_allocate_number() returns trigger as $$
declare
    v_prefix text;
begin
    -- Only the draft -> issued transition allocates. Any other update leaves
    -- the number exactly as it was, which is what "cannot be reused" means in
    -- practice: there is no code path that writes this column twice.
    if old.status = 'draft' and new.status = 'issued' then

        -- Derive-don't-trust, as journal_entry_validate does for FR-GL-013. A
        -- caller that could name its own number could fill a hole, restart the
        -- series, or duplicate a number in another year. The unique constraint
        -- catches only the last of those.
        insert into invoice_sequence
            (organization_id, administration_id, fiscal_year_id, next_number)
        values
            (new.organization_id, new.administration_id, new.fiscal_year_id, 1)
        on conflict (administration_id, fiscal_year_id) do nothing;

        update invoice_sequence
           set next_number = next_number + 1
         where administration_id = new.administration_id
           and fiscal_year_id = new.fiscal_year_id
        returning next_number - 1, prefix into new.invoice_number, v_prefix;

        new.number_prefix := v_prefix;
        new.issued_at := now();

    elsif old.status = 'issued' and new.status = 'draft' then
        -- Un-issuing would free a number that has been quoted to somebody, and
        -- FR-AR-004 says issued numbers cannot be reused. A mistake on an
        -- issued invoice is corrected by a credit note.
        raise exception
            'invoice % has been issued and cannot return to draft; correct it '
            'with a credit note (FR-AR-001, FR-AR-004)', old.id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_allocate_number_trg
    before update on sales_invoice
    for each row execute function sales_invoice_allocate_number();

-- ===========================================================================
-- An issued invoice is frozen
-- ===========================================================================
-- Everything a customer was told stays exactly as they were told it. The only
-- columns that may still move are the ones that describe what happened to the
-- document AFTER it was issued, not what it says.
create or replace function sales_invoice_issued_is_frozen() returns trigger as $$
begin
    if old.status <> 'issued' then
        return new;
    end if;

    if new.invoice_number   is distinct from old.invoice_number
    or new.number_prefix    is distinct from old.number_prefix
    or new.invoice_date     is distinct from old.invoice_date
    or new.supply_date      is distinct from old.supply_date
    or new.due_date         is distinct from old.due_date
    or new.customer_name    is distinct from old.customer_name
    or new.customer_address is distinct from old.customer_address
    or new.customer_country is distinct from old.customer_country
    or new.customer_vat_number is distinct from old.customer_vat_number
    or new.fiscal_year_id   is distinct from old.fiscal_year_id
    or new.administration_id is distinct from old.administration_id
    or new.organization_id  is distinct from old.organization_id
    then
        raise exception
            'invoice % has been issued; its content cannot be changed. Correct '
            'it with a credit note (FR-AR-001).', old.id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_issued_is_frozen_trg
    before update on sales_invoice
    for each row execute function sales_invoice_issued_is_frozen();

create or replace function sales_invoice_issued_is_permanent() returns trigger as $$
begin
    if old.status = 'issued' then
        raise exception
            'invoice % has been issued and cannot be deleted; its number is '
            'part of a gapless series (FR-AR-004). Credit it instead.', old.id;
    end if;
    return old;
end;
$$ language plpgsql;

create trigger sales_invoice_issued_is_permanent_trg
    before delete on sales_invoice
    for each row execute function sales_invoice_issued_is_permanent();

-- Lines and VAT totals of an issued invoice are equally frozen. Without this,
-- the invoice header would be immutable and the amounts on it would not.
create or replace function sales_invoice_lines_draft_only() returns trigger as $$
declare
    v_invoice uuid;
    v_status  text;
begin
    v_invoice := coalesce(new.invoice_id, old.invoice_id);
    select status into v_status from sales_invoice where id = v_invoice;

    -- A deleted invoice cascades to its lines; by then the parent row is gone
    -- and there is nothing left to protect.
    if not found then
        return coalesce(new, old);
    end if;

    if v_status = 'issued' then
        raise exception
            'invoice % has been issued; its lines cannot be changed. Correct it '
            'with a credit note (FR-AR-001).', v_invoice;
    end if;
    return coalesce(new, old);
end;
$$ language plpgsql;

create trigger sales_invoice_line_draft_only_trg
    before insert or update or delete on sales_invoice_line
    for each row execute function sales_invoice_lines_draft_only();

-- ===========================================================================
-- FR-GL-013-style gap detection, for FR-AR-004
-- ===========================================================================
-- Always empty while the allocation above stands. Reported anyway, for the
-- reason 0020 gives about its own gap report: a check that can only ever be
-- empty is the check on the thing that makes it empty. An inspector asking
-- "prove the series is gapless" gets an answer rather than an assurance.
create or replace function invoice_number_gaps(
    p_administration_id uuid,
    p_fiscal_year_id    uuid
) returns table (missing_number bigint)
language sql stable as $$
    select g.n
      from generate_series(1, coalesce((
                select max(invoice_number)
                  from sales_invoice
                 where administration_id = p_administration_id
                   and fiscal_year_id = p_fiscal_year_id
            ), 0)) as g(n)
     where not exists (
        select 1 from sales_invoice x
         where x.administration_id = p_administration_id
           and x.fiscal_year_id = p_fiscal_year_id
           and x.invoice_number = g.n
     )
     order by g.n;
$$;

-- ===========================================================================
-- Row-level security - IAM-001, IAM-005
-- ===========================================================================
alter table sales_invoice           enable row level security;
alter table sales_invoice           force  row level security;
alter table sales_invoice_line      enable row level security;
alter table sales_invoice_line      force  row level security;
alter table sales_invoice_vat_total enable row level security;
alter table sales_invoice_vat_total force  row level security;
alter table invoice_sequence        enable row level security;
alter table invoice_sequence        force  row level security;

create policy sales_invoice_select on sales_invoice
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_insert on sales_invoice
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_update on sales_invoice
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy sales_invoice_delete on sales_invoice
    for delete using (app.has_administration_access(administration_id));

create policy sales_invoice_line_select on sales_invoice_line
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_line_insert on sales_invoice_line
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_line_update on sales_invoice_line
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy sales_invoice_line_delete on sales_invoice_line
    for delete using (app.has_administration_access(administration_id));

create policy sales_invoice_vat_total_select on sales_invoice_vat_total
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_vat_total_insert on sales_invoice_vat_total
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_vat_total_delete on sales_invoice_vat_total
    for delete using (app.has_administration_access(administration_id));

create policy invoice_sequence_select on invoice_sequence
    for select using (app.has_administration_access(administration_id));
create policy invoice_sequence_insert on invoice_sequence
    for insert with check (app.has_administration_access(administration_id));
create policy invoice_sequence_update on invoice_sequence
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

grant select, insert, update, delete on sales_invoice           to ledgr_app;
grant select, insert, update, delete on sales_invoice_line      to ledgr_app;
grant select, insert, delete          on sales_invoice_vat_total to ledgr_app;
grant select, insert, update          on invoice_sequence        to ledgr_app;
grant execute on function invoice_number_gaps(uuid, uuid) to ledgr_app;

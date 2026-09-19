-- 0057_sales_quotes.sql
-- FR-AR-007 (PRD §6.3), SI-08. See docs/decisions/ADR-075-quotes-and-conversion.md.
--
--   FR-AR-007  Quotes and order confirmations convertible to invoices.
--
-- ===========================================================================
-- What this is, and what it deliberately is not
-- ===========================================================================
--
-- The requirement is CONVERSION, but there was no quote to convert: FR-TPL-012 names
-- quotes and order confirmations as part of the document family and
-- `DocumentType` reserves the members, with the comment that they are "later PRD
-- sections". So this builds the smallest quote that conversion needs - a numbered
-- document with lines, a validity date and a lifecycle - and stops there. Rendering
-- one as a PDF and e-mailing it is the document family's work (FR-TPL-012), not
-- this migration's.
--
-- A quote is NOT an invoice and asserts nothing in the books: no number from the
-- gapless series (FR-AR-004), no posting, no VAT. Its own number is a plain
-- per-administration counter - gaps are fine, a quote is not a statutory document.
--
-- ===========================================================================
-- The lifecycle, enforced here as well as in code
-- ===========================================================================
--
--     draft --> sent --> accepted --> converted (an invoice now exists)
--       |         |          |
--       |         +--> declined
--       +---------+----------+--> cancelled
--
-- `converted`, `declined` and `cancelled` are terminal. A quote is EDITABLE only
-- while it is a draft: once it has gone to a customer it is what they were told, and
-- changing it silently would change the offer. (The one exception is extending its
-- validity while it is still `sent`, which only ever helps the customer.) The
-- triggers below refuse anything else, so a second writer cannot sidestep the
-- service.
--
-- ===========================================================================
-- One quote, one invoice
-- ===========================================================================
--
-- `converted_invoice_id` is unique and set in the same statement that moves the quote
-- to `converted`, so two conversions racing produce one invoice: the second finds the
-- quote no longer `accepted`. Partial delivery (invoicing half of a quote now and
-- half later) is not modelled.

begin;

-- ===========================================================================
-- sales_quote_counter - the per-administration number
-- ===========================================================================
create table sales_quote_counter (
    administration_id   uuid not null references administration(id),
    organization_id     uuid not null references organization(id),
    kind                text not null check (kind in ('quote', 'order_confirmation')),
    last_number         integer not null default 0 check (last_number >= 0),
    primary key (administration_id, kind)
);

comment on table sales_quote_counter is
    'FR-AR-007. The next quote number per administration and kind. A plain counter: '
    'gaps are fine, a quote is not a statutory document (FR-AR-004 is for invoices).';

-- ===========================================================================
-- sales_quote
-- ===========================================================================
create table sales_quote (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- FR-TPL-012's two members that precede an invoice.
    kind                text not null default 'quote'
                            check (kind in ('quote', 'order_confirmation')),

    -- Allocated by `sales_quote_number_trg`, never supplied. `reference` is what a
    -- customer quotes back: 'OF-0007' for a quote, 'OB-0003' for a confirmation.
    quote_number        integer not null,
    reference           text not null,

    -- A customer MASTER record: converting needs one to raise an invoice for, and a
    -- prospect is simply a customer not yet invoiced.
    customer_id         uuid not null references customer(id),

    subject             text check (subject is null or length(btrim(subject)) > 0),
    -- The last day the offer stands. Null: it does not expire.
    valid_until         date,
    notes               text,

    status              text not null default 'draft'
                            check (status in (
                                'draft', 'sent', 'accepted', 'declined', 'cancelled', 'converted'
                            )),

    sent_at             timestamptz,
    accepted_at         timestamptz,
    -- WHO recorded the acceptance (a user of this system)...
    accepted_by_user_id uuid references users(id),
    -- ...and who ACCEPTED it on the customer's side, and their own reference (a
    -- purchase-order number). Both are free text the customer supplied.
    accepted_by_name    text,
    acceptance_reference text,
    declined_at         timestamptz,
    decline_reason      text,
    cancelled_at        timestamptz,

    converted_at        timestamptz,
    converted_invoice_id uuid references sales_invoice(id),

    created_by_user_id  uuid references users(id),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint sales_quote_reference_unique unique (administration_id, reference),
    -- A converted quote points at its invoice; nothing else does. Together with the
    -- unique index below, that is "one quote, one invoice".
    constraint sales_quote_converted_has_an_invoice check (
        (status = 'converted') = (converted_invoice_id is not null)
    ),
    constraint sales_quote_converted_was_accepted check (
        status <> 'converted' or accepted_at is not null
    )
);

create unique index sales_quote_invoice_idx
    on sales_quote(converted_invoice_id) where converted_invoice_id is not null;
create index sales_quote_administration_idx
    on sales_quote(administration_id, status, created_at desc);
create index sales_quote_organization_idx on sales_quote(organization_id);
create index sales_quote_customer_idx on sales_quote(customer_id);

comment on table sales_quote is
    'FR-AR-007. A quote or order confirmation: numbered, with lines and a validity '
    'date, editable only while a draft, convertible once accepted into exactly one '
    'draft invoice. Asserts nothing in the books.';

-- ===========================================================================
-- sales_quote_line
-- ===========================================================================
create table sales_quote_line (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    quote_id            uuid not null references sales_quote(id) on delete cascade,

    position            integer not null check (position >= 1),
    description         text not null check (length(btrim(description)) > 0),

    -- Exactly `sales_invoice_line`'s shape and precision (0037), so a quoted line
    -- becomes an invoice line with no conversion and no rounding.
    quantity            numeric(19,4) not null,
    unit_price          numeric(19,4) not null,
    discount_percent    numeric(9,4) not null default 0
                            check (discount_percent >= 0 and discount_percent <= 100),
    vat_treatment       text not null references vat_treatment(code),

    -- The same generated total as an invoice line, so a quote's net total is what the
    -- invoice's will be.
    line_net            numeric(19,2) generated always as (
                            round(quantity * unit_price * (1 - discount_percent / 100), 2)
                        ) stored,

    constraint sales_quote_line_position_unique unique (quote_id, position)
);

create index sales_quote_line_quote_idx on sales_quote_line(quote_id, position);
create index sales_quote_line_organization_idx on sales_quote_line(organization_id);

-- ===========================================================================
-- Numbering, and tenant coherence
-- ===========================================================================
create or replace function sales_quote_before_insert() returns trigger as $$
declare
    v_org      uuid;
    v_admin    uuid;
    v_number   integer;
begin
    select organization_id into v_org from administration where id = new.administration_id;
    if v_org is null then
        raise exception 'administration % does not exist', new.administration_id;
    end if;

    select administration_id into v_admin from customer where id = new.customer_id;
    if v_admin is distinct from new.administration_id then
        raise exception
            'a quote is addressed to a customer of its own administration: customer is in %, '
            'quote claims % (CLAUDE.md rule 1)', v_admin, new.administration_id;
    end if;

    -- An atomic upsert-increment: two quotes created at once get two numbers.
    insert into sales_quote_counter (administration_id, organization_id, kind, last_number)
    values (new.administration_id, v_org, new.kind, 1)
    on conflict (administration_id, kind)
    do update set last_number = sales_quote_counter.last_number + 1
    returning last_number into v_number;

    new.organization_id := v_org;
    new.quote_number    := v_number;
    new.reference       := case new.kind when 'quote' then 'OF-' else 'OB-' end
                           || lpad(v_number::text, 4, '0');
    return new;
end;
$$ language plpgsql;

create trigger sales_quote_before_insert_trg
    before insert on sales_quote
    for each row execute function sales_quote_before_insert();

-- ---------------------------------------------------------------------------
-- The lifecycle, and "editable only while a draft"
-- ---------------------------------------------------------------------------
create or replace function sales_quote_before_update() returns trigger as $$
begin
    -- Identity never changes.
    if new.id is distinct from old.id
       or new.administration_id is distinct from old.administration_id
       or new.organization_id  is distinct from old.organization_id
       or new.kind             is distinct from old.kind
       or new.quote_number     is distinct from old.quote_number
       or new.reference        is distinct from old.reference then
        raise exception 'a quote''s identity (number, reference, kind) never changes';
    end if;

    -- Terminal states are history.
    if old.status in ('declined', 'cancelled', 'converted') then
        raise exception
            'quote % is % and is history; create a new quote instead', old.reference, old.status;
    end if;

    if new.status is distinct from old.status then
        if not (
            (old.status = 'draft'    and new.status in ('sent', 'accepted', 'cancelled'))
            or (old.status = 'sent'  and new.status in ('accepted', 'declined', 'cancelled'))
            or (old.status = 'accepted' and new.status in ('converted', 'cancelled'))
        ) then
            raise exception
                'a quote cannot go from % to % (FR-AR-007)', old.status, new.status;
        end if;
    end if;

    -- Once it has gone to the customer it is what they were told.
    if old.status <> 'draft' and (
        new.customer_id is distinct from old.customer_id
        or new.subject  is distinct from old.subject
        or new.notes    is distinct from old.notes
        or (new.valid_until is distinct from old.valid_until
            -- ...except extending the validity of one that is still out, which only
            -- ever helps the customer.
            and not (old.status = 'sent'
                     and new.status = 'sent'
                     and old.valid_until is not null
                     and new.valid_until is not null
                     and new.valid_until > old.valid_until))
    ) then
        raise exception
            'quote % has been % and is what the customer was told; it can no longer be '
            'edited. Cancel it and create a new one.', old.reference, old.status;
    end if;

    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger sales_quote_before_update_trg
    before update on sales_quote
    for each row execute function sales_quote_before_update();

-- Lines belong to the quote's administration, and only a DRAFT's may change.
create or replace function sales_quote_line_guard() returns trigger as $$
declare
    v_quote_id uuid;
    v_admin    uuid;
    v_org      uuid;
    v_status   text;
begin
    v_quote_id := case when tg_op = 'DELETE' then old.quote_id else new.quote_id end;
    select administration_id, organization_id, status into v_admin, v_org, v_status
      from sales_quote where id = v_quote_id;

    if v_status is distinct from 'draft' then
        raise exception
            'the lines of a quote that is % cannot change; only a draft is editable', v_status;
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'a quote line belongs to its quote''s administration: quote is in %, line claims % '
            '(CLAUDE.md rule 1)', v_admin, new.administration_id;
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_quote_line_guard_trg
    before insert or update or delete on sales_quote_line
    for each row execute function sales_quote_line_guard();

-- A conversion's invoice must be the quote's own administration's.
create or replace function sales_quote_invoice_guard() returns trigger as $$
declare
    v_invoice_admin uuid;
begin
    if new.converted_invoice_id is not null then
        select administration_id into v_invoice_admin
          from sales_invoice where id = new.converted_invoice_id;
        if v_invoice_admin is distinct from new.administration_id then
            raise exception
                'a quote converts into an invoice of its own administration: invoice is in %, '
                'quote is in % (IAM-001)', v_invoice_admin, new.administration_id;
        end if;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sales_quote_invoice_guard_trg
    before update on sales_quote
    for each row execute function sales_quote_invoice_guard();

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table sales_quote_counter owner to ledgr_migrator;
alter table sales_quote         owner to ledgr_migrator;
alter table sales_quote_line    owner to ledgr_migrator;

-- A quote is cancelled, never deleted: "we never offered that" must stay answerable.
grant select, insert, update on sales_quote_counter to ledgr_app;
grant select, insert, update on sales_quote to ledgr_app;
grant select, insert, update, delete on sales_quote_line to ledgr_app;
grant select on sales_quote_counter, sales_quote, sales_quote_line to ledgr_ops;

do $$
declare
    t text;
begin
    foreach t in array array['sales_quote_counter', 'sales_quote', 'sales_quote_line'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy %I on %I for select using (app.has_administration_access(administration_id))',
            t || '_select', t);
        execute format(
            'create policy %I on %I for insert with check (app.has_administration_access(administration_id))',
            t || '_insert', t);
    end loop;
end $$;

create policy sales_quote_counter_update on sales_quote_counter
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy sales_quote_update on sales_quote
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy sales_quote_line_update on sales_quote_line
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
create policy sales_quote_line_delete on sales_quote_line
    for delete using (app.has_administration_access(administration_id));

commit;

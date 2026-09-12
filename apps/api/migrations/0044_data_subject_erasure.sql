-- 0044_data_subject_erasure.sql
-- PRIV-021 .. PRIV-023 (PRD 10.3). See
-- docs/decisions/ADR-052-data-subject-erasure.md.
--
--   PRIV-021  Controller-initiated data subject requests (access,
--             rectification, erasure, portability, restriction) are
--             supported through tooling that completes within the statutory
--             one-month window.
--   PRIV-022  Erasure requests are honoured except where fiscal retention
--             law requires preservation. The system distinguishes the two
--             and gives a clear, specific explanation of what was retained
--             and why.
--   PRIV-023  Where erasure is blocked by retention law, the record is
--             restricted: access limited to fiscal purposes, excluded from
--             analytics and search, deleted automatically at the end of the
--             retention period.
--
-- ===========================================================================
-- Two different records, two different answers, one shared decision log
-- ===========================================================================
--
-- A customer master row (0039) carries nothing CMP-001 requires kept: an
-- invoice snapshots what it needs onto its OWN columns and never reads back
-- through this row (0039's header). So a customer's erasure request is always
-- honoured outright, on this table - see customer.erased_at below.
--
-- What CMP-001 does require kept is the ISSUED INVOICE's own frozen snapshot
-- (sales_invoice.customer_name/address/country/vat_number, 0037/0039), for
-- seven years from the end of its fiscal year - and 0039's freeze trigger
-- already forbids changing any of them. So while any of a customer's issued
-- invoices are still inside that window, the request is only PARTIALLY
-- honoured: the master record is anonymised, and the invoices are named in
-- the explanation as PRIV-022 requires, restricted rather than touched.
--
-- `data_subject_erasure_request` is where that explanation is recorded,
-- whichever resource it is about. It is insert-only - the record of what was
-- decided and why must survive exactly as long as the decision does, the same
-- reasoning 0031's document_deletion_request already applies to a document's
-- deletion.

begin;

-- ===========================================================================
-- customer: erasure is anonymisation, because there is no DELETE to be had
-- ===========================================================================
-- 0039 grants ledgr_app no DELETE on customer at all - "a customer is never
-- deleted", because statutory documents point at the row for seven years. So
-- honouring an erasure request here can only ever mean overwriting the
-- identifying columns, never removing the row, and `erased_at` is what
-- records that this happened.
alter table customer add column erased_at timestamptz;

comment on column customer.erased_at is
    'PRIV-022. Set once, by api.customers.service.CustomerService.'
    'request_erasure. The identifying columns are overwritten in the same '
    'statement - see customer_erasure_is_frozen_trg for what that guarantees.';

-- An erased customer's invoice_email is null regardless of delivery_channel -
-- there is no address left to hold it to. Restated rather than widened: the
-- ORIGINAL constraint still applies to every customer that has not been
-- erased.
alter table customer drop constraint customer_email_channel_has_an_address;
alter table customer add constraint customer_email_channel_has_an_address check (
    erased_at is not null
    or delivery_channel <> 'email'
    or invoice_email is not null
);

-- ---------------------------------------------------------------------------
-- Once erased, it stays erased
-- ---------------------------------------------------------------------------
-- Unconditional, the same shape `document_original_immutable` (0031) gives
-- the archive's original: an application bug that called `update_customer`
-- against an erased row must not be able to re-identify it, and a database
-- guard is the layer that holds even if that bug exists. `archived_at` and
-- `updated_at` are deliberately NOT in the frozen list - an erased customer
-- may still be archived, and that changes nothing about what was erased.
create or replace function customer_erasure_is_frozen() returns trigger as $$
begin
    if old.erased_at is null then
        return new;
    end if;

    if new.erased_at is distinct from old.erased_at then
        raise exception
            'customer % was erased at %; erasure cannot be undone or re-dated '
            '(PRIV-022)', old.id, old.erased_at;
    end if;

    if new.name        is distinct from old.name
    or new.trade_name   is distinct from old.trade_name
    or new.address_line1 is distinct from old.address_line1
    or new.address_line2 is distinct from old.address_line2
    or new.postal_code   is distinct from old.postal_code
    or new.city          is distinct from old.city
    or new.kvk_number    is distinct from old.kvk_number
    or new.vat_number    is distinct from old.vat_number
    or new.peppol_participant_id is distinct from old.peppol_participant_id
    or new.invoice_email is distinct from old.invoice_email
    or new.notes         is distinct from old.notes
    then
        raise exception
            'customer % was erased at % (PRIV-022): the fields an erasure '
            'clears cannot be reinstated', old.id, old.erased_at;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger customer_erasure_is_frozen_trg
    before update on customer
    for each row execute function customer_erasure_is_frozen();

-- ---------------------------------------------------------------------------
-- Erased customers stay out of the picker - PRIV-023's "excluded from search"
-- ---------------------------------------------------------------------------
-- 0039's trigram indexes, recreated with the exclusion built in rather than
-- trusted to every query that touches them - the same posture 0031's
-- document_retention_idx takes on `restricted`. api.customers.repository's
-- own WHERE clause is the belt; this is the braces.
drop index customer_name_trgm_idx;
drop index customer_trade_name_trgm_idx;

create index customer_name_trgm_idx on customer using gin (name gin_trgm_ops)
    where erased_at is null;
create index customer_trade_name_trgm_idx on customer using gin (trade_name gin_trgm_ops)
    where trade_name is not null and erased_at is null;

-- ===========================================================================
-- data_subject_erasure_request
-- ===========================================================================
-- The explanation PRIV-022 requires, as a row rather than only a log line: an
-- auditor or the data subject themselves asking "what happened to my erasure
-- request" gets an answer that outlives whatever service call produced it.
create table data_subject_erasure_request (
    id                   uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1.
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),

    -- What the request was about. Not a foreign key: a document row can be
    -- deleted by the very sweep this request anticipated (PRIV-030), and the
    -- decision record must outlive the resource it was about, the same
    -- reasoning 0031's document_deletion_request already rests on.
    resource_type        text not null check (resource_type in ('customer', 'document')),
    resource_id          uuid not null,

    requested_by_user_id uuid not null references users(id),
    requested_at         timestamptz not null default now(),

    -- PRIV-022's fork, mirroring api.privacy.model.ErasureOutcome.
    decision             text not null check (decision in ('erased', 'restricted')),

    -- PRIV-022's "clear, specific explanation of what was retained and why".
    -- A floor on length for the same reason 0031's document_deletion_request
    -- puts one on `legal_basis`: a one-word explanation reads as one and
    -- answers nothing later.
    explanation           text not null check (length(btrim(explanation)) >= 20),

    -- Set only when decision = 'restricted' - PRIV-023's "deleted
    -- automatically at the end of the retention period".
    retained_until        date,

    constraint data_subject_erasure_retained_until_matches_decision check (
        (decision = 'restricted') = (retained_until is not null)
    )
);

create index data_subject_erasure_request_resource_idx
    on data_subject_erasure_request(administration_id, resource_type, resource_id);

comment on table data_subject_erasure_request is
    'PRIV-022/023. One row per erasure request, naming what was decided and '
    'why. Insert-only - see data_subject_erasure_request_immutable_trg.';

create or replace function data_subject_erasure_request_immutable() returns trigger as $$
begin
    raise exception
        'a data subject erasure decision is the record of what was decided '
        'and why; it cannot be % (PRIV-022)', lower(tg_op);
end;
$$ language plpgsql;

create trigger data_subject_erasure_request_no_update_trg
    before update on data_subject_erasure_request
    for each row execute function data_subject_erasure_request_immutable();

create trigger data_subject_erasure_request_no_delete_trg
    before delete on data_subject_erasure_request
    for each row execute function data_subject_erasure_request_immutable();

create trigger data_subject_erasure_request_no_truncate_trg
    before truncate on data_subject_erasure_request
    for each statement execute function data_subject_erasure_request_immutable();

alter table data_subject_erasure_request owner to ledgr_migrator;

grant select, insert on data_subject_erasure_request to ledgr_app;
grant select on data_subject_erasure_request to ledgr_ops;

alter table data_subject_erasure_request enable row level security;
alter table data_subject_erasure_request force row level security;

create policy data_subject_erasure_request_select on data_subject_erasure_request
    for select using (app.has_administration_access(administration_id));
create policy data_subject_erasure_request_insert on data_subject_erasure_request
    for insert with check (app.has_administration_access(administration_id));
-- No UPDATE or DELETE policy, matching the grants above and the trigger:
-- three independent reasons a write would fail before a fourth is even
-- reached.

commit;

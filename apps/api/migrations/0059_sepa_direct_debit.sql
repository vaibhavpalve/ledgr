-- 0059_sepa_direct_debit.sql
-- FR-AR-011 (PRD 6.3), SI-09. See docs/decisions/ADR-077-sepa-direct-debit.md.
--
--   FR-AR-011  SEPA direct debit mandate management including mandate reference, signature
--              date, first/recurring flag and pain.008 file generation.
--
-- ===========================================================================
-- What is here
-- ===========================================================================
--
--   administration.sepa_creditor_id   the creditor identifier (Incassant-ID) the bank issued
--   sepa_mandate                      a customer's signed authorisation to debit an account
--   sepa_collection_batch             one generated pain.008 file, kept exactly as generated
--   sepa_collection                   one invoice inside a batch, and how it turned out
--
-- LEDGR generates the FILE; the business uploads it to its own bank. Nothing here moves
-- money, so nothing here posts to the ledger: money arrives, if it does, as a payment
-- (0052) recorded when the outcome is known, and that payment is what posts.
--
-- ===========================================================================
-- Mandates are immutable, with two one-way steps
-- ===========================================================================
--
-- A mandate is evidence of a customer's consent, so what it says never changes. It can be
-- REVOKED (once) and `last_collected_on` advances as it is used - the second drives the
-- 36-month lapse rule (a mandate unused for 36 months may no longer be collected on). A
-- customer who changes bank gets a new mandate rather than an edit of the old one. There is
-- no DELETE grant: "we had a mandate" is precisely what a disputed collection turns on.
--
-- ===========================================================================
-- First / recurring is derived, never typed
-- ===========================================================================
--
-- `sequence_type` on a collection is FRST while the mandate has no submitted or collected
-- item, RCUR after, OOFF for a one-off mandate. It is stored on the item because the file
-- said it, and is decided by the service in the same transaction as the item is written.
--
-- ===========================================================================
-- An invoice is in at most one live collection
-- ===========================================================================
--
-- `sepa_collection_live_invoice_idx` allows one `submitted` item per invoice, so two files
-- generated at the same moment cannot both ask the bank to debit the same invoice.

begin;

alter table administration
    add column sepa_creditor_id text;

comment on column administration.sepa_creditor_id is
    'SI-09: the SEPA creditor identifier (Incassant-ID) issued by the bank / Dutch Payments '
    'Association, validated (ISO 7064 mod-97 over the national part) at write time. Null '
    'means direct debit files cannot be generated for this administration.';

-- ===========================================================================
-- sepa_mandate
-- ===========================================================================
create table sepa_mandate (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    customer_id         uuid not null references customer(id),

    -- Unique per creditor (SEPA rulebook): the debtor's bank matches on it. The character
    -- set is the SEPA one, at most 35 long; checked in the rules and again here.
    mandate_reference   text not null
        check (length(mandate_reference) between 1 and 35
               and mandate_reference ~ '^[A-Za-z0-9+?/:().,''-]+$'),

    scheme              text not null check (scheme in ('core', 'b2b')),
    -- recurring: collect repeatedly (FRST then RCUR). one_off: a single collection (OOFF).
    sequence_kind       text not null check (sequence_kind in ('recurring', 'one_off')),

    signed_on           date not null,
    debtor_name         text not null check (length(btrim(debtor_name)) > 0),
    debtor_iban         text not null check (debtor_iban ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$'),
    debtor_bic          text check (debtor_bic is null or debtor_bic ~ '^[A-Z0-9]{8}([A-Z0-9]{3})?$'),

    status              text not null default 'active' check (status in ('active', 'revoked')),
    revoked_at          timestamptz,
    revoked_by_user_id  uuid references users(id),
    revoked_reason      text,
    last_collected_on   date,

    created_by_user_id  uuid not null references users(id),
    created_at          timestamptz not null default now(),

    constraint sepa_mandate_revoke_is_complete check (
        (status = 'revoked') = (revoked_at is not null)
        and (revoked_at is null) = (revoked_by_user_id is null)
    )
);

create unique index sepa_mandate_reference_idx
    on sepa_mandate(administration_id, mandate_reference);
create index sepa_mandate_customer_idx on sepa_mandate(customer_id, status);
create index sepa_mandate_organization_idx on sepa_mandate(organization_id);

comment on table sepa_mandate is
    'FR-AR-011. A customer''s signed SEPA direct debit authorisation. Immutable except for '
    'one revocation and the advance of last_collected_on; never deleted.';

create or replace function sepa_mandate_guard() returns trigger as $$
declare
    v_admin uuid;
    v_org   uuid;
begin
    select administration_id, organization_id into v_admin, v_org
      from customer where id = new.customer_id;
    if not found then
        raise exception 'customer % does not exist', new.customer_id;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'mandate belongs to administration %, but its customer belongs to % (IAM-001)',
            new.administration_id, v_admin;
    end if;
    -- A mandate cannot be signed in the future: it would authorise a debit before consent.
    if new.signed_on > current_date then
        raise exception 'a mandate cannot be signed in the future (%)', new.signed_on;
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sepa_mandate_guard_trg
    before insert on sepa_mandate
    for each row execute function sepa_mandate_guard();

create or replace function sepa_mandate_immutable() returns trigger as $$
begin
    if old.status = 'revoked' then
        raise exception 'mandate % was revoked; a revoked mandate is history', old.id;
    end if;
    if new.id                  is distinct from old.id
       or new.organization_id  is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.customer_id      is distinct from old.customer_id
       or new.mandate_reference is distinct from old.mandate_reference
       or new.scheme           is distinct from old.scheme
       or new.sequence_kind    is distinct from old.sequence_kind
       or new.signed_on        is distinct from old.signed_on
       or new.debtor_name      is distinct from old.debtor_name
       or new.debtor_iban      is distinct from old.debtor_iban
       or new.debtor_bic       is distinct from old.debtor_bic
       or new.created_by_user_id is distinct from old.created_by_user_id
       or new.created_at       is distinct from old.created_at
    then
        raise exception
            'mandate % is evidence of consent; it can only be revoked. A customer who changes '
            'bank signs a new mandate.', old.id;
    end if;
    if new.last_collected_on is distinct from old.last_collected_on
       and old.last_collected_on is not null
       and (new.last_collected_on is null or new.last_collected_on < old.last_collected_on) then
        raise exception 'last_collected_on only moves forward';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sepa_mandate_immutable_trg
    before update on sepa_mandate
    for each row execute function sepa_mandate_immutable();

-- ===========================================================================
-- sepa_collection_batch
-- ===========================================================================
create table sepa_collection_batch (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- pain.008 GrpHdr/MsgId: unique per creditor, at most 35 characters.
    message_id          text not null check (length(message_id) between 1 and 35),
    collection_date     date not null,
    item_count          integer not null check (item_count > 0),
    total_amount        numeric(14,2) not null check (total_amount > 0),

    -- The file exactly as generated, and its hash: what the bank was given must be
    -- reproducible byte for byte if a collection is disputed.
    file_xml            text not null,
    file_sha256         text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),

    created_by_user_id  uuid not null references users(id),
    created_at          timestamptz not null default now(),
    cancelled_at        timestamptz,
    cancelled_by_user_id uuid references users(id),

    constraint sepa_batch_cancel_is_complete check (
        (cancelled_at is null) = (cancelled_by_user_id is null)
    )
);

create unique index sepa_batch_message_idx on sepa_collection_batch(administration_id, message_id);
create index sepa_batch_administration_idx
    on sepa_collection_batch(administration_id, created_at desc);
create index sepa_batch_organization_idx on sepa_collection_batch(organization_id);

create or replace function sepa_batch_immutable() returns trigger as $$
begin
    if old.cancelled_at is not null then
        raise exception 'batch % was cancelled; that is history', old.id;
    end if;
    if new.id is distinct from old.id
       or new.administration_id is distinct from old.administration_id
       or new.message_id is distinct from old.message_id
       or new.collection_date is distinct from old.collection_date
       or new.item_count is distinct from old.item_count
       or new.total_amount is distinct from old.total_amount
       or new.file_xml is distinct from old.file_xml
       or new.file_sha256 is distinct from old.file_sha256
       or new.created_by_user_id is distinct from old.created_by_user_id
       or new.created_at is distinct from old.created_at
       or new.organization_id is distinct from old.organization_id
    then
        raise exception
            'batch % is the file the bank was given; only cancelling it is allowed', old.id;
    end if;
    if new.cancelled_at is null then
        raise exception 'batch % update changes nothing', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sepa_batch_immutable_trg
    before update on sepa_collection_batch
    for each row execute function sepa_batch_immutable();

-- ===========================================================================
-- sepa_collection
-- ===========================================================================
create table sepa_collection (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    batch_id            uuid not null references sepa_collection_batch(id),
    invoice_id          uuid not null references sales_invoice(id),
    mandate_id          uuid not null references sepa_mandate(id),

    amount              numeric(14,2) not null check (amount > 0),
    sequence_type       text not null check (sequence_type in ('FRST', 'RCUR', 'OOFF')),
    -- pain.008 EndToEndId, at most 35 characters; the bank returns it on the statement, which
    -- is what makes a later automatic match possible.
    end_to_end_id       text not null check (length(end_to_end_id) between 1 and 35),

    status              text not null default 'submitted'
                            check (status in ('submitted', 'collected', 'failed', 'cancelled')),
    decided_at          timestamptz,
    decided_by_user_id  uuid references users(id),
    -- ISO 20022 return reason (AC01, AM04, MS02, MD01 ...) or free text, for a failure.
    failure_reason      text,
    payment_id          uuid references sales_invoice_payment(id),

    constraint sepa_collection_decided_is_complete check (
        (status = 'submitted') = (decided_at is null)
        and (status = 'submitted') = (decided_by_user_id is null)
        and (status = 'collected') = (payment_id is not null)
    )
);

create unique index sepa_collection_e2e_idx on sepa_collection(batch_id, end_to_end_id);
create unique index sepa_collection_live_invoice_idx
    on sepa_collection(invoice_id) where status = 'submitted';
create index sepa_collection_mandate_idx on sepa_collection(mandate_id, status);
create index sepa_collection_batch_idx on sepa_collection(batch_id);
create index sepa_collection_organization_idx on sepa_collection(organization_id);

create or replace function sepa_collection_guard() returns trigger as $$
declare
    v_status      text;
    v_admin       uuid;
    v_org         uuid;
    v_credits     uuid;
    v_outstanding numeric;
    v_m_admin     uuid;
    v_m_status    text;
    v_b_admin     uuid;
begin
    -- FOR UPDATE: serialises collections, payments and write-offs against one invoice.
    select status, administration_id, organization_id, credits_invoice_id
      into v_status, v_admin, v_org, v_credits
      from sales_invoice where id = new.invoice_id
       for update;
    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'collection belongs to administration %, but its invoice belongs to % (IAM-001)',
            new.administration_id, v_admin;
    end if;
    if v_status <> 'issued' or v_credits is not null then
        raise exception 'invoice % is not an issued invoice that is owed money', new.invoice_id;
    end if;

    select administration_id, status into v_m_admin, v_m_status
      from sepa_mandate where id = new.mandate_id;
    if v_m_admin is distinct from new.administration_id then
        raise exception 'the mandate belongs to another administration (IAM-001)';
    end if;
    if v_m_status <> 'active' then
        raise exception 'mandate % is % and cannot be collected on', new.mandate_id, v_m_status;
    end if;

    select administration_id into v_b_admin from sepa_collection_batch where id = new.batch_id;
    if v_b_admin is distinct from new.administration_id then
        raise exception 'the batch belongs to another administration (IAM-001)';
    end if;

    select b.outstanding into v_outstanding
      from invoicing.invoice_balances(new.administration_id, new.invoice_id) b;
    if new.amount is distinct from coalesce(v_outstanding, 0) then
        raise exception
            'a collection of % is not the % still outstanding on invoice %',
            new.amount, coalesce(v_outstanding, 0), new.invoice_id;
    end if;

    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sepa_collection_guard_trg
    before insert on sepa_collection
    for each row execute function sepa_collection_guard();

create or replace function sepa_collection_immutable() returns trigger as $$
begin
    if old.status <> 'submitted' then
        raise exception 'collection % is % and cannot change', old.id, old.status;
    end if;
    if new.id is distinct from old.id
       or new.organization_id is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.batch_id is distinct from old.batch_id
       or new.invoice_id is distinct from old.invoice_id
       or new.mandate_id is distinct from old.mandate_id
       or new.amount is distinct from old.amount
       or new.sequence_type is distinct from old.sequence_type
       or new.end_to_end_id is distinct from old.end_to_end_id
    then
        raise exception 'collection % is what the bank was asked to do; only its outcome is '
                        'recorded', old.id;
    end if;
    if new.status = 'submitted' then
        raise exception 'collection % update changes nothing', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sepa_collection_immutable_trg
    before update on sepa_collection
    for each row execute function sepa_collection_immutable();

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table sepa_mandate owner to ledgr_migrator;
alter table sepa_collection_batch owner to ledgr_migrator;
alter table sepa_collection owner to ledgr_migrator;

-- No DELETE on any of them: consent, the file and the outcome are all evidence.
grant select, insert, update on sepa_mandate, sepa_collection_batch, sepa_collection to ledgr_app;
grant select on sepa_mandate, sepa_collection_batch, sepa_collection to ledgr_ops;

alter table sepa_mandate enable row level security;
alter table sepa_mandate force row level security;
alter table sepa_collection_batch enable row level security;
alter table sepa_collection_batch force row level security;
alter table sepa_collection enable row level security;
alter table sepa_collection force row level security;

create policy sepa_mandate_select on sepa_mandate
    for select using (app.has_administration_access(administration_id));
create policy sepa_mandate_insert on sepa_mandate
    for insert with check (app.has_administration_access(administration_id));
create policy sepa_mandate_update on sepa_mandate
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy sepa_batch_select on sepa_collection_batch
    for select using (app.has_administration_access(administration_id));
create policy sepa_batch_insert on sepa_collection_batch
    for insert with check (app.has_administration_access(administration_id));
create policy sepa_batch_update on sepa_collection_batch
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy sepa_collection_select on sepa_collection
    for select using (app.has_administration_access(administration_id));
create policy sepa_collection_insert on sepa_collection
    for insert with check (app.has_administration_access(administration_id));
create policy sepa_collection_update on sepa_collection
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

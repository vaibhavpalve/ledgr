-- 0077_bank_feed_connection.sql
-- FR-BNK-001's live bank feed (PSD2/AISP), behind the adapter api.bank.adapters has waited with
-- since 0065. See docs/decisions/ADR-108-psd2-bank-feed-behind-the-adapter.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   bank_feed_connection   one consent at an account-information provider, for one bank
--                          account: who the provider is, its id for the consent (a GoCardless
--                          requisition), which of the bank's accounts it reads, when the consent
--                          ends, and when it last synced.
--
-- The consent id is not a credential: only Boeklite's own provider secret can use it, and that
-- secret is configuration, never stored here.
--
-- Transactions the feed fetches land in bank_transaction through the SAME insert the statement
-- import uses, with the same content hash as external_id. A line fetched by the feed and the same
-- line in a statement file uploaded later therefore de-duplicate on 0065's own unique index; mixing
-- the two sources can never book a payment twice. Each sync writes one bank_statement_import row
-- with source_format 'psd2', so every fetched line still traces to the run that brought it in.
--
-- ===========================================================================
-- Lifecycle
-- ===========================================================================
--
--   pending   the person was sent to their bank to give consent
--   linked    consent given; the feed reads provider_account_id
--   expired   the consent period ended (PSD2 consents last at most 180 days); renew to continue
--   revoked   disconnected in Boeklite (the consent is also deleted at the provider)
--   failed    the bank refused, or the consent never completed
--
-- At most one pending or linked connection per bank account. Rows are never deleted: a revoked or
-- expired consent is history the audit trail refers to.
--
-- Backward compatible: a new table, and bank_statement_import's source_format check only widens.
-- Safe to apply while the previous application version is serving.
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer syncs feeds):
--   drop table bank_feed_connection;
--   delete from bank_statement_import where source_format = 'psd2'
--       and not exists (select 1 from bank_transaction t where t.import_id = bank_statement_import.id);
--   -- (rows still referenced by fetched transactions must stay; the check below cannot be
--   --  narrowed again while they exist)
--   alter table bank_statement_import drop constraint bank_statement_import_source_format_check;
--   alter table bank_statement_import add constraint bank_statement_import_source_format_check
--       check (source_format in ('csv'));

begin;

alter table bank_statement_import drop constraint bank_statement_import_source_format_check;
alter table bank_statement_import add constraint bank_statement_import_source_format_check
    check (source_format in ('csv', 'psd2'));

comment on column bank_statement_import.source_format is
    '''csv'' for an uploaded statement file of any format (CSV, CAMT.053, MT940; ADR-091), '
    '''psd2'' for one sync of the live bank feed (ADR-108).';

create table bank_feed_connection (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),
    bank_account_id      uuid not null references bank_account(id),

    provider             text not null check (provider in ('gocardless')),
    institution_id       text not null check (length(btrim(institution_id)) > 0),
    institution_name     text,
    -- The provider's id for this consent (a GoCardless requisition id).
    provider_reference   text,
    -- Which of the bank's accounts this feed reads, once consent is given.
    provider_account_id  text,

    status               text not null default 'pending'
                             check (status in ('pending', 'linked', 'expired', 'revoked', 'failed')),
    consent_expires_at   timestamptz,
    last_synced_at       timestamptz,
    -- The last sync's refusal, as an i18n reason code, cleared by the next success.
    last_error           text,

    created_at           timestamptz not null default now(),
    created_by_user_id   uuid references users(id),
    updated_at           timestamptz not null default now(),

    constraint bank_feed_connection_linked_reads_an_account check (
        status <> 'linked' or provider_account_id is not null
    )
);

create unique index bank_feed_connection_one_active_idx
    on bank_feed_connection(bank_account_id)
    where status in ('pending', 'linked');
create index bank_feed_connection_administration_idx
    on bank_feed_connection(administration_id, status);
create index bank_feed_connection_linked_idx
    on bank_feed_connection(status, last_synced_at)
    where status = 'linked';

comment on table bank_feed_connection is
    'One PSD2 consent for one bank account (ADR-108). Fetched lines go through the statement '
    'import''s own insert and de-duplicate against uploaded files.';

create or replace function bank_feed_connection_belongs_here() returns trigger as $$
declare
    v_account bank_account%rowtype;
begin
    select * into v_account from bank_account where id = new.bank_account_id;
    if not found then
        raise exception 'bank account % does not exist', new.bank_account_id;
    end if;
    if v_account.administration_id <> new.administration_id
       or v_account.organization_id <> new.organization_id then
        raise exception 'bank account % belongs to another administration', new.bank_account_id;
    end if;
    if tg_op = 'UPDATE' and (
        new.bank_account_id is distinct from old.bank_account_id
        or new.administration_id is distinct from old.administration_id
        or new.provider is distinct from old.provider
        or new.institution_id is distinct from old.institution_id
    ) then
        raise exception 'bank_feed_connection % cannot be moved to another bank or account', old.id;
    end if;
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger bank_feed_connection_belongs_here_trg
    before insert or update on bank_feed_connection
    for each row execute function bank_feed_connection_belongs_here();

alter table bank_feed_connection owner to ledgr_migrator;

-- No DELETE: a consent's history is evidence. The daily sync (scripts/sync_bank_feeds.py) only
-- READS this table as ledgr_ops to find what is due; its writes go through ledgr_app, scoped to
-- each connection's own organization, like a request.
grant select, insert, update on bank_feed_connection to ledgr_app;
grant select on bank_feed_connection to ledgr_ops;

alter table bank_feed_connection enable row level security;
alter table bank_feed_connection force row level security;

create policy bank_feed_connection_select on bank_feed_connection
    for select using (app.has_administration_access(administration_id));
create policy bank_feed_connection_insert on bank_feed_connection
    for insert with check (app.has_administration_access(administration_id));
create policy bank_feed_connection_update on bank_feed_connection
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

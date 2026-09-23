-- 0065_bank_accounts.sql
-- The Bank screen (`/bank`) - PRD's bank feed/reconciliation, the fourth ComingSoon stub named by
-- the gap analysis against Exact Online/Yuki. See docs/decisions/ADR-084-bank-accounts-statement-
-- import-and-reconciliation.md.
--
-- ===========================================================================
-- What is here, and what is deliberately not
-- ===========================================================================
--
--   bank_account            one row per bank/cash account, pointing at the ledger asset account
--                           it represents.
--   bank_statement_import   one row per uploaded statement file.
--   bank_transaction        one imported line, matched and reconciled or not.
--
-- No PSD2/AISP integration is built (see ADR-084 and api.bank.adapters) - this is CSV import and
-- manual reconciliation, the P0 substitute the same way ADR-081 gated automatic invoice reading
-- behind an adapter defaulting to "none". Reconciliation posts through LedgerService.post() and,
-- for an invoice match, api.invoicing.payments.SalesPaymentService.record() - both already-public
-- entry points, not a new write path into the ledger.
--
-- ===========================================================================
-- Import is idempotent per bank account
-- ===========================================================================
--
-- `external_id` is a hash of the imported line's own content, computed at import time
-- (api.bank.service). Re-uploading the same statement is then a no-op rather than a duplicate -
-- `bank_transaction_dedup_idx` is what makes a second attempt fail cleanly rather than double an
-- account's transactions.
--
-- ===========================================================================
-- What may change after import
-- ===========================================================================
--
-- Everything about an imported line except its reconciliation state is fixed at insert - a bank
-- statement is evidence, the same posture a posted journal entry takes. Only status, the match it
-- was reconciled against, and who/when may change, and the guard trigger below is what keeps that
-- true for every writer, not only the service that is disciplined about it today.

begin;

create table bank_account (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    name                text not null check (length(btrim(name)) > 0),
    iban                text,
    currency            text not null default 'EUR' check (currency ~ '^[A-Z]{3}$'),

    -- The ledger asset account this bank account's balance IS. Every reconciled transaction
    -- posts a line against this account.
    ledger_account_id   uuid not null references ledger_account(id),

    status              text not null default 'active' check (status in ('active', 'archived')),

    created_at          timestamptz not null default now(),
    created_by_user_id  uuid references users(id)
);

create index bank_account_administration_idx on bank_account(administration_id, status);
create index bank_account_organization_idx on bank_account(organization_id);
create unique index bank_account_one_per_ledger_account_idx
    on bank_account(ledger_account_id)
    where status = 'active';

comment on index bank_account_one_per_ledger_account_idx is
    'Two active bank accounts pointing at the same ledger account would make every reconciled '
    'transaction ambiguous about which bank account it belongs to.';

create or replace function bank_account_belongs_here() returns trigger as $$
declare
    v_account ledger_account%rowtype;
begin
    select * into v_account from ledger_account where id = new.ledger_account_id;
    if not found then
        raise exception 'ledger account % does not exist', new.ledger_account_id;
    end if;
    if v_account.administration_id <> new.administration_id then
        raise exception
            'ledger account % belongs to another administration', new.ledger_account_id;
    end if;
    if v_account.account_type <> 'asset' then
        raise exception
            'ledger account % is %, but a bank account''s ledger account must be an asset',
            new.ledger_account_id, v_account.account_type;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger bank_account_belongs_here_trg
    before insert or update on bank_account
    for each row execute function bank_account_belongs_here();

alter table bank_account owner to ledgr_migrator;
grant select, insert, update on bank_account to ledgr_app;
grant select on bank_account to ledgr_ops;

alter table bank_account enable row level security;
alter table bank_account force row level security;

create policy bank_account_select on bank_account
    for select using (app.has_administration_access(administration_id));
create policy bank_account_insert on bank_account
    for insert with check (app.has_administration_access(administration_id));
create policy bank_account_update on bank_account
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ===========================================================================
-- bank_statement_import
-- ===========================================================================
create table bank_statement_import (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),
    bank_account_id      uuid not null references bank_account(id),

    source_format        text not null check (source_format in ('csv')),
    filename             text,
    transaction_count    integer not null default 0,
    duplicate_count      integer not null default 0,

    imported_at          timestamptz not null default now(),
    imported_by_user_id  uuid references users(id)
);

create index bank_statement_import_account_idx
    on bank_statement_import(bank_account_id, imported_at);

alter table bank_statement_import owner to ledgr_migrator;
grant select, insert on bank_statement_import to ledgr_app;
grant select on bank_statement_import to ledgr_ops;

alter table bank_statement_import enable row level security;
alter table bank_statement_import force row level security;

create policy bank_statement_import_select on bank_statement_import
    for select using (app.has_administration_access(administration_id));
create policy bank_statement_import_insert on bank_statement_import
    for insert with check (app.has_administration_access(administration_id));

-- ===========================================================================
-- bank_transaction
-- ===========================================================================
create table bank_transaction (
    id                        uuid primary key default gen_random_uuid(),
    organization_id           uuid not null references organization(id),
    administration_id         uuid not null references administration(id),
    bank_account_id           uuid not null references bank_account(id),
    import_id                 uuid references bank_statement_import(id),

    booking_date              date not null,
    value_date                date,
    -- Signed: positive is money IN (a credit to the bank account), negative is money OUT.
    -- Direction is a sign here rather than split debit/credit columns, because this is a bank
    -- statement line before it is a posting - `api.bank.service` is what turns it into one.
    amount                    numeric(19, 2) not null check (amount <> 0),
    currency                  text not null default 'EUR' check (currency ~ '^[A-Z]{3}$'),

    counterparty_name         text,
    counterparty_iban         text,
    description               text,

    -- A hash of this line's own content (api.bank.service) - what makes re-importing the same
    -- statement a no-op rather than a duplicate.
    external_id               text not null,

    status                    text not null default 'unmatched'
                                  check (status in ('unmatched', 'reconciled')),
    matched_sales_invoice_id  uuid references sales_invoice(id),
    matched_payment_id        uuid references sales_invoice_payment(id),
    journal_entry_id          uuid references journal_entry(id),
    reconciled_at             timestamptz,
    reconciled_by_user_id     uuid references users(id),

    created_at                timestamptz not null default now(),

    constraint bank_transaction_reconciliation_is_complete check (
        (status = 'reconciled') = (journal_entry_id is not null)
        and (status = 'reconciled') = (reconciled_at is not null)
    )
);

create unique index bank_transaction_dedup_idx on bank_transaction(bank_account_id, external_id);
create index bank_transaction_administration_idx
    on bank_transaction(administration_id, status);
create index bank_transaction_account_idx
    on bank_transaction(bank_account_id, booking_date desc);

comment on table bank_transaction is
    'One imported statement line. Reconciliation posts through LedgerService.post() (generic) or '
    'SalesPaymentService.record() (matched to an invoice) - this table records the OUTCOME, it is '
    'not itself a posting path.';

create or replace function bank_transaction_immutable_once_imported() returns trigger as $$
begin
    if new.booking_date       is distinct from old.booking_date
       or new.value_date      is distinct from old.value_date
       or new.amount          is distinct from old.amount
       or new.currency        is distinct from old.currency
       or new.counterparty_name is distinct from old.counterparty_name
       or new.counterparty_iban is distinct from old.counterparty_iban
       or new.description     is distinct from old.description
       or new.external_id     is distinct from old.external_id
       or new.bank_account_id is distinct from old.bank_account_id
       or new.import_id       is distinct from old.import_id
    then
        raise exception
            'bank_transaction % is the imported statement line and cannot be edited; only its '
            'reconciliation state may change', old.id;
    end if;
    if old.status = 'reconciled' and new.status <> 'reconciled' then
        raise exception
            'bank_transaction % has already been reconciled; correct it with a reversing entry, '
            'not by unreconciling', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger bank_transaction_immutable_once_imported_trg
    before update on bank_transaction
    for each row execute function bank_transaction_immutable_once_imported();

alter table bank_transaction owner to ledgr_migrator;

-- No DELETE: an imported statement line is evidence. UPDATE is granted so the service can move a
-- row from unmatched to reconciled; the trigger above is what stops it being anything else.
grant select, insert, update on bank_transaction to ledgr_app;
grant select on bank_transaction to ledgr_ops;

alter table bank_transaction enable row level security;
alter table bank_transaction force row level security;

create policy bank_transaction_select on bank_transaction
    for select using (app.has_administration_access(administration_id));
create policy bank_transaction_insert on bank_transaction
    for insert with check (app.has_administration_access(administration_id));
create policy bank_transaction_update on bank_transaction
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

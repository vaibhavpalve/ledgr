-- 0071_bank_batched_payment_allocation.sql
-- One bank line settling several invoices at once (FR-BNK-005's batched-payment shape). See
-- docs/decisions/ADR-098-batched-payment-allocation.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
-- bank_transaction_allocation: one row per (bank line, invoice) pair in a batched payment - a
-- customer's single transfer covering several invoices. Each row carries its own amount, its own
-- SalesPaymentService.record() payment and the journal entry that payment posted - there is no
-- new write path into the ledger, only a new way to call the existing one more than once for a
-- single bank line (api.bank.service.BankService.reconcile_with_invoices).
--
-- The single-invoice match (reconcile_with_invoice, ADR-091/097) is unchanged and untouched: it
-- still writes matched_sales_invoice_id/matched_payment_id/journal_entry_id directly on
-- bank_transaction and never touches this table. A BATCHED reconciliation instead leaves those
-- first two columns null - journal_entry_id points at the FIRST of the batch's entries, because
-- bank_transaction's own CHECK constraint requires it non-null once reconciled, but the full set
-- of what was paid lives in bank_transaction_allocation, not in a single column that could never
-- represent more than one invoice.
--
-- ===========================================================================
-- Why the amount is not derived
-- ===========================================================================
--
-- Each allocation's amount is what THAT invoice's payment recorded - not necessarily the
-- invoice's own outstanding balance, since an allocation may itself be a partial payment of that
-- invoice (same shape as ADR-097's single-line case, now inside a batch). The service validates
-- the whole batch's amounts sum to exactly the bank line's before posting anything, but that is an
-- application check, not a schema one: a SQL CHECK cannot see sibling rows, and the invariant that
-- matters - "this table's rows for one bank line sum to that line's amount" - is enforced once, in
-- Python, before the first SalesPaymentService.record() call, not something worth a deferred
-- constraint trigger for a check that runs exactly once per reconciliation.
--
-- Tenancy: organization_id/administration_id carried directly (not joined through
-- bank_transaction), same as every other tenant-scoped table - RLS does not follow foreign keys.
-- Ledger: no new write path; every entry referenced here was posted by
-- SalesPaymentService.record(), which already goes through LedgerService.post().
--
-- NFR-044: backward compatible (a new table only), no downtime. Reversible:
--   drop table bank_transaction_allocation;
-- Reversing loses the per-invoice breakdown of any batched reconciliation already recorded, but
-- the ledger entries themselves are untouched (this table records what was allocated, it is not
-- itself a posting).

begin;

create table bank_transaction_allocation (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),
    bank_transaction_id   uuid not null references bank_transaction(id),
    sales_invoice_id      uuid not null references sales_invoice(id),
    payment_id            uuid not null references sales_invoice_payment(id),
    journal_entry_id      uuid not null references journal_entry(id),
    amount                numeric(19, 2) not null check (amount > 0),

    created_at            timestamptz not null default now(),
    created_by_user_id    uuid references users(id),

    -- One allocation per (line, invoice): the same invoice cannot appear twice in one batch.
    unique (bank_transaction_id, sales_invoice_id)
);

create index bank_transaction_allocation_transaction_idx
    on bank_transaction_allocation(bank_transaction_id);
create index bank_transaction_allocation_invoice_idx
    on bank_transaction_allocation(sales_invoice_id);
create index bank_transaction_allocation_administration_idx
    on bank_transaction_allocation(administration_id);

comment on table bank_transaction_allocation is
    'FR-BNK-005. One row per invoice a batched bank line paid. Insert-only, like the ledger '
    'entries it points at - correcting a batched reconciliation is a reversing entry (FR-GL-003), '
    'not an edit here.';

alter table bank_transaction_allocation owner to ledgr_migrator;

-- No UPDATE, no DELETE: append-only, the same posture bank_transaction takes once reconciled.
grant select, insert on bank_transaction_allocation to ledgr_app;
grant select on bank_transaction_allocation to ledgr_ops;

alter table bank_transaction_allocation enable row level security;
alter table bank_transaction_allocation force row level security;

create policy bank_transaction_allocation_select on bank_transaction_allocation
    for select using (app.has_administration_access(administration_id));
create policy bank_transaction_allocation_insert on bank_transaction_allocation
    for insert with check (app.has_administration_access(administration_id));

commit;

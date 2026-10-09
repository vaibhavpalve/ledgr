-- 0079_booking_proposal.sql
-- FR-BNK-003/004's "propose", for the firm home's auto-bookings review (FR-FRM-002). See
-- docs/decisions/ADR-110-auto-bookings-are-proposals.md and docs/firm-home/contract.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   booking_proposal   one suggestion that an unmatched bank line is settled by one open
--                      document (a bank-paid receipt or a sales invoice), made when matching
--                      (api.bank.matching) finds a single certain HIGH candidate for it.
--
-- A proposal is OUTSIDE the ledger. Nothing here posts, and nothing that has been posted is ever
-- flagged, edited or deleted by this table. Approving a proposal books it through the existing
-- bank reconciliation path (api.bank.service: SalesPaymentService.record() or LedgerService.post());
-- rejecting it changes only this row.
--
-- ===========================================================================
-- Lifecycle
-- ===========================================================================
--
--   pending     waiting for a person's decision
--   approved    a person approved it and the bank line was reconciled in the same transaction
--   rejected    a person rejected it; the ledger was not touched
--   superseded  the system withdrew it: the line was reconciled some other way, the document was
--               taken by another line, or matching no longer finds it certain
--
-- Status moves pending -> one of the other three, once, and never back (trigger below). At most one
-- pending proposal per bank line (partial unique index). Rows are never deleted: a decision is
-- history the audit trail refers to.
--
-- `document_kind` is an addition to the contract's column list: a proposal must say whether
-- document_id names a sales invoice or an expense, because approving each takes a different path.
--
-- Backward compatible: a new table only. The application treats proposal generation as advisory
-- and runs it in a savepoint, so a build that is deployed before this migration is applied keeps
-- importing statements and posting receipts exactly as before.
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer reads or writes proposals):
--   drop trigger booking_proposal_guard_trg on booking_proposal;
--   drop function booking_proposal_guard();
--   drop table booking_proposal;

begin;

create table booking_proposal (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),

    bank_transaction_id  uuid references bank_transaction(id),
    -- The sales invoice's or the expense's id; which one is `document_kind`.
    document_id          uuid,
    document_kind        text check (document_kind in ('sales_invoice', 'expense')),

    proposal_kind        text not null default 'bank_match'
                             check (proposal_kind in ('bank_match')),
    confidence           text not null check (confidence in ('high', 'medium')),
    status               text not null default 'pending'
                             check (status in ('pending', 'approved', 'rejected', 'superseded')),

    -- Normalised counterparty + target account code, so the review sheet can group
    -- "KPN -> 4500 Telefoon" across clients. Built by api.firm.proposals.group_key.
    group_key            text not null check (length(btrim(group_key)) > 0),
    counterparty         text,
    account_code         text,
    -- NFR-031: decimal, never float. What the line pays toward the document - always positive,
    -- whichever direction the money moved.
    amount               numeric(18, 2) not null check (amount > 0),

    created_at           timestamptz not null default now(),
    decided_at           timestamptz,
    -- NULL for a proposal the system superseded.
    decided_by_user_id   uuid references users(id),

    constraint booking_proposal_decided_once_decided check (
        (status = 'pending') = (decided_at is null)
    ),
    constraint booking_proposal_document_named check (
        (document_id is null) = (document_kind is null)
    )
);

create unique index booking_proposal_one_pending_idx
    on booking_proposal(bank_transaction_id)
    where status = 'pending';
create index booking_proposal_administration_idx
    on booking_proposal(administration_id, status);
create index booking_proposal_pending_group_idx
    on booking_proposal(group_key)
    where status = 'pending';
create index booking_proposal_document_idx
    on booking_proposal(document_id)
    where status = 'pending';

comment on table booking_proposal is
    'A suggested bank match, outside the ledger (ADR-110). Approving books it through the bank '
    'reconciliation path; rejecting changes only this row. Status moves from pending once.';

-- The line belongs to this administration, and a proposal only ever moves from pending, once. Every
-- other column is what was proposed and stays as it was proposed.
create or replace function booking_proposal_guard() returns trigger as $$
declare
    v_transaction bank_transaction%rowtype;
begin
    if tg_op = 'INSERT' then
        if new.status <> 'pending' then
            raise exception 'a booking_proposal is created pending';
        end if;
        if new.bank_transaction_id is not null then
            select * into v_transaction from bank_transaction where id = new.bank_transaction_id;
            if not found then
                raise exception 'bank transaction % does not exist', new.bank_transaction_id;
            end if;
            if v_transaction.administration_id <> new.administration_id
               or v_transaction.organization_id <> new.organization_id then
                raise exception 'bank transaction % belongs to another administration',
                    new.bank_transaction_id;
            end if;
        end if;
        return new;
    end if;

    if old.status <> 'pending' then
        raise exception 'booking_proposal % was already % and cannot change', old.id, old.status;
    end if;
    if new.status = 'pending' then
        raise exception 'booking_proposal % may only move from pending to a decision', old.id;
    end if;
    if new.id                     is distinct from old.id
       or new.organization_id     is distinct from old.organization_id
       or new.administration_id   is distinct from old.administration_id
       or new.bank_transaction_id is distinct from old.bank_transaction_id
       or new.document_id         is distinct from old.document_id
       or new.document_kind       is distinct from old.document_kind
       or new.proposal_kind       is distinct from old.proposal_kind
       or new.confidence          is distinct from old.confidence
       or new.group_key           is distinct from old.group_key
       or new.counterparty        is distinct from old.counterparty
       or new.account_code        is distinct from old.account_code
       or new.amount              is distinct from old.amount
       or new.created_at          is distinct from old.created_at
    then
        raise exception 'booking_proposal % is what was proposed; only its decision may change',
            old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger booking_proposal_guard_trg
    before insert or update on booking_proposal
    for each row execute function booking_proposal_guard();

alter table booking_proposal owner to ledgr_migrator;

-- No DELETE: a decision is evidence. UPDATE is granted so a proposal can be decided; the trigger
-- above is what stops it being anything else. ledgr_ops reads it for the backfill
-- (api.firm.proposals_job), which writes as ledgr_app like a request.
grant select, insert, update on booking_proposal to ledgr_app;
grant select on booking_proposal to ledgr_ops;

alter table booking_proposal enable row level security;
alter table booking_proposal force row level security;

create policy booking_proposal_select on booking_proposal
    for select using (app.has_administration_access(administration_id));
create policy booking_proposal_insert on booking_proposal
    for insert with check (app.has_administration_access(administration_id));
create policy booking_proposal_update on booking_proposal
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

-- 0082_booking_rule.sql
-- FR-BNK-004 / FR-BNK-006: approval rules - "always do this" for one client's certain matches.
-- See docs/decisions/ADR-113-booking-rules-auto-approve-through-the-proposal-path.md and
-- docs/firm-home/contract-wave2.md decisions 1-4.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   booking_rule              one administration's standing approval: a certain HIGH proposal
--                             whose normalised counterparty and target account equal the rule's
--                             (and whose amount is at most max_amount, when set) is approved
--                             without waiting for a person - through exactly the wave-1 approve
--                             path (ADR-110), never a new one.
--   booking_proposal.rule_id  the rule that approved a proposal; NULL for every proposal a person
--                             decided or the system superseded.
--
-- A rule is per ADMINISTRATION (FR-BNK-006: never across tenants). Approving a group spanning nine
-- clients with "Always do this" writes nine rows. A rule posts nothing by itself: it only ever
-- approves a booking_proposal, and a proposal only ever exists for a single certain match of a bank
-- line to an existing document.
--
-- ===========================================================================
-- Lifecycle
-- ===========================================================================
--
--   active      applied at proposal generation
--   suspended   skipped: its creator no longer holds `reconcile bank_transaction` on the
--               administration (checked at apply time against current state); the reason is kept
--   retired     switched off by a person; final
--
-- Status moves active -> suspended -> active, and any -> retired (trigger below). Every other column
-- except max_amount is what was created. Rows are never deleted (no DELETE grant): a rule is the
-- standing authority that rule-posted bookings refer to.
--
-- At most one non-retired rule per (administration, counterparty_key, account_code) (partial
-- unique index), so re-approving with "remember" reactivates or keeps the one rule.
--
-- booking_proposal's 0079 guard is replaced by one that also covers rule_id: NULL on insert, and set
-- only in the same update that moves a proposal from pending to approved. Everything 0079's guard
-- enforced is enforced unchanged.
--
-- Backward compatible: a new table and a nullable column. Code from before this migration never
-- reads or writes either. The application treats rule creation and auto-approval as advisory and
-- runs them in their own savepoints, so a build deployed before this migration is applied keeps
-- importing and approving exactly as before (NFR-044).
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer reads or writes rules):
--   -- restore 0079's guard: re-run 0079's `create or replace function booking_proposal_guard()`
--   -- statement verbatim (the trigger itself is unchanged and stays)
--   alter table booking_proposal drop column rule_id;
--   drop trigger booking_rule_guard_trg on booking_rule;
--   drop function booking_rule_guard();
--   drop table booking_rule;

begin;

create table booking_rule (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),

    -- api.firm.proposals.normalise_counterparty() output: "KPN B.V." and "kpn bv" are "kpn".
    counterparty_key     text not null check (length(btrim(counterparty_key)) > 0),
    account_code         text not null check (length(btrim(account_code)) > 0),
    -- NFR-031: decimal, never float. A match above it stays a normal pending proposal.
    max_amount           numeric(18, 2) check (max_amount is null or max_amount > 0),

    status               text not null default 'active'
                             check (status in ('active', 'suspended', 'retired')),
    suspended_reason     text,

    created_by_user_id   uuid not null references users(id),
    created_at           timestamptz not null default now(),
    retired_by_user_id   uuid references users(id),
    retired_at           timestamptz,

    constraint booking_rule_suspended_has_reason check (
        (status = 'suspended') = (suspended_reason is not null)
    ),
    constraint booking_rule_retired_once check (
        (status = 'retired') = (retired_at is not null)
    )
);

create unique index booking_rule_one_live_idx
    on booking_rule(administration_id, counterparty_key, account_code)
    where status <> 'retired';
create index booking_rule_administration_idx
    on booking_rule(administration_id, status);

comment on table booking_rule is
    'A per-administration standing approval of certain bank matches (ADR-113). It only ever '
    'approves a booking_proposal through the wave-1 approve path; it never posts by itself.';

create or replace function booking_rule_guard() returns trigger as $$
declare
    v_owner uuid;
begin
    if tg_op = 'INSERT' then
        if new.status <> 'active' then
            raise exception 'a booking_rule is created active';
        end if;
        if new.retired_by_user_id is not null or new.retired_at is not null then
            raise exception 'a booking_rule is not created retired';
        end if;
        select organization_id into v_owner from administration where id = new.administration_id;
        if not found or v_owner <> new.organization_id then
            raise exception 'booking_rule organization must own administration %',
                new.administration_id;
        end if;
        return new;
    end if;

    if new.id                  is distinct from old.id
       or new.organization_id    is distinct from old.organization_id
       or new.administration_id  is distinct from old.administration_id
       or new.counterparty_key   is distinct from old.counterparty_key
       or new.account_code       is distinct from old.account_code
       or new.created_by_user_id is distinct from old.created_by_user_id
       or new.created_at         is distinct from old.created_at
    then
        raise exception 'booking_rule % is what was created; only its status and cap may change',
            old.id;
    end if;

    if old.status = 'retired' then
        raise exception 'booking_rule % is retired and cannot change', old.id;
    end if;
    -- active -> active (a new cap), active <-> suspended, active|suspended -> retired.
    if new.status = 'retired' then
        if new.retired_by_user_id is null then
            raise exception 'retiring booking_rule % needs who retired it', old.id;
        end if;
    elsif new.retired_by_user_id is not null or new.retired_at is not null then
        raise exception 'booking_rule % is not retired', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger booking_rule_guard_trg
    before insert or update on booking_rule
    for each row execute function booking_rule_guard();

alter table booking_rule owner to ledgr_migrator;

-- No DELETE: a rule is the authority its postings refer to. UPDATE is granted so a rule can be
-- suspended, reactivated, capped or retired; the trigger above is what stops anything else.
grant select, insert, update on booking_rule to ledgr_app;
grant select on booking_rule to ledgr_ops;

alter table booking_rule enable row level security;
alter table booking_rule force row level security;

create policy booking_rule_select on booking_rule
    for select using (app.has_administration_access(administration_id));
create policy booking_rule_insert on booking_rule
    for insert with check (app.has_administration_access(administration_id));
create policy booking_rule_update on booking_rule
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ---------------------------------------------------------------------------
-- booking_proposal.rule_id
-- ---------------------------------------------------------------------------

alter table booking_proposal add column rule_id uuid references booking_rule(id);

create index booking_proposal_rule_idx
    on booking_proposal(rule_id)
    where rule_id is not null;

-- 0079's guard, plus rule_id: NULL when proposed, set only by the decision that approves it, and
-- only to a rule of the same administration. Every check 0079 made is made unchanged.
create or replace function booking_proposal_guard() returns trigger as $$
declare
    v_transaction bank_transaction%rowtype;
    v_rule_admin  uuid;
begin
    if tg_op = 'INSERT' then
        if new.status <> 'pending' then
            raise exception 'a booking_proposal is created pending';
        end if;
        if new.rule_id is not null then
            raise exception 'a booking_proposal is created without a rule';
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
    if new.rule_id is not null then
        if new.status <> 'approved' then
            raise exception 'only an approval of booking_proposal % can name a rule', old.id;
        end if;
        select administration_id into v_rule_admin from booking_rule where id = new.rule_id;
        if not found or v_rule_admin <> new.administration_id then
            raise exception 'booking_rule % belongs to another administration', new.rule_id;
        end if;
    end if;
    return new;
end;
$$ language plpgsql;

commit;

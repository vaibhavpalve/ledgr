-- 0078_firm_worklist.sql
-- The accountant's firm home ("week-back view"): FR-FRM-001 (portfolio status), FR-FRM-002 (the
-- cross-client work queue), FR-FRM-004b (bulk assignment, partial). See
-- docs/decisions/ADR-109-firm-worklist-cross-client-reads.md and docs/firm-home/contract.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   users.last_login_at          when this person last opened a session (any device)
--   users.previous_login_at      the one before that - the login hook shifts last -> previous
--   users.firm_activity_seen_at  when they last said "I've read the week-back summary"
--                                (POST /v1/firm/summary/seen). The summary's "since" window is
--                                this, falling back to previous_login_at, then seven days.
--
--   client_assignment   who at the firm looks after a client. Append-only; the CURRENT
--                       assignment is the latest row per administration, and a row with
--                       assigned_user_id NULL clears it.
--   client_snooze       "not this client until <date>". Append-only; current = latest row per
--                       administration, and snoozed_until NULL clears it.
--
-- Neither table touches the books, and neither grants anything: an assignment is a label on the
-- work queue, never a role_assignment. Access is still only what IAM-107's grants give.
--
-- ===========================================================================
-- Append-only
-- ===========================================================================
--
-- No UPDATE or DELETE grant on either table, and a trigger refusing both for any role that has one
-- anyway - the same belt-and-braces 0068 gives vat_return. A reassignment or an un-snooze is a new
-- row, so "who had this client, and since when" stays answerable.
--
-- ===========================================================================
-- Indexes for the worklist's predicates
-- ===========================================================================
--
-- The worklist reads every granted administration at once (set-based, ADR-109), so the predicates
-- it filters on need indexes that start with administration_id:
--
--   expense(administration_id, gross_amount)          "is there a receipt for this debit?"
--   bank_transaction(administration_id, booking_date)  unmatched lines, by date (partial)
--   role_assignment(granted_by_organization_id)        the firm's staff list
--
-- The rest already exist: bank_transaction(administration_id, status) (0065),
-- bank_transaction(matched_expense_id) (0070), expense(administration_id, status) and
-- (administration_id, created_at desc) (0032), expense_duplicate_exact_idx (0036),
-- journal_entry(period_id) (0020), vat_return(period_id) (0068), period(administration_id) (0001),
-- bank_feed_connection(administration_id, status) (0077), booking_proposal(administration_id,
-- status) and (document_id) (0079).
--
-- ===========================================================================
-- Backward compatible, and reversible (NFR-044)
-- ===========================================================================
--
-- Three nullable columns with no default (a catalogue-only change, no table rewrite), two new
-- tables and three indexes. Nothing existing changes shape, so the previous application version
-- keeps working while this is applied. The login hook (api.auth.repository) writes the two login
-- columns in a savepoint and never fails a sign-in over them, so a build deployed before this
-- migration still signs people in.
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer reads the firm home):
--   drop trigger client_assignment_is_history_trg on client_assignment;
--   drop trigger client_snooze_is_history_trg on client_snooze;
--   drop function firm_worklist_history_is_final();
--   drop table client_assignment;
--   drop table client_snooze;
--   drop index expense_administration_amount_idx;
--   drop index bank_transaction_unmatched_by_date_idx;
--   drop index role_assignment_granted_by_org_idx;
--   alter table users drop column last_login_at,
--                     drop column previous_login_at,
--                     drop column firm_activity_seen_at;

begin;

-- ---------------------------------------------------------------------------
-- users: the "since" window
-- ---------------------------------------------------------------------------
-- `users` carries no RLS (users are global - 0003); every read and write of these columns names
-- its own `WHERE id = <the verified token's user>`. The table-level grants 0003 gives ledgr_app
-- cover new columns.
alter table users
    add column last_login_at          timestamptz,
    add column previous_login_at      timestamptz,
    add column firm_activity_seen_at  timestamptz;

comment on column users.last_login_at is
    'When this person last opened a session. Set by the login hook (ADR-109).';
comment on column users.previous_login_at is
    'The session before last_login_at - the firm home''s default "since" (ADR-109).';
comment on column users.firm_activity_seen_at is
    'When this person last marked the firm home''s summary as read (POST /v1/firm/summary/seen). '
    'Takes precedence over previous_login_at, so peeking on a phone does not reset the window.';

-- ---------------------------------------------------------------------------
-- client_assignment
-- ---------------------------------------------------------------------------
create table client_assignment (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),

    -- NULL clears the assignment. Not a grant: who may open the client is still role_assignment.
    assigned_user_id      uuid references users(id),
    assigned_by_user_id   uuid not null references users(id),

    created_at            timestamptz not null default now()
);

create index client_assignment_current_idx
    on client_assignment(administration_id, created_at desc);
create index client_assignment_organization_idx on client_assignment(organization_id);

comment on table client_assignment is
    'FR-FRM-004b. Who at the firm looks after a client, for the work queue. Append-only; the '
    'current assignment is the latest row per administration. Grants nothing (ADR-109).';

-- ---------------------------------------------------------------------------
-- client_snooze
-- ---------------------------------------------------------------------------
create table client_snooze (
    id                    uuid primary key default gen_random_uuid(),
    organization_id       uuid not null references organization(id),
    administration_id     uuid not null references administration(id),

    -- NULL clears the snooze. A date, not a timestamp: "until the 15th" is a day.
    snoozed_until         date,
    reason                text check (reason is null or length(reason) <= 500),

    created_by_user_id    uuid not null references users(id),
    created_at            timestamptz not null default now()
);

create index client_snooze_current_idx
    on client_snooze(administration_id, created_at desc);
create index client_snooze_organization_idx on client_snooze(organization_id);

comment on table client_snooze is
    'FR-FRM-002. "Not this client until <date>" on the work queue. Append-only; current = latest '
    'row per administration, snoozed_until NULL clears it (ADR-109).';

-- ---------------------------------------------------------------------------
-- Both tables are history
-- ---------------------------------------------------------------------------
create or replace function firm_worklist_history_is_final() returns trigger as $$
begin
    raise exception '% is append-only; record the change as a new row', tg_table_name;
end;
$$ language plpgsql;

create trigger client_assignment_is_history_trg
    before update or delete on client_assignment
    for each row execute function firm_worklist_history_is_final();
create trigger client_snooze_is_history_trg
    before update or delete on client_snooze
    for each row execute function firm_worklist_history_is_final();

alter table client_assignment owner to ledgr_migrator;
alter table client_snooze owner to ledgr_migrator;

grant select, insert on client_assignment to ledgr_app;
grant select, insert on client_snooze to ledgr_app;
grant select on client_assignment to ledgr_ops;
grant select on client_snooze to ledgr_ops;

alter table client_assignment enable row level security;
alter table client_assignment force row level security;
alter table client_snooze enable row level security;
alter table client_snooze force row level security;

create policy client_assignment_select on client_assignment
    for select using (app.has_administration_access(administration_id));
create policy client_assignment_insert on client_assignment
    for insert with check (app.has_administration_access(administration_id));

create policy client_snooze_select on client_snooze
    for select using (app.has_administration_access(administration_id));
create policy client_snooze_insert on client_snooze
    for insert with check (app.has_administration_access(administration_id));

-- ---------------------------------------------------------------------------
-- Indexes for the worklist (see the header)
-- ---------------------------------------------------------------------------
create index expense_administration_amount_idx
    on expense(administration_id, gross_amount)
    where gross_amount is not null;

create index bank_transaction_unmatched_by_date_idx
    on bank_transaction(administration_id, booking_date)
    where status = 'unmatched';

create index role_assignment_granted_by_org_idx
    on role_assignment(granted_by_organization_id)
    where revoked_at is null;

commit;

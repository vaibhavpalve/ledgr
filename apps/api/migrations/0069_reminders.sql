-- 0069_reminders.sql
-- Reminder e-mails: a BTW return coming due or overdue, and receipts waiting to be booked.
-- See docs/decisions/ADR-090-reminder-emails.md.
--
-- users.reminder_emails   the person's own opt-out, set from Settings > Profile.
-- reminder_log            one row per reminder SENT, unique per (administration, user, kind,
--                         key), so the daily job can run any number of times a day and a person
--                         still gets each reminder once. The job inserts the row in the same
--                         transaction as it hands the mail over; a send that fails rolls the row
--                         back and the next run tries again.
--
-- The job runs as ledgr_ops (BYPASSRLS), like the document-retention sweep: it has to see every
-- tenant's due dates. ledgr_app only reads the log (nothing in the product writes it).

begin;

alter table users
    add column reminder_emails boolean not null default true;

comment on column users.reminder_emails is
    'ADR-090. Whether this person receives reminder e-mails (BTW deadlines, receipts waiting). '
    'Set by the person themselves only.';

create table reminder_log (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    user_id             uuid not null references users(id),
    kind                text not null check (kind in ('vat_due', 'vat_overdue', 'receipts_waiting')),
    reminder_key        text not null check (length(btrim(reminder_key)) > 0),
    sent_at             timestamptz not null default now()
);

create unique index reminder_log_once_idx
    on reminder_log(administration_id, user_id, kind, reminder_key);
create index reminder_log_organization_idx on reminder_log(organization_id);

comment on table reminder_log is
    'ADR-090. Every reminder e-mail sent, once per (administration, user, kind, key).';

alter table reminder_log owner to ledgr_migrator;
grant select, insert on reminder_log to ledgr_ops;
grant select on reminder_log to ledgr_app;

-- What the job reads, column by column: ledgr_ops is narrowly granted by design (0002), so it gets
-- exactly the columns that decide who is reminded of what, and nothing else - no password hash,
-- no amount, no description.
grant select (id, email, language, status, email_verified_at, reminder_emails) on users to ledgr_ops;
grant select (user_id, role_id, scope_type, scope_id, revoked_at, expires_at)
    on role_assignment to ledgr_ops;
grant select (id, name) on "role" to ledgr_ops;
grant select (id, period_id) on journal_entry to ledgr_ops;
grant select (journal_entry_id, vat_treatment) on journal_line to ledgr_ops;

alter table reminder_log enable row level security;
alter table reminder_log force row level security;

create policy reminder_log_select on reminder_log
    for select using (app.has_administration_access(administration_id));

commit;

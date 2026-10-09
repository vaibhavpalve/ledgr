-- 0080_question_threads.sql
-- FR-FRM-005: question/answer threads between a firm and its client, attached to a specific
-- transaction or document, resolvable, with client notification.
-- See docs/decisions/ADR-111-question-threads.md.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
--   question_thread         one conversation about one administration, optionally about one
--                           record in it (a bank transaction, document, expense or sales invoice).
--   question_message        what was said. APPEND-ONLY: no UPDATE or DELETE grant to anyone. A
--                           message cannot be edited or withdrawn; a correction is another message.
--   question_read           "this person had the thread open at this moment". APPEND-ONLY; the
--                           latest row per (thread, user) wins. Nothing is ever updated, so two
--                           devices reading at once cannot lose a write.
--   question_notification   one row per e-mail SENT to a client user about a firm message, unique
--                           per (message, user), written by the notification sweep
--                           (api.questions.notifications, run as ledgr_ops) the way reminder_log
--                           is written by the reminder job (0069, ADR-090).
--
-- ===========================================================================
-- What may change on a thread, and why
-- ===========================================================================
--
-- The thread row carries three facts derived from its messages - `status`, `awaiting` and
-- `last_message_at` (plus `resolved_at`/`resolved_by_user_id`). They are kept on the row, and so
-- are UPDATEd, because the firm home reads them for every administration on every page view
-- (open-question counts, "waiting on client", the inbox's order) and recomputing them from the
-- message log for 200 clients per request is the wrong trade. They are a cache of the append-only
-- log, never the record: the conversation itself is question_message, which nothing can alter.
-- question_thread_guard_trg below makes every other column immutable and lets `status` move only
-- from 'open' to 'resolved', never back.
--
-- None of this is ledger data: no posting table is read or written here.
--
-- ===========================================================================
-- Tenancy
-- ===========================================================================
--
-- Every row carries organization_id (the administration's OWNING organization, checked by trigger)
-- and administration_id, and RLS on all four tables is app.has_administration_access - the owner,
-- or a firm with an active engagement. Which side of the conversation a writer is on
-- (author_side) is decided by the API from the session's organization, never from a request body.
--
-- Backward compatible: four new tables and column-level grants only. Safe to apply while the
-- previous application version is serving (it never reads these tables).
--
-- Down (documented rather than shipped as a file, matching every earlier migration here; run
-- inside one transaction, after the application no longer serves /questions):
--   revoke select (granted_by_organization_id) on role_assignment from ledgr_ops;
--   revoke select (archived_at) on "role" from ledgr_ops;
--   drop table question_notification;
--   drop table question_read;
--   drop table question_message;
--   drop table question_thread;
--   drop function question_thread_guard();
--   drop function question_child_belongs_here();

begin;

-- ---------------------------------------------------------------------------
-- question_thread
-- ---------------------------------------------------------------------------
create table question_thread (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),

    subject              text not null check (length(btrim(subject)) between 1 and 200),
    status               text not null default 'open' check (status in ('open', 'resolved')),
    -- Whose move it is: the side that did NOT write the last message.
    awaiting             text not null check (awaiting in ('client', 'firm')),

    -- What the question is about, when it is about one record. Checked to exist in the same
    -- administration by the API at creation (ADR-111); not a foreign key because it names one of
    -- four tables.
    resource_type        text check (resource_type in
                             ('bank_transaction', 'document', 'expense', 'sales_invoice')),
    resource_id          uuid,

    created_by_user_id   uuid not null references users(id),
    created_at           timestamptz not null default now(),
    last_message_at      timestamptz not null default now(),
    resolved_at          timestamptz,
    resolved_by_user_id  uuid references users(id),

    constraint question_thread_resource_pair check ((resource_type is null) = (resource_id is null)),
    constraint question_thread_resolution_pair check (
        (status = 'resolved') = (resolved_at is not null)
        and (resolved_at is null) = (resolved_by_user_id is null)
    )
);

-- Per-administration open threads (the client's question list, firm-core's open_questions count)
-- and the inbox, which reads open threads across many administrations newest first.
create index question_thread_open_by_administration_idx
    on question_thread(administration_id, last_message_at desc)
    where status = 'open';
-- Counts by administration and whose move it is (firm-core's "waiting on client").
create index question_thread_awaiting_idx
    on question_thread(administration_id, awaiting)
    where status = 'open';
-- Every status, for ?status=resolved|all.
create index question_thread_administration_idx
    on question_thread(administration_id, status, last_message_at desc);
create index question_thread_resource_idx
    on question_thread(administration_id, resource_type, resource_id)
    where resource_id is not null;

comment on table question_thread is
    'FR-FRM-005 / ADR-111. One firm-client conversation. status/awaiting/last_message_at are a '
    'cache of question_message and the only columns that change; status moves open -> resolved only.';

create or replace function question_thread_guard() returns trigger as $$
declare
    v_owner uuid;
begin
    if tg_op = 'INSERT' then
        select organization_id into v_owner from administration where id = new.administration_id;
        if v_owner is null then
            raise exception 'administration % is not a visible administration', new.administration_id;
        end if;
        if v_owner <> new.organization_id then
            raise exception 'question_thread.organization_id must be the administration''s owner';
        end if;
        return new;
    end if;

    -- UPDATE: identity is immutable.
    if new.id is distinct from old.id
       or new.organization_id is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.subject is distinct from old.subject
       or new.resource_type is distinct from old.resource_type
       or new.resource_id is distinct from old.resource_id
       or new.created_by_user_id is distinct from old.created_by_user_id
       or new.created_at is distinct from old.created_at then
        raise exception 'question_thread % : only status, awaiting, last_message_at and the '
                        'resolution may change', old.id;
    end if;
    -- A resolved thread is closed for good; a follow-up is a new thread.
    if old.status = 'resolved' then
        raise exception 'question_thread % is resolved and cannot change', old.id;
    end if;
    if new.last_message_at < old.last_message_at then
        raise exception 'question_thread % : last_message_at cannot move backwards', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger question_thread_guard_trg
    before insert or update on question_thread
    for each row execute function question_thread_guard();

-- ---------------------------------------------------------------------------
-- question_message (append-only)
-- ---------------------------------------------------------------------------
create table question_message (
    id                 uuid primary key default gen_random_uuid(),
    thread_id          uuid not null references question_thread(id),
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),
    author_user_id     uuid not null references users(id),
    author_side        text not null check (author_side in ('firm', 'client')),
    body               text not null check (length(btrim(body)) between 1 and 5000),
    created_at         timestamptz not null default now()
);

create index question_message_thread_idx on question_message(thread_id, created_at);
create index question_message_administration_idx on question_message(administration_id, created_at);

comment on table question_message is
    'FR-FRM-005 / ADR-111. Append-only: no UPDATE or DELETE is granted. author_side is derived '
    'server-side from the session''s organization, never from a request body.';

-- ---------------------------------------------------------------------------
-- question_read (append-only, latest row wins)
-- ---------------------------------------------------------------------------
create table question_read (
    id                 uuid primary key default gen_random_uuid(),
    thread_id          uuid not null references question_thread(id),
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),
    user_id            uuid not null references users(id),
    read_at            timestamptz not null default now()
);

-- "When did this person last have this thread open" - the inbox's unread test, per thread.
create index question_read_latest_idx on question_read(user_id, thread_id, read_at desc);

comment on table question_read is
    'FR-FRM-005 / ADR-111. Append-only read receipts; the latest row per (thread, user) wins.';

-- A message or read receipt belongs to its thread's administration and organization.
create or replace function question_child_belongs_here() returns trigger as $$
declare
    v_thread question_thread%rowtype;
begin
    select * into v_thread from question_thread where id = new.thread_id;
    if not found then
        raise exception 'question_thread % does not exist', new.thread_id;
    end if;
    if v_thread.administration_id <> new.administration_id
       or v_thread.organization_id <> new.organization_id then
        raise exception 'row belongs to another administration than question_thread %', new.thread_id;
    end if;
    if tg_table_name = 'question_message' and v_thread.status <> 'open' then
        raise exception 'question_thread % is resolved', new.thread_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger question_message_belongs_here_trg
    before insert on question_message
    for each row execute function question_child_belongs_here();
create trigger question_read_belongs_here_trg
    before insert on question_read
    for each row execute function question_child_belongs_here();

-- ---------------------------------------------------------------------------
-- question_notification (written by the sweep, as ledgr_ops)
-- ---------------------------------------------------------------------------
create table question_notification (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organization(id),
    administration_id  uuid not null references administration(id),
    thread_id          uuid not null references question_thread(id),
    message_id         uuid not null references question_message(id),
    user_id            uuid not null references users(id),
    sent_at            timestamptz not null default now()
);

create unique index question_notification_once_idx on question_notification(message_id, user_id);
create index question_notification_thread_idx on question_notification(thread_id, user_id);

comment on table question_notification is
    'FR-FRM-005 / ADR-111. Every question e-mail sent, once per (firm message, client user).';

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
alter table question_thread       owner to ledgr_migrator;
alter table question_message      owner to ledgr_migrator;
alter table question_read         owner to ledgr_migrator;
alter table question_notification owner to ledgr_migrator;

-- question_thread: UPDATE for the cached columns only (see the header). No DELETE anywhere.
grant select, insert, update on question_thread to ledgr_app;
grant select, insert on question_message to ledgr_app;
grant select, insert on question_read to ledgr_app;
grant select on question_notification to ledgr_app;

-- The notification sweep reads across tenants as ledgr_ops (ADR-090's posture): the threads, the
-- firm messages' ids and times (not their bodies - the e-mail never quotes them), read receipts,
-- and who on the client side holds a live grant. 0069 already granted the users and
-- role_assignment columns it needs except the two below.
grant select (id, organization_id, administration_id, status, awaiting, last_message_at)
    on question_thread to ledgr_ops;
grant select (id, thread_id, author_side, created_at) on question_message to ledgr_ops;
grant select (thread_id, user_id, read_at) on question_read to ledgr_ops;
grant select, insert on question_notification to ledgr_ops;
grant select (granted_by_organization_id) on role_assignment to ledgr_ops;
grant select (archived_at) on "role" to ledgr_ops;

alter table question_thread       enable row level security;
alter table question_thread       force row level security;
alter table question_message      enable row level security;
alter table question_message      force row level security;
alter table question_read         enable row level security;
alter table question_read         force row level security;
alter table question_notification enable row level security;
alter table question_notification force row level security;

create policy question_thread_select on question_thread
    for select using (app.has_administration_access(administration_id));
create policy question_thread_insert on question_thread
    for insert with check (app.has_administration_access(administration_id));
create policy question_thread_update on question_thread
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy question_message_select on question_message
    for select using (app.has_administration_access(administration_id));
create policy question_message_insert on question_message
    for insert with check (app.has_administration_access(administration_id));

create policy question_read_select on question_read
    for select using (app.has_administration_access(administration_id));
create policy question_read_insert on question_read
    for insert with check (app.has_administration_access(administration_id));

create policy question_notification_select on question_notification
    for select using (app.has_administration_access(administration_id));

commit;

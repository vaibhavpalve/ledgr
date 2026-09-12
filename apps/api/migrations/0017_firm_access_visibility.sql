-- 0017_firm_access_visibility.sql
-- IAM-109: "The client sees, at any time and without asking, which firm
-- users hold access to their administration, with what role, since when,
-- and when each last accessed it."
-- IAM-110: "On revocation of firm access, firm sessions for that
-- administration terminate immediately, the administration disappears from
-- the firm switcher, and the client retains all data including entries the
-- firm made."
-- See docs/decisions/ADR-018-firm-access-visibility-and-revocation.md.
--
-- Three of IAM-109's four columns already exist on role_assignment: WHO
-- (user_id), WITH WHAT ROLE (role_id), and SINCE WHEN (created_at). The
-- fourth - "when each last accessed it" - had nowhere to live, because
-- nothing in this system has ever recorded that a user looked at an
-- administration. That is what administration_access is for.
--
-- IAM-110's "sessions for that administration" presumes a session has an
-- administration context - the thing a firm switcher switches. Sessions
-- (0003) had none: they are per user, and a firm user works across many
-- clients in one login. sessions.active_administration_id adds it, which is
-- what makes "terminate sessions FOR THAT ADMINISTRATION" a statement about
-- specific rows rather than a choice between killing a firm employee's
-- entire login and killing nothing.

begin;

-- ---------------------------------------------------------------------------
-- administration_access
-- ---------------------------------------------------------------------------
-- One row per (user, administration), not an event per request. IAM-109 asks
-- for "when each last accessed it" - a single timestamp - and an append-only
-- event log would mean a row per page view forever, plus an aggregation on
-- every read of a screen the client is invited to check "at any time".
--
-- This is deliberately NOT the general-purpose audit trail (IAM-090+, not
-- built). It answers one question, for one screen, and an auditor wanting
-- the full history will want that other system.
create table administration_access (
    user_id             uuid not null references users(id),
    administration_id   uuid not null references administration(id),
    first_accessed_at   timestamptz not null default now(),
    last_accessed_at    timestamptz not null default now(),
    access_count        bigint not null default 1 check (access_count > 0),
    primary key (user_id, administration_id)
);

create index administration_access_administration_idx
    on administration_access(administration_id, last_accessed_at desc);

-- Monotonic: last_accessed_at only ever moves forward and access_count only
-- ever rises. A clock skew between application instances, or a retried
-- request carrying an older timestamp, must not make a firm user's last
-- access appear EARLIER than it was - that is the direction that would
-- mislead a client checking who has been in their books.
create or replace function administration_access_monotonic() returns trigger as $$
begin
    if new.last_accessed_at < old.last_accessed_at then
        new.last_accessed_at := old.last_accessed_at;
    end if;
    if new.access_count < old.access_count then
        new.access_count := old.access_count;
    end if;
    new.first_accessed_at := old.first_accessed_at;
    return new;
end;
$$ language plpgsql;

create trigger administration_access_monotonic_trg
    before update on administration_access
    for each row execute function administration_access_monotonic();

alter table administration_access owner to ledgr_migrator;

-- UPDATE is granted because recording an access is an upsert. No DELETE:
-- a firm user cannot erase the record of having looked at a client's books,
-- which is the whole point of showing it to the client.
grant select, insert, update on administration_access to ledgr_app;

alter table administration_access enable row level security;
alter table administration_access force row level security;

-- IAM-109's "without asking" lives in this policy. The CLIENT - the
-- organization that owns the administration - can read every access record
-- on it, including the firm's, with no cooperation from the firm required.
-- app.has_administration_access covers both sides (owner or engaged firm),
-- which is correct here: a firm may see its own staff's activity too.
create policy administration_access_select on administration_access
    for select using (app.has_administration_access(administration_id));

create policy administration_access_insert on administration_access
    for insert with check (app.has_administration_access(administration_id));

create policy administration_access_update on administration_access
    for update
    using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ---------------------------------------------------------------------------
-- sessions.active_administration_id
-- ---------------------------------------------------------------------------
-- The administration this session is currently working in - what the firm
-- switcher sets. NULL means the user is not inside any administration
-- (signed in, at the switcher, in organization-level settings).
--
-- Nullable and unconstrained by role: holding this column does not grant
-- anything. Authorization is still evaluated per request against live grants
-- (IAM-034), so a session pointing at an administration whose grants were
-- revoked is denied exactly as if the column were empty. The column exists so
-- IAM-110 can say WHICH sessions to terminate, not to carry access.
alter table sessions
    add column active_administration_id uuid references administration(id);

comment on column sessions.active_administration_id is 'The administration this session is currently working in - what the firm switcher sets, and what IAM-110 terminates on revocation. Carries no authority: every request still evaluates live grants (IAM-034).';

create index sessions_active_administration_idx
    on sessions(active_administration_id)
    where revoked_at is null and active_administration_id is not null;

commit;

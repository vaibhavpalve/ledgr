-- 0019_audit_log.sql
-- IAM-090 through IAM-093 (PRD §8.10). See docs/decisions/ADR-020-audit-log.md.
--
--   IAM-090  append-only audit log covering authentication, permission
--            grants/revocations, data reads of financial records, exports,
--            postings, approvals, filings, configuration changes and
--            support access
--   IAM-091  actor, actor type, tenant, resource, action, outcome,
--            timestamp (UTC), source IP, user agent, correlation ID
--   IAM-092  immutable and tamper-evident. No role, including Owner or
--            LEDGR staff, can edit or delete entries
--   IAM-093  retained 7 years, matching fiscal record retention
--
-- ===========================================================================
-- What actually enforces IAM-092, in order of what each layer stops
-- ===========================================================================
--
-- 1. NO PRIVILEGES. ledgr_app and ledgr_ops get SELECT and INSERT. Nobody
--    gets UPDATE or DELETE - not even the table's owner, from which both are
--    explicitly revoked. A statement that is never granted cannot be issued.
--
-- 2. TRIGGERS THAT ALWAYS RAISE. audit_log_no_update_trg and
--    audit_log_no_delete_trg reject every UPDATE and DELETE unconditionally.
--    This is the layer that stops the two roles a privilege check would not:
--
--      * ledgr_migrator, which owns every other table in this schema and
--        could therefore GRANT itself back anything it revoked. Postgres
--        does not privilege-check a table's owner.
--      * ledgr_ops, which holds BYPASSRLS for the operator scripts.
--        BYPASSRLS skips row-level security; it has never skipped a trigger.
--
--    Postgres runs BEFORE triggers for every writer, including a superuser.
--    That is what makes this stronger than any policy or grant.
--
-- 3. OWNERSHIP BY A ROLE NOBODY CAN CONNECT AS. A trigger only protects a
--    table while it exists, and a table's owner may DROP TRIGGER. So
--    audit_log and its trigger functions are owned by ledgr_audit:
--    NOLOGIN, NOSUPERUSER, NOBYPASSRLS, granted to nobody. ledgr_migrator
--    does not own this table and so cannot disarm it, which is what closes
--    the "not a database migration" case specifically.
--
--    This reuses the shape 0001 established for ledgr_bootstrap: put the
--    dangerous capability behind a role with no way in.
--
-- 4. HASH CHAINING, because layers 1-3 stop every role this application
--    creates and none of them stop a Postgres SUPERUSER. Nothing in the
--    database can - a superuser may drop a trigger, change an owner, or
--    rewrite a row. IAM-092 asks for immutable AND tamper-evident precisely
--    because prevention alone cannot survive that, and detection can.
--
--    Each entry hashes its own fields together with the previous entry's
--    hash, so altering entry N invalidates every entry after it. The hash is
--    computed by a trigger from the row's own values, never supplied by the
--    caller: an application that could name its own hash could forge a
--    consistent chain.
--
--    The honest limit: a superuser who rewrites an entry can also recompute
--    the rest of the chain. What defeats THAT is anchoring the head hash
--    outside this database - see app.audit_chain_head() and
--    scripts/anchor_audit_chain.py. Until an anchor is stored somewhere the
--    database cannot reach, the chain detects tampering by anyone who is not
--    a superuser, and no more. That is a deployment property, not a schema
--    one, and pretending otherwise would be the dishonest part.

begin;

-- ---------------------------------------------------------------------------
-- ledgr_audit
-- ---------------------------------------------------------------------------
-- Owns the audit table and its triggers, and nothing else. NOLOGIN, so there
-- is no session anyone can open as it; granted to no role, so nobody can SET
-- ROLE to it. Its entire purpose is to be an owner that is not ledgr_migrator.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ledgr_audit') then
        create role ledgr_audit nosuperuser nocreatedb nocreaterole nobypassrls nologin;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- audit_log
-- ---------------------------------------------------------------------------
create table audit_log (
    id                  uuid primary key default gen_random_uuid(),

    -- Chain position. Both assigned by the sealing trigger; a value supplied
    -- by a caller is overwritten.
    sequence_number     bigint not null,
    previous_hash       text not null,
    entry_hash          text not null,

    -- IAM-091: tenant. NOT NULL, which makes the chain per-organization -
    -- see the note on app.audit_chain_head() for why that matters.
    organization_id     uuid not null references organization(id),
    administration_id   uuid references administration(id),

    -- IAM-091: actor and actor type.
    actor_user_id       uuid references users(id),
    actor_type          text not null check (
        actor_type in ('user', 'service_account', 'system', 'support')
    ),

    -- IAM-090's coverage list, as a closed set. A category the requirement
    -- names but the code never emits is a gap; one the code emits that the
    -- requirement does not name is scope creep. Both are visible here.
    category            text not null check (
        category in (
            'authentication',
            'permission_change',
            'financial_read',
            'export',
            'posting',
            'approval',
            'filing',
            'configuration',
            'support_access'
        )
    ),

    -- IAM-091: resource, action, outcome.
    action              text not null,
    resource_type       text not null,
    resource_id         uuid,
    outcome             text not null check (outcome in ('success', 'failure', 'denied')),

    -- IAM-091: timestamp (UTC). Two of them, deliberately.
    --   occurred_at  when the event happened, as the caller reports it
    --   recorded_at  when the database sealed it, from the server clock
    -- Both are in the hash. A single timestamp cannot distinguish an event
    -- recorded late from one backdated on the way in, and a log whose whole
    -- job is being trusted should be able to tell those apart.
    occurred_at         timestamptz not null default now(),
    recorded_at         timestamptz not null,

    -- IAM-091: source IP, user agent, correlation ID.
    source_ip           inet,
    user_agent          text,
    correlation_id      text,

    -- Event-specific context. In the hash, so it cannot be edited either.
    detail              jsonb not null default '{}',

    constraint audit_log_chain_unique unique (organization_id, sequence_number)
);

create index audit_log_organization_idx on audit_log(organization_id, occurred_at desc);
create index audit_log_actor_idx on audit_log(organization_id, actor_user_id, occurred_at desc);
create index audit_log_category_idx on audit_log(organization_id, category, occurred_at desc);
create index audit_log_resource_idx on audit_log(organization_id, resource_type, resource_id);
create index audit_log_correlation_idx on audit_log(correlation_id) where correlation_id is not null;

-- ---------------------------------------------------------------------------
-- Sealing
-- ---------------------------------------------------------------------------
-- Length-prefixes each field before concatenating. Without it, a delimiter
-- appearing inside a value would let two different entries produce the same
-- payload: user_agent 'a|b' with correlation 'c' would hash identically to
-- user_agent 'a' with correlation 'b|c'. Academic, but the whole value of
-- this table is that two different entries never hash alike.
create or replace function app.audit_field(value text) returns text
language sql
immutable
as $$
    select coalesce(length(value)::text || ':' || value, '-1:');
$$;

create or replace function audit_log_seal() returns trigger as $$
declare
    v_prev_hash text;
    v_prev_seq bigint;
    v_payload text;
begin
    -- Serialize appends to THIS organization's chain. An advisory lock
    -- rather than SELECT ... FOR UPDATE because the latter requires an
    -- UPDATE privilege that no role has on this table, by design - the
    -- immutability guarantee and row locking are mutually exclusive here,
    -- and immutability wins.
    perform pg_advisory_xact_lock(hashtextextended(new.organization_id::text, 0));

    select entry_hash, sequence_number into v_prev_hash, v_prev_seq
    from audit_log
    where organization_id = new.organization_id
    order by sequence_number desc
    limit 1;

    -- Derived, never trusted. A caller that could choose its own sequence
    -- number or previous hash could fork the chain or forge a consistent one.
    new.sequence_number := coalesce(v_prev_seq, 0) + 1;
    new.previous_hash := coalesce(v_prev_hash, repeat('0', 64));
    new.recorded_at := now();

    v_payload :=
        app.audit_field(new.previous_hash) ||
        app.audit_field(new.sequence_number::text) ||
        app.audit_field(new.organization_id::text) ||
        app.audit_field(new.administration_id::text) ||
        app.audit_field(new.actor_user_id::text) ||
        app.audit_field(new.actor_type) ||
        app.audit_field(new.category) ||
        app.audit_field(new.action) ||
        app.audit_field(new.resource_type) ||
        app.audit_field(new.resource_id::text) ||
        app.audit_field(new.outcome) ||
        app.audit_field(to_char(new.occurred_at at time zone 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ||
        app.audit_field(to_char(new.recorded_at at time zone 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ||
        app.audit_field(host(new.source_ip)) ||
        app.audit_field(new.user_agent) ||
        app.audit_field(new.correlation_id) ||
        app.audit_field(new.detail::text);

    new.entry_hash := encode(digest(v_payload, 'sha256'), 'hex');
    return new;
end;
$$ language plpgsql;

create trigger audit_log_seal_trg
    before insert on audit_log
    for each row execute function audit_log_seal();

-- ---------------------------------------------------------------------------
-- Immutability
-- ---------------------------------------------------------------------------
-- Unconditional. There is no branch, no exemption and no flag - a function
-- whose only statement is RAISE cannot be talked into permitting anything.
-- These fire for ledgr_app, for ledgr_migrator, for ledgr_ops with BYPASSRLS,
-- and for a superuser at a psql prompt alike.
create or replace function audit_log_immutable() returns trigger as $$
begin
    raise exception
        'the audit log is append-only (IAM-092): entries cannot be % once written',
        lower(tg_op);
end;
$$ language plpgsql;

create trigger audit_log_no_update_trg
    before update on audit_log
    for each row execute function audit_log_immutable();

create trigger audit_log_no_delete_trg
    before delete on audit_log
    for each row execute function audit_log_immutable();

-- TRUNCATE is neither UPDATE nor DELETE and would otherwise empty the table
-- without firing either trigger above. It is a statement-level event, so it
-- needs its own.
create trigger audit_log_no_truncate_trg
    before truncate on audit_log
    for each statement execute function audit_log_immutable();

-- ---------------------------------------------------------------------------
-- Verification (IAM-092's "tamper-evident")
-- ---------------------------------------------------------------------------
-- Recomputes every entry's hash from its own stored fields and checks it
-- against both the stored hash and the next entry's previous_hash. Returns
-- the first broken link, or no rows when the chain is intact.
--
-- Deliberately recomputes rather than only comparing previous_hash to
-- entry_hash: a tamperer who edited a field and left the hashes alone would
-- pass a link check and fail this one.
create or replace function app.verify_audit_chain(p_organization_id uuid)
returns table (
    broken_sequence_number bigint,
    broken_entry_id uuid,
    reason text
)
language plpgsql
stable
as $$
declare
    r audit_log;
    v_expected_prev text := repeat('0', 64);
    v_expected_seq bigint := 1;
    v_payload text;
    v_hash text;
begin
    for r in
        select * from audit_log
        where organization_id = p_organization_id
        order by sequence_number
    loop
        if r.sequence_number <> v_expected_seq then
            return query select r.sequence_number, r.id,
                format('sequence gap: expected %s', v_expected_seq);
            return;
        end if;

        if r.previous_hash <> v_expected_prev then
            return query select r.sequence_number, r.id,
                'previous_hash does not match the preceding entry'::text;
            return;
        end if;

        v_payload :=
            app.audit_field(r.previous_hash) ||
            app.audit_field(r.sequence_number::text) ||
            app.audit_field(r.organization_id::text) ||
            app.audit_field(r.administration_id::text) ||
            app.audit_field(r.actor_user_id::text) ||
            app.audit_field(r.actor_type) ||
            app.audit_field(r.category) ||
            app.audit_field(r.action) ||
            app.audit_field(r.resource_type) ||
            app.audit_field(r.resource_id::text) ||
            app.audit_field(r.outcome) ||
            app.audit_field(to_char(r.occurred_at at time zone 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ||
            app.audit_field(to_char(r.recorded_at at time zone 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ||
            app.audit_field(host(r.source_ip)) ||
            app.audit_field(r.user_agent) ||
            app.audit_field(r.correlation_id) ||
            app.audit_field(r.detail::text);
        v_hash := encode(digest(v_payload, 'sha256'), 'hex');

        if v_hash <> r.entry_hash then
            return query select r.sequence_number, r.id,
                'entry contents do not match its hash'::text;
            return;
        end if;

        v_expected_prev := r.entry_hash;
        v_expected_seq := r.sequence_number + 1;
    end loop;

    return;
end;
$$;

-- The value an external anchor stores. Anchoring this outside the database -
-- object storage under a WORM policy, a separate account, a customer's own
-- copy - is what extends tamper-evidence to cover a superuser, who can
-- otherwise rewrite an entry and recompute the chain after it. Without an
-- external anchor the chain detects everyone except the one who owns the
-- server, which is worth stating plainly rather than implying otherwise.
create or replace function app.audit_chain_head(p_organization_id uuid)
returns table (sequence_number bigint, head_hash text, entries bigint)
language sql
stable
as $$
    select max(a.sequence_number),
           (select entry_hash from audit_log
            where organization_id = p_organization_id
            order by sequence_number desc limit 1),
           count(*)
    from audit_log a
    where a.organization_id = p_organization_id;
$$;

-- ---------------------------------------------------------------------------
-- Ownership and privileges
-- ---------------------------------------------------------------------------
alter table audit_log owner to ledgr_audit;
alter function audit_log_seal() owner to ledgr_audit;
alter function audit_log_immutable() owner to ledgr_audit;
alter function app.audit_field(text) owner to ledgr_audit;
alter function app.verify_audit_chain(uuid) owner to ledgr_audit;
alter function app.audit_chain_head(uuid) owner to ledgr_audit;

-- Belt and braces on top of the triggers. Postgres does not privilege-check
-- a table's owner, so this revoke is not what stops ledgr_audit - the
-- triggers are. It stops the privileges being inherited or granted onward by
-- accident, and it makes the intent unmissable to anyone reading \dp.
revoke all on audit_log from public;
revoke update, delete, truncate on audit_log from ledgr_audit;

grant select, insert on audit_log to ledgr_app;
-- Support tooling reads and never writes: IAM-090 requires support ACCESS to
-- be audited, and a role that could write the log recording its own access
-- is not an auditor of itself.
grant select on audit_log to ledgr_ops;

grant execute on function app.verify_audit_chain(uuid) to ledgr_app, ledgr_ops;
grant execute on function app.audit_chain_head(uuid) to ledgr_app, ledgr_ops;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
alter table audit_log enable row level security;
alter table audit_log force row level security;

-- IAM-094 (customers search and export their own audit log without
-- contacting support) is not built, but this is the policy it will need: a
-- tenant reads its own entries and no others.
create policy audit_log_select on audit_log
    for select using (organization_id = app.current_org_id());

create policy audit_log_insert on audit_log
    for insert with check (organization_id = app.current_org_id());

-- No UPDATE or DELETE policy, and no grant either. Two independent reasons a
-- write would fail before the triggers are even reached.

-- ---------------------------------------------------------------------------
-- IAM-093: seven-year retention
-- ---------------------------------------------------------------------------
-- Satisfied by there being no way to remove a row at all - retention is
-- unbounded, which is longer than seven years. Recorded here because "we
-- never delete" is a decision someone might later mistake for an oversight
-- and 'fix' with a purge job. A purge that ever becomes necessary must not
-- be a DELETE: it would have to be an export-then-drop-partition operation,
-- which is a schema change and a new ADR, not a cron job.

commit;

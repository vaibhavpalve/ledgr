-- 0022_idempotency.sql
-- NFR-032 (PRD §12). See docs/decisions/ADR-024-idempotency.md.
--
--   NFR-032  Idempotency keys on all mutating API endpoints so retries
--            cannot double-post.
--
-- ===========================================================================
-- Two layers, and why both exist
-- ===========================================================================
--
-- This table stops a retried REQUEST from being executed twice. It is not the
-- only thing standing between a retry and a double posting:
--
--   * HTTP layer (here): the second request never reaches the handler. The
--     first response is replayed. Bounded by expiry - see below.
--   * Domain layer (0020): journal_entry.idempotency_key, unbounded in time.
--     Even a request that gets past this table - because the key expired, or
--     because the caller came in through a path that has no HTTP edge at all -
--     cannot produce a second posting for the same key.
--
-- The domain guarantee is the durable one; this is the one that makes a retry
-- cheap and gives the caller back the same response body. Neither subsumes
-- the other: the domain key only covers postings, and this covers every
-- mutating endpoint.
--
-- ===========================================================================
-- What a key identifies
-- ===========================================================================
--
-- (organization_id, user_id, method, path_template, key). All five, because
-- each one prevents a real confusion:
--
--   organization_id  two tenants must be able to choose the same key string
--   user_id          otherwise user B replays user A's response body, which is
--                    a cross-user data leak wearing an idempotency key
--   method           PUT and DELETE on one path are different operations
--   path_template    "abc" on /v1/entries and on /v1/invoices are different
--                    keys; the TEMPLATE, not the concrete path, so the path
--                    parameters live in the fingerprint where a mismatch is
--                    reported rather than silently making a new key
--   key              the client's own value
--
-- ===========================================================================
-- Retention (the honest limit)
-- ===========================================================================
--
-- Rows expire. A retry after the window re-executes at the HTTP layer, and is
-- caught by the domain-layer key for postings and by nothing for anything
-- else. That is a real limit and the reason the window is a deployment
-- decision recorded here rather than a constant buried in application code.
--
-- Unlike the ledger and the audit log, this table IS deletable: it holds a
-- cache of responses, not a record of what happened. What happened is in
-- audit_log, which has no delete path at all.

begin;

create table idempotency_key (
    id                  uuid primary key default gen_random_uuid(),

    organization_id     uuid not null references organization(id),
    -- NOT NULL: a key with no caller could be replayed by anyone in the
    -- tenant, and the stored response body is whatever the first caller was
    -- allowed to see. Every mutating request has an authenticated user by the
    -- time it reaches this table (MfaEnforcementMiddleware runs first), so
    -- this costs nothing and closes a leak.
    user_id             uuid not null references users(id),

    method              text not null check (method in ('POST', 'PUT', 'PATCH', 'DELETE')),
    path_template       text not null,
    idempotency_key     text not null check (
        length(btrim(idempotency_key)) between 1 and 255
    ),

    -- sha256 over method, concrete path, query string and body. A second
    -- request with the same key and a different fingerprint is a client bug -
    -- a key reused for a different operation - and is refused rather than
    -- silently served the first response.
    request_fingerprint text not null,

    -- in_progress is claimed BEFORE the handler runs, in its own committed
    -- transaction, so a concurrent duplicate sees it and is told to wait
    -- rather than both being executed.
    state               text not null default 'in_progress'
                            check (state in ('in_progress', 'completed')),

    response_status     smallint check (response_status between 100 and 599),
    response_body       bytea,
    -- A deliberately narrow allow-list of headers, chosen by the application.
    -- Replaying Set-Cookie or an auth header from the first caller's response
    -- is the same leak user_id above prevents, by another route.
    response_headers    jsonb not null default '{}',

    created_at          timestamptz not null default now(),
    completed_at        timestamptz,
    expires_at          timestamptz not null,

    constraint idempotency_key_unique
        unique (organization_id, user_id, method, path_template, idempotency_key),

    -- A completed row must carry the response it is going to replay. Without
    -- this, a bug that marked a row completed without storing the body would
    -- surface as a retry receiving an empty 200 - the worst possible failure
    -- for this table, because it looks like success.
    constraint idempotency_key_completed_has_a_response check (
        state <> 'completed'
        or (response_status is not null and completed_at is not null)
    )
);

create index idempotency_key_expiry_idx on idempotency_key(expires_at);
create index idempotency_key_organization_idx on idempotency_key(organization_id);

-- ---------------------------------------------------------------------------
-- Expiry
-- ---------------------------------------------------------------------------
-- Called by a scheduled job. A function rather than a raw DELETE in a cron
-- script so the predicate lives with the table: "expired" is a property of
-- this schema, and a job that computed it independently could drift from it.
--
-- Returns the count so the job can log it - a purge that silently deletes
-- nothing for a month is indistinguishable from one that is working, and the
-- number is the difference.
-- SECURITY DEFINER, owned by ledgr_bootstrap. A purge is cross-tenant by
-- nature and FORCE ROW LEVEL SECURITY applies to the table's owner as well,
-- so a plain function would silently delete only the rows of whichever tenant
-- happened to be in the session GUC - and NOTHING at all for a maintenance
-- job that sets no tenant context, which is exactly how a job like this is
-- run. Fail-closed, but broken.
--
-- ledgr_bootstrap is 0001's NOLOGIN, BYPASSRLS role, granted to nobody. This
-- is the same shape 0001 used for organization signup: the cross-tenant
-- capability lives behind a role with no way in, and the bypass surface is
-- exactly this function body.
create or replace function app.purge_expired_idempotency_keys()
returns bigint
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_deleted bigint;
begin
    delete from idempotency_key where expires_at < now();
    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

-- ---------------------------------------------------------------------------
-- Claiming a key
-- ---------------------------------------------------------------------------
-- One statement, so the check and the claim cannot interleave. Returns what
-- the caller needs to decide among the four outcomes:
--
--   claimed    no row existed; this request owns the key and should run
--   in_flight  a row exists in in_progress; a duplicate is running right now
--   replay     a completed row with a MATCHING fingerprint; serve its response
--   mismatch   a row exists with a DIFFERENT fingerprint; the key was reused
--              for a different request, which is a client bug
--
-- ON CONFLICT DO NOTHING rather than a SELECT-then-INSERT: two concurrent
-- first-attempts would both see nothing and both insert, and one would fail
-- on the unique constraint at commit - after doing the work. Here the loser
-- gets no row back from the insert, re-reads, and reports in_flight.
create or replace function app.claim_idempotency_key(
    p_organization_id     uuid,
    p_user_id             uuid,
    p_method              text,
    p_path_template       text,
    p_key                 text,
    p_request_fingerprint text,
    p_expires_at          timestamptz
)
returns table (
    outcome         text,
    response_status smallint,
    response_body   bytea,
    response_headers jsonb
)
language plpgsql
as $$
declare
    v_row idempotency_key%rowtype;
begin
    -- An expired row is not a row. Without this the INSERT below still
    -- conflicts with it, the read finds it, and an expired key is replayed
    -- forever - expiry would mean nothing until the purge job happened to
    -- run, making correctness depend on a cron schedule.
    --
    -- Scoped to this one key rather than a general sweep: reclaiming space is
    -- the purge job's business, and a claim should not do unbounded work.
    delete from idempotency_key
     where organization_id = p_organization_id
       and user_id = p_user_id
       and method = p_method
       and path_template = p_path_template
       and idempotency_key = p_key
       and expires_at < now();

    insert into idempotency_key (
        organization_id, user_id, method, path_template, idempotency_key,
        request_fingerprint, state, expires_at
    )
    values (
        p_organization_id, p_user_id, p_method, p_path_template, p_key,
        p_request_fingerprint, 'in_progress', p_expires_at
    )
    on conflict on constraint idempotency_key_unique do nothing
    returning * into v_row;

    if found then
        return query select 'claimed'::text, null::smallint, null::bytea, null::jsonb;
        return;
    end if;

    select * into v_row
      from idempotency_key
     where organization_id = p_organization_id
       and user_id = p_user_id
       and method = p_method
       and path_template = p_path_template
       and idempotency_key = p_key;

    if not found then
        -- The row vanished between the insert and this read: the only way is
        -- a concurrent purge of an expired row. Reporting in_flight makes the
        -- caller retry, which will then claim cleanly.
        return query select 'in_flight'::text, null::smallint, null::bytea, null::jsonb;
        return;
    end if;

    -- Checked BEFORE state. A key reused for a different request is a client
    -- bug whether or not the first one has finished, and reporting it as
    -- "still in flight" would send the caller into a retry loop that can
    -- never succeed.
    if v_row.request_fingerprint <> p_request_fingerprint then
        return query select 'mismatch'::text, null::smallint, null::bytea, null::jsonb;
        return;
    end if;

    if v_row.state = 'in_progress' then
        return query select 'in_flight'::text, null::smallint, null::bytea, null::jsonb;
        return;
    end if;

    return query select 'replay'::text, v_row.response_status, v_row.response_body,
                        v_row.response_headers;
end;
$$;

-- ---------------------------------------------------------------------------
-- Completing, and releasing
-- ---------------------------------------------------------------------------
create or replace function app.complete_idempotency_key(
    p_organization_id uuid,
    p_user_id         uuid,
    p_method          text,
    p_path_template   text,
    p_key             text,
    p_status          smallint,
    p_body            bytea,
    p_headers         jsonb
)
returns void
language sql
as $$
    update idempotency_key
       set state = 'completed',
           response_status = p_status,
           response_body = p_body,
           response_headers = coalesce(p_headers, '{}'::jsonb),
           completed_at = now()
     where organization_id = p_organization_id
       and user_id = p_user_id
       and method = p_method
       and path_template = p_path_template
       and idempotency_key = p_key
       and state = 'in_progress';
$$;

-- A request that failed in a way the caller should be able to RETRY - a 5xx,
-- or an unhandled exception - releases its claim instead of completing it.
--
-- Storing a 5xx and replaying it would make a transient failure permanent for
-- the life of the key: the caller retries correctly, and gets the same 500
-- back forever. The request's own transaction has already rolled back, so
-- nothing was committed that a retry could duplicate - and for postings the
-- domain-level key in 0020 is the backstop if anything did.
create or replace function app.release_idempotency_key(
    p_organization_id uuid,
    p_user_id         uuid,
    p_method          text,
    p_path_template   text,
    p_key             text
)
returns void
language sql
as $$
    delete from idempotency_key
     where organization_id = p_organization_id
       and user_id = p_user_id
       and method = p_method
       and path_template = p_path_template
       and idempotency_key = p_key
       and state = 'in_progress';
$$;

-- ---------------------------------------------------------------------------
-- Ownership and privileges
-- ---------------------------------------------------------------------------
alter table idempotency_key owner to ledgr_migrator;

revoke all on idempotency_key from public;

-- DELETE is granted here, unlike anywhere else in this schema. This table
-- holds a cache of responses, not a record of what happened - that is
-- audit_log, which has no delete path at all. A row must be removable so a
-- failed request can release its claim and so expiry can reclaim space.
grant select, insert, update, delete on idempotency_key to ledgr_app;
grant select on idempotency_key to ledgr_ops;

grant execute on function app.claim_idempotency_key(
    uuid, uuid, text, text, text, text, timestamptz
) to ledgr_app;
grant execute on function app.complete_idempotency_key(
    uuid, uuid, text, text, text, smallint, bytea, jsonb
) to ledgr_app;
grant execute on function app.release_idempotency_key(uuid, uuid, text, text, text)
    to ledgr_app;
alter function app.purge_expired_idempotency_keys() owner to ledgr_bootstrap;
grant delete on idempotency_key to ledgr_bootstrap;
-- Not granted to ledgr_app: purging is a maintenance operation, and a request
-- handler that could delete other tenants' keys is a capability no endpoint
-- needs. ledgr_ops runs the scheduled job.
grant execute on function app.purge_expired_idempotency_keys() to ledgr_ops;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
alter table idempotency_key enable row level security;
alter table idempotency_key force row level security;

-- A stored response body is whatever the first caller was allowed to see, so
-- the tenant predicate here is doing real work: without it a replay could
-- serve one tenant's response to another.
create policy idempotency_key_select on idempotency_key
    for select using (organization_id = app.current_org_id());
create policy idempotency_key_insert on idempotency_key
    for insert with check (organization_id = app.current_org_id());
create policy idempotency_key_update on idempotency_key
    for update using (organization_id = app.current_org_id())
    with check (organization_id = app.current_org_id());
create policy idempotency_key_delete on idempotency_key
    for delete using (organization_id = app.current_org_id());

commit;

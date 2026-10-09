-- 0081_audit_log_firm_acting_on_client.sql
-- IAM-090, IAM-092, IAM-094, IAM-107. See docs/decisions/ADR-112-audit-log-firm-acting-on-client.md.
--
-- A firm user acting in a client's books files that action's audit entry in
-- the CLIENT's chain (IAM-094: the client reads "my accountant posted this" in
-- its own log). audit_log_insert (0019) admitted a row only for the session's
-- own organization - the firm - so every posting, capture and expense post
-- from a firm session failed. ADR-059 worked around it once with a
-- transaction-local tenant switch in onboarding; this is the systemic fix.
--
-- Three changes, each the narrowest that closes its gap:
--
-- 1. audit_log_insert gains ONE extra branch. A row may name another
--    organization only when it also names an administration that
--      * app.has_administration_access() admits for this session (own, or an
--        ACTIVE firm_engagement - a pending/revoked engagement admits nothing),
--      * AND that administration's owning organization is EXACTLY the row's
--        organization_id.
--    So a firm can file into the chain of a client it is actively engaged
--    on, under that client's own organization, and nowhere else. A row with
--    administration_id NULL still needs organization_id = current_org_id().
--
-- 2. audit_log_seal() becomes SECURITY DEFINER (owner ledgr_audit, search_path
--    pinned), and a SELECT policy scoped TO ledgr_audit lets it - and only it -
--    see every row. Without this, the seal reads the chain's predecessor under
--    the FIRM's RLS, finds nothing, and seals the entry as sequence 1 with the
--    zero hash: a forked client chain. FORCE RLS binds the owner too, which is
--    why ownership alone is not enough. ledgr_audit stays NOLOGIN, NOBYPASSRLS
--    and granted to nobody, so the only code that ever runs as it is the
--    functions it owns; the role-scoped policy is invisible to ledgr_app,
--    ledgr_ops and ledgr_migrator, whose reads are unchanged.
--
-- 3. Nothing else. audit_log_select is NOT widened: a firm may append to a
--    client's chain but still cannot read a single row of it (IAM-094 /
--    IAM-109 own that decision). The application therefore no longer uses
--    INSERT ... RETURNING on audit_log (RETURNING evaluates the SELECT policy);
--    api.audit.repository inserts with a client-generated id and reads the
--    row back through RLS, getting nothing back for a row it may not read.
--
-- Down (reverses exactly; no data is touched, so it is lossless - rows filed
-- by firm sessions in the meantime stay valid links in their clients' chains):
--
--   begin;
--   drop policy if exists audit_log_insert on audit_log;
--   create policy audit_log_insert on audit_log
--       for insert with check (organization_id = app.current_org_id());
--   drop policy if exists audit_log_seal_read on audit_log;
--   alter function audit_log_seal() security invoker;
--   alter function audit_log_seal() reset search_path;
--   revoke usage on schema app from ledgr_audit;
--   commit;
--
-- Backward compatible (NFR-044): every row the old policy admitted, the new one
-- admits; the seal computes byte-identical hashes; the app code shipped with
-- this migration (no RETURNING) works against the old schema too.

begin;

-- ---------------------------------------------------------------------------
-- What the definer needs
-- ---------------------------------------------------------------------------
-- USAGE on app: the seal calls app.audit_field (which ledgr_audit already
-- owns). Nothing else: the seal reads no table but audit_log, so ledgr_audit
-- is granted nothing on administration or firm_engagement.
grant usage on schema app to ledgr_audit;

-- ---------------------------------------------------------------------------
-- The seal reads the true chain head regardless of the caller's RLS
-- ---------------------------------------------------------------------------
create or replace function audit_log_seal() returns trigger
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_prev_hash text;
    v_prev_seq bigint;
    v_payload text;
begin
    -- Serialize appends to THIS organization's chain (unchanged from 0019).
    perform pg_advisory_xact_lock(hashtextextended(new.organization_id::text, 0));

    -- Runs as ledgr_audit, which audit_log_seal_read admits to every row:
    -- the predecessor is found whichever tenant's session is appending.
    select entry_hash, sequence_number into v_prev_hash, v_prev_seq
    from public.audit_log
    where organization_id = new.organization_id
    order by sequence_number desc
    limit 1;

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
$$;

alter function audit_log_seal() owner to ledgr_audit;
revoke all on function audit_log_seal() from public;

-- Only ledgr_audit - i.e. only code ledgr_audit owns - is admitted by this
-- policy. Policies are per role; ledgr_app/ledgr_ops/ledgr_migrator never
-- match it and keep 0019's audit_log_select as their only read path.
drop policy if exists audit_log_seal_read on audit_log;
create policy audit_log_seal_read on audit_log
    for select to ledgr_audit using (true);

-- ---------------------------------------------------------------------------
-- The insert policy: own organization, or the owner of an engaged administration
-- ---------------------------------------------------------------------------
-- Evaluated as the caller (ledgr_app), so the administration lookup is itself
-- under RLS: a firm sees an administration only through an active engagement.
-- organization_id is qualified with the table name because inside the
-- subquery an unqualified name would bind to administration.organization_id.
drop policy if exists audit_log_insert on audit_log;
create policy audit_log_insert on audit_log
    for insert with check (
        organization_id = app.current_org_id()
        or (
            administration_id is not null
            and app.has_administration_access(administration_id)
            and exists (
                select 1 from administration a
                where a.id = audit_log.administration_id
                  and a.organization_id = audit_log.organization_id
            )
        )
    );

commit;

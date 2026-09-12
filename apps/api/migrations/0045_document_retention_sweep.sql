-- 0045_document_retention_sweep.sql
-- PRIV-030 (PRD 10.4). See docs/decisions/ADR-051-document-retention-sweep.md.
--
--   PRIV-030  Retention is enforced by automated jobs, not manual process,
--             with an exception report for records that failed to expire.
--
-- ===========================================================================
-- What already makes this safe, and what does not
-- ===========================================================================
--
-- `document_deletion_guard` (0031) already permits an unconditional DELETE
-- once `retention_until < current_date`, whatever the row's `status` -
-- PRIV-023's "deleted automatically at the end of the retention period"
-- applies to a restricted document exactly as it does to an active one, and
-- no approval is needed for either. So the guard is not what this migration
-- adds.
--
-- What it adds is the SELECTION, as a callable rule rather than a query the
-- job would otherwise have to get right on its own - the same reasoning
-- 0023's `ledger.integrity_*` functions rest on. And it adds foresight: a
-- document that was ever linked to a posting can never actually be removed,
-- because `document_posting_link` is immutable and never deleted (0031,
-- FR-DOC-003) and its default foreign key to `document` has no ON DELETE
-- action. A DELETE against such a row raises a foreign key violation, and
-- that violation says only that something referenced the row, not what or
-- why - which is not the "exception report for records that failed to
-- expire" PRIV-030 asks for. `blocking_link_count` lets the job decide in
-- advance and explain the exception in FR-DOC-003's own terms instead.
--
-- SECURITY INVOKER (the default - stated here as ledger.integrity_* states it
-- explicitly), so tenant scoping is RLS's: `ledgr_app` sees one administration,
-- `ledgr_ops` (BYPASSRLS) sees every one, and the nightly sweep needs no
-- per-tenant loop. A SECURITY DEFINER function here would report a clean
-- archive for every tenant when run through `ledgr_ledger`-style roles with no
-- BYPASSRLS - the exact failure ADR-025 documents for the ledger integrity
-- checks, and the reason this function is invoker too.

begin;

create or replace function documents.expired(
    p_administration_id uuid default null
)
returns table (
    id                  uuid,
    organization_id     uuid,
    administration_id   uuid,
    retention_basis     text,
    retention_until     date,
    status              text,
    blocking_link_count bigint
)
language sql
stable
as $$
    select d.id, d.organization_id, d.administration_id, d.retention_basis,
           d.retention_until, d.status,
           (select count(*)
              from document_posting_link l
             where l.document_id = d.id) as blocking_link_count
      from document d
     where d.retention_until < current_date
       and (p_administration_id is null or d.administration_id = p_administration_id)
     order by d.retention_until;
$$;

comment on function documents.expired(uuid) is
    'PRIV-030. Documents whose FR-DOC-002 retention has run out, with the '
    'count of document_posting_link rows (live or detached) that would block '
    'their deletion. Empty on an archive with nothing yet due.';

grant execute on function documents.expired(uuid) to ledgr_app, ledgr_ops;

commit;

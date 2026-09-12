-- 0031_documents.sql
-- FR-DOC-001 .. FR-DOC-005 (PRD §6.10), SEC-005 (§9). See
-- docs/decisions/ADR-030-document-storage.md.
--
--   FR-DOC-001  Every document is stored in original form, unaltered,
--               alongside any derived text and extracted fields.
--   FR-DOC-002  Retention of 7 years from the end of the fiscal year (10 years
--               where immovable property is involved), enforced by policy and
--               not deletable by users.
--   FR-DOC-003  Documents are linked to postings bidirectionally; a posting
--               without a source document is flagged in a completeness report.
--   FR-DOC-004  Full-text search across document content, supplier, amount and
--               date.
--   FR-DOC-005  Storage is write-once for the retention period; deletion
--               before expiry requires a documented legal basis and privileged
--               approval.
--
-- ===========================================================================
-- "Unaltered" and "alongside" are two different columns' worth of rule
-- ===========================================================================
--
-- FR-DOC-001 is one sentence carrying two opposite requirements. The ORIGINAL
-- must never change: its bytes, its hash, its size, its verified content type,
-- the administration it belongs to. The DERIVED material must be writable
-- after the fact, because OCR and field extraction happen after the upload
-- (FR-EXP-001c is explicit that the product never blocks on extraction being
-- available) and may be re-run when the pipeline improves.
--
-- So this table is not immutable and is not mutable. It is immutable in its
-- identity columns and mutable in exactly six, and `document_original_
-- immutable()` below is the line between them. That is the same shape
-- `ledger_account_identity_immutable()` has in 0020, for the same reason:
-- "append-only" is too blunt an instrument when a row legitimately learns
-- things about itself later.
--
-- The bytes themselves are not in this table at all. They are in Azure Blob
-- (PRD §13), encrypted under the administration's own key (IAM-004, SEC-022,
-- migration 0002), and `storage_key` is the reference. What this schema
-- guarantees about them is `content_hash`: an original that was altered no
-- longer matches, and `documents.integrity_deviations()` reports it.
--
-- ===========================================================================
-- Retention is DERIVED, never supplied
-- ===========================================================================
--
-- FR-DOC-002 fixes retention to the fiscal year's end, not the upload date. A
-- receipt uploaded in March 2026 for fiscal year 2025 is retained until the
-- end of 2032, not 2033 - and a receipt uploaded on the last day of a fiscal
-- year and one uploaded on its first day are retained to the same date.
--
-- `retention_until` is therefore computed by a trigger and any caller-supplied
-- value is DISCARDED, the same stance 0020 takes on `journal_entry.
-- entry_number`. A retention date the uploader could choose is not a retention
-- policy; it is a suggestion, and the one thing FR-DOC-002 says it must not be
-- is "deletable by users".
--
-- The rule exists twice on purpose - `documents.retention_until()` here and
-- `api.documents.retention` in Python - because an onboarding screen has to
-- show a document's retention date before the row exists, and the database has
-- to compute it inside the transaction that creates one. Neither can borrow
-- the other's answer, so tests/documents/retention_cases.py runs one table
-- against both. That is the device 0029 uses for fiscal period derivation.

begin;

-- CLAUDE.md's search deviation: Postgres full-text search over the
-- already-RLS-partitioned tables rather than a second tenant-partitioned
-- datastore. Trigram rather than tsvector, and the reason is bilingual: a
-- tsvector column needs a stemmer chosen per document, and this corpus is
-- Dutch and English mixed with no reliable way to tell which a scanned receipt
-- is. `gin_trgm_ops` needs no language at all.
create extension if not exists pg_trgm;

create schema if not exists documents;
comment on schema documents is
    'FR-DOC. Functions over the document archive: retention derivation, the '
    'completeness report, and the only path that deletes anything.';

-- ===========================================================================
-- The retention rule, as a function anyone can call
-- ===========================================================================
-- Pure: no tenant, no reads, no writes. Callable to show a retention date on a
-- screen before the document is stored, which is the difference between
-- choosing to upload something and finding out what happened to it.
create or replace function documents.retention_until(
    p_fiscal_year_end date,
    p_basis           text default 'standard'
)
returns date
language sql
immutable
as $$
    -- "7 years from the END OF the fiscal year": the year ends on
    -- p_fiscal_year_end, and the seventh full year after it ends on the same
    -- date seven years later. A 2025 calendar year (ending 2025-12-31) is
    -- retained until 2032-12-31.
    --
    -- Deliberately NOT `+ interval '7 years' - 1 day`: that would expire the
    -- archive one day early, and the day it would lose is the last day of the
    -- seventh year - exactly the day an inspection dated to the deadline would
    -- ask for.
    select (p_fiscal_year_end + make_interval(
        years => case p_basis when 'immovable_property' then 10 else 7 end
    ))::date;
$$;

comment on function documents.retention_until(date, text) is
    'FR-DOC-002. 7 years from the end of the fiscal year, 10 where immovable '
    'property is involved. Pure - safe to call before the document exists.';

-- ===========================================================================
-- document
-- ===========================================================================
create table document (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1: every record carries both, and RLS reads them.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- -------------------------------------------------------------------
    -- The original (FR-DOC-001). Immutable - see document_original_immutable.
    -- -------------------------------------------------------------------
    -- The blob reference. UNIQUE because two rows pointing at one blob would
    -- make deletion at the end of one's retention period silently destroy the
    -- other's original.
    storage_key         text not null unique
                            check (length(btrim(storage_key)) > 0),
    -- SHA-256 of the ORIGINAL bytes, before encryption and before any
    -- derivation. This is what makes "unaltered" checkable rather than
    -- asserted; documents.integrity_deviations() is the check.
    content_hash        bytea not null check (octet_length(content_hash) = 32),
    byte_size           bigint not null check (byte_size > 0),
    -- SEC-005: "type verified by content not extension". This column holds
    -- what the BYTES said, established by api.documents.content_type before
    -- the row is written. The uploaded filename is kept separately and is
    -- never consulted to decide what a file is.
    content_type        text not null,
    original_filename   text,

    -- -------------------------------------------------------------------
    -- Derived material (FR-DOC-001's "alongside"). Mutable, deliberately.
    -- -------------------------------------------------------------------
    -- OCR output. Arrives after upload and may be replaced when the pipeline
    -- improves - which is why this table is not simply append-only.
    derived_text        text,
    extracted_fields    jsonb not null default '{}'::jsonb,

    -- -------------------------------------------------------------------
    -- Retention (FR-DOC-002). Derived by trigger; supplied values discarded.
    -- -------------------------------------------------------------------
    fiscal_year_id      uuid not null references fiscal_year(id),
    retention_basis     text not null default 'standard'
                            check (retention_basis in ('standard', 'immovable_property')),
    retention_until     date not null,

    -- PRIV-023: a record whose erasure is blocked by retention law is
    -- RESTRICTED rather than deleted - "access limited to fiscal purposes,
    -- excluded from analytics and search". `restricted` is that state, and
    -- documents.search() excludes it.
    status              text not null default 'active'
                            check (status in ('active', 'restricted')),

    -- -------------------------------------------------------------------
    -- SEC-005: malware scanning
    -- -------------------------------------------------------------------
    -- `pending` is the state a document is created in. It is NOT downloadable
    -- until `clean`: an unscanned file and an infected one are the same risk
    -- to whoever opens it, and the difference between them is only time.
    scan_status         text not null default 'pending'
                            check (scan_status in ('pending', 'clean', 'infected', 'failed')),
    scanned_at          timestamptz,
    scanner             text,

    uploaded_by_user_id uuid references users(id),
    uploaded_at         timestamptz not null default now(),

    -- FR-DOC-004, and the reason it is a generated column: a search index over
    -- a value some writer has to remember to maintain is an index that goes
    -- stale. Amount and date are not in here - they are structured, and are
    -- queried as predicates over extracted_fields rather than as text.
    search_text         text generated always as (
        coalesce(derived_text, '') || ' ' ||
        coalesce(original_filename, '') || ' ' ||
        coalesce(extracted_fields ->> 'supplier', '')
    ) stored,

    -- The scan verdict has to be recorded with its time and its scanner, or it
    -- cannot be re-evaluated when a scanner is later found to have been broken.
    constraint document_scan_recorded check (
        (scan_status = 'pending') = (scanned_at is null)
        and (scan_status = 'pending') = (scanner is null)
    )
);

create index document_administration_idx on document(administration_id, uploaded_at desc);
create index document_organization_idx   on document(organization_id);
create index document_fiscal_year_idx    on document(fiscal_year_id);
-- PRIV-030's expiry job sweeps by date across tenants.
create index document_retention_idx      on document(retention_until) where status <> 'restricted';
-- IAM-101's "view own submissions": an Expense Submitter's own uploads. The
-- authorization side of that is still to be built (see matrix.py's note on
-- EXTENSION_CAPABILITIES); this is the column and the index it will need.
create index document_uploader_idx       on document(administration_id, uploaded_by_user_id);
-- FR-DOC-004. Trigram, so a bookkeeper searching a fragment they remember
-- finds it without the query having to be stemmed in the right language.
create index document_search_idx         on document using gin (search_text gin_trgm_ops);
-- FR-DOC-001's integrity check, and duplicate detection (FR-EXP-001g) later.
create index document_content_hash_idx   on document(administration_id, content_hash);

comment on table document is
    'FR-DOC-001. One source document: the reference to its unaltered original, '
    'the hash that proves it is unaltered, and the derived material stored '
    'alongside it. Write-once for its retention period (FR-DOC-005).';

-- ---------------------------------------------------------------------------
-- Retention is computed here, from the fiscal year, whatever the caller said
-- ---------------------------------------------------------------------------
create or replace function document_set_retention() returns trigger as $$
declare
    v_end   date;
    v_admin uuid;
begin
    select f.end_date, f.administration_id into v_end, v_admin
      from fiscal_year f where f.id = new.fiscal_year_id;

    if not found then
        raise exception 'fiscal year % does not exist', new.fiscal_year_id;
    end if;

    -- A document retained against another administration's fiscal year would
    -- expire on that administration's schedule, which is a tenancy defect
    -- wearing a retention defect's clothes.
    if v_admin is distinct from new.administration_id then
        raise exception
            'fiscal year % belongs to administration %, not % (FR-DOC-002)',
            new.fiscal_year_id, v_admin, new.administration_id;
    end if;

    new.retention_until := documents.retention_until(v_end, new.retention_basis);
    return new;
end;
$$ language plpgsql;

-- Fires on UPDATE too, so that re-classifying a document as involving
-- immovable property recomputes 7 years into 10. That is the one legitimate
-- way retention_until changes, and document_original_immutable() below permits
-- exactly it.
create trigger document_set_retention_trg
    before insert or update of fiscal_year_id, retention_basis on document
    for each row execute function document_set_retention();

-- ---------------------------------------------------------------------------
-- FR-DOC-001 / FR-DOC-005: the original is immutable, the derivation is not
-- ---------------------------------------------------------------------------
create or replace function document_original_immutable() returns trigger as $$
begin
    if new.storage_key       is distinct from old.storage_key
       or new.content_hash   is distinct from old.content_hash
       or new.byte_size      is distinct from old.byte_size
       or new.content_type   is distinct from old.content_type
       or new.original_filename is distinct from old.original_filename
       or new.administration_id is distinct from old.administration_id
       or new.organization_id   is distinct from old.organization_id
       or new.fiscal_year_id    is distinct from old.fiscal_year_id
       or new.uploaded_by_user_id is distinct from old.uploaded_by_user_id
       or new.uploaded_at    is distinct from old.uploaded_at
    then
        raise exception
            'a stored document is unaltered (FR-DOC-001): its original, its '
            'hash and its provenance cannot be changed. Derived text, '
            'extracted fields, scan verdict and status may be.';
    end if;

    -- Retention may only ever be EXTENDED. Shortening it is the deletion
    -- FR-DOC-002 forbids, arriving by a quieter route than DELETE.
    if new.retention_until < old.retention_until then
        raise exception
            'retention cannot be shortened (FR-DOC-002): % is earlier than the '
            'stored %. Reclassifying to immovable_property extends it; nothing '
            'shortens it.', new.retention_until, old.retention_until;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger document_original_immutable_trg
    before update on document
    for each row execute function document_original_immutable();

-- ---------------------------------------------------------------------------
-- FR-DOC-005: write-once for the retention period
-- ---------------------------------------------------------------------------
-- Unconditional except for the one documented path, for the reason 0020's
-- immutability triggers give: a BEFORE trigger fires for ledgr_app, for
-- ledgr_migrator, for ledgr_ops with BYPASSRLS and for a superuser at a psql
-- prompt alike. BYPASSRLS skips row-level security and has never skipped a
-- trigger, which is what makes "not deletable by users" true of support
-- tooling as well.
create or replace function document_deletion_guard() returns trigger as $$
declare
    v_approved uuid;
begin
    -- Path 1: retention has run out. PRIV-023's "deleted automatically at the
    -- end of the retention period", and PRIV-030's job is what exercises it.
    --
    -- Strictly `<`, because retention_until is INCLUSIVE: the document is held
    -- THROUGH that date. `<=` would permit removal on the last day of the
    -- seventh year - which is precisely the day an inspection dated to the
    -- deadline asks for. api.documents.retention.is_expired reads it the same
    -- way, and tests/documents/retention_cases.py asserts the two agree at the
    -- boundary rather than near it.
    if old.retention_until < current_date then
        return old;
    end if;

    -- Path 2: FR-DOC-005's documented legal basis and privileged approval.
    -- Read from the request table rather than a session flag, so the record of
    -- WHY survives the transaction that used it.
    select r.id into v_approved
      from document_deletion_request r
     where r.document_id = old.id
       and r.approved_at is not null
       and r.executed_at is null
     limit 1;

    if v_approved is not null then
        return old;
    end if;

    raise exception
        'document % is within its retention period until % and has no approved '
        'deletion request (FR-DOC-005). Deletion before expiry requires a '
        'documented legal basis and a second approver.',
        old.id, old.retention_until;
end;
$$ language plpgsql;

create trigger document_deletion_guard_trg
    before delete on document
    for each row execute function document_deletion_guard();

create or replace function document_no_truncate() returns trigger as $$
begin
    raise exception
        'the document archive is write-once for its retention period '
        '(FR-DOC-005); % cannot be truncated', tg_table_name;
end;
$$ language plpgsql;

-- TRUNCATE is neither UPDATE nor DELETE and fires neither trigger above. It is
-- a statement-level event: TRUNCATE administration CASCADE would otherwise
-- reach this table without naming it.
create trigger document_no_truncate_trg
    before truncate on document
    for each statement execute function document_no_truncate();

-- ===========================================================================
-- FR-DOC-003: documents are linked to postings BIDIRECTIONALLY
-- ===========================================================================
-- A table rather than a column, and that is forced twice over:
--
--   * `journal_entry` is append-only with no UPDATE grant (0020). A document
--     attached to a posting that already exists - which is the ordinary case,
--     since a bookkeeper codes an invoice after it arrives - could not be
--     recorded on the entry at all.
--   * The relationship is many-to-many in both directions. One invoice can
--     support several entries (a cost split across periods or cost centres);
--     one entry can rest on several documents (invoice plus delivery note).
--     `journal_entry.document_reference` (0020) stays what it is: FR-GL-004's
--     free-text reference, a human's note, not a foreign key.
--
-- "Bidirectional" is then a property of the index set rather than of two
-- columns: both directions are a single index lookup, and neither side owns
-- the other.
create table document_posting_link (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    document_id         uuid not null references document(id),
    journal_entry_id    uuid not null references journal_entry(id),

    linked_by_user_id   uuid references users(id),
    linked_at           timestamptz not null default now(),

    -- Detached rather than deleted, following the convention 0013 sets for
    -- profile assignments: a link that was once asserted is evidence about
    -- what somebody believed, and the correction is a new fact rather than the
    -- disappearance of the old one.
    detached_at         timestamptz,
    detached_by_user_id uuid references users(id),
    detached_reason     text,

    constraint document_posting_link_detach_recorded check (
        (detached_at is null) = (detached_by_user_id is null)
        and (detached_at is null) = (detached_reason is null)
    )
);

-- One live link per pair; a detached one does not block re-linking.
create unique index document_posting_link_live_idx
    on document_posting_link(document_id, journal_entry_id)
    where detached_at is null;

create index document_posting_link_document_idx
    on document_posting_link(document_id) where detached_at is null;
create index document_posting_link_entry_idx
    on document_posting_link(journal_entry_id) where detached_at is null;
create index document_posting_link_administration_idx
    on document_posting_link(administration_id);

comment on table document_posting_link is
    'FR-DOC-003. The bidirectional link between a source document and a '
    'posting. Many-to-many, because an invoice can support several entries and '
    'an entry can rest on several documents.';

create or replace function document_posting_link_immutable() returns trigger as $$
begin
    if tg_op = 'DELETE' then
        raise exception
            'a document-posting link is detached, not deleted (FR-DOC-003): '
            'set detached_at with a reason, so the record of what was once '
            'believed survives the correction.';
    end if;

    if new.document_id      is distinct from old.document_id
       or new.journal_entry_id is distinct from old.journal_entry_id
       or new.administration_id is distinct from old.administration_id
       or new.organization_id   is distinct from old.organization_id
       or new.linked_by_user_id is distinct from old.linked_by_user_id
       or new.linked_at      is distinct from old.linked_at
    then
        raise exception 'a document-posting link is immutable except for detachment';
    end if;

    if old.detached_at is not null and new.detached_at is distinct from old.detached_at then
        raise exception 'a detached link cannot be reattached or re-dated';
    end if;

    return new;
end;
$$ language plpgsql;

create trigger document_posting_link_immutable_trg
    before update or delete on document_posting_link
    for each row execute function document_posting_link_immutable();

-- A link must not reach across administrations: the document, the entry and
-- the link all belong to one set of books, and a link that crossed would make
-- one tenant's posting cite another tenant's evidence.
create or replace function document_posting_link_same_tenant() returns trigger as $$
declare
    v_doc_admin   uuid;
    v_entry_admin uuid;
begin
    select administration_id into v_doc_admin   from document      where id = new.document_id;
    select administration_id into v_entry_admin from journal_entry where id = new.journal_entry_id;

    if v_doc_admin is distinct from new.administration_id
       or v_entry_admin is distinct from new.administration_id then
        raise exception
            'a document-posting link stays inside one administration '
            '(CLAUDE.md rule 1): document is in %, entry is in %, link claims %',
            v_doc_admin, v_entry_admin, new.administration_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger document_posting_link_same_tenant_trg
    before insert on document_posting_link
    for each row execute function document_posting_link_same_tenant();

create trigger document_posting_link_no_truncate_trg
    before truncate on document_posting_link
    for each statement execute function document_no_truncate();

-- ===========================================================================
-- FR-DOC-005: deletion before expiry
-- ===========================================================================
-- "a documented legal basis and privileged approval" is two people and a
-- reason, so it is a row rather than a flag: the basis has to survive the
-- deletion it authorised, or the archive cannot answer why something is
-- missing from it.
create table document_deletion_request (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    document_id         uuid not null references document(id),

    -- "documented legal basis". Free text with a floor on its length, because
    -- the failure this guards is a one-word basis - "GDPR", "asked" - that
    -- reads as a reason and answers nothing later.
    legal_basis         text not null check (length(btrim(legal_basis)) >= 20),

    requested_by_user_id uuid not null references users(id),
    requested_at        timestamptz not null default now(),

    approved_by_user_id uuid references users(id),
    approved_at         timestamptz,

    executed_at         timestamptz,

    constraint document_deletion_approval_recorded check (
        (approved_at is null) = (approved_by_user_id is null)
    ),
    -- "privileged approval": a second person. A request that approves itself
    -- is a request, and SoD (§8.4) is the same argument this schema already
    -- makes about payment release.
    constraint document_deletion_two_person check (
        approved_by_user_id is null or approved_by_user_id <> requested_by_user_id
    ),
    constraint document_deletion_executed_after_approval check (
        executed_at is null or approved_at is not null
    )
);

create index document_deletion_request_document_idx
    on document_deletion_request(document_id) where executed_at is null;
create index document_deletion_request_administration_idx
    on document_deletion_request(administration_id);

comment on table document_deletion_request is
    'FR-DOC-005. The documented legal basis and second-person approval that '
    'deletion within the retention period requires. Outlives the document it '
    'authorises, so the archive can say why something is missing.';

create or replace function document_deletion_request_immutable() returns trigger as $$
begin
    if tg_op = 'DELETE' then
        raise exception
            'a deletion request is the record of why a document went; it '
            'cannot itself be deleted (FR-DOC-005)';
    end if;
    if new.legal_basis is distinct from old.legal_basis
       or new.document_id is distinct from old.document_id
       or new.requested_by_user_id is distinct from old.requested_by_user_id
       or new.requested_at is distinct from old.requested_at
    then
        raise exception 'the basis and provenance of a deletion request are immutable';
    end if;
    if old.approved_at is not null and new.approved_at is distinct from old.approved_at then
        raise exception 'an approval cannot be withdrawn or re-dated';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger document_deletion_request_immutable_trg
    before update or delete on document_deletion_request
    for each row execute function document_deletion_request_immutable();

create trigger document_deletion_request_no_truncate_trg
    before truncate on document_deletion_request
    for each statement execute function document_no_truncate();

-- The only path that removes a document row, and it is granted to ledgr_ops
-- alone - never to the application role. FR-DOC-002's "not deletable by users"
-- is that grant, and the trigger above is what makes it true of ledgr_ops too
-- until an approval exists.
create or replace function documents.delete_with_approval(
    p_request_id     uuid,
    p_executed_by    uuid
)
returns void
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_request document_deletion_request%rowtype;
begin
    select * into v_request from document_deletion_request where id = p_request_id;
    if not found then
        raise exception 'deletion request % does not exist', p_request_id;
    end if;
    if v_request.approved_at is null then
        raise exception
            'deletion request % is not approved. FR-DOC-005 requires a second '
            'person, not only a documented basis.', p_request_id;
    end if;
    if v_request.executed_at is not null then
        raise exception 'deletion request % has already been executed', p_request_id;
    end if;

    -- Marked executed BEFORE the delete, so the request cannot be replayed
    -- against a document that has since been re-uploaded under the same id.
    update document_deletion_request
       set executed_at = now()
     where id = p_request_id;

    delete from document where id = v_request.document_id;
end;
$$;

comment on function documents.delete_with_approval(uuid, uuid) is
    'FR-DOC-005. The only path that deletes a document inside its retention '
    'period, and only against an approved request. Granted to ledgr_ops alone.';

-- ===========================================================================
-- FR-DOC-003: the completeness report
-- ===========================================================================
-- "a posting without a source document is flagged in a completeness report".
--
-- Built for the reason 0020 gives about its own gap report: the check on the
-- thing that makes it empty. A bookkeeper's month-end question is "what am I
-- missing evidence for", and the answer has to be a list rather than a search.
create or replace function documents.postings_without_documents(
    p_administration_id uuid,
    p_fiscal_year_id    uuid default null
)
returns table (
    journal_entry_id   uuid,
    administration_id  uuid,
    entry_date         date,
    entry_number       bigint,
    description        text,
    document_reference text,
    posted_at          timestamptz
)
language sql
stable
as $$
    select e.id, e.administration_id, e.entry_date, e.entry_number,
           e.description, e.document_reference, e.posted_at
      from journal_entry e
     where e.administration_id = p_administration_id
       and (p_fiscal_year_id is null or e.fiscal_year_id = p_fiscal_year_id)
       and not exists (
           select 1 from document_posting_link l
            where l.journal_entry_id = e.id
              and l.detached_at is null
       )
     order by e.entry_date, e.entry_number;
$$;

comment on function documents.postings_without_documents(uuid, uuid) is
    'FR-DOC-003. Postings with no live source document linked to them.';

-- ---------------------------------------------------------------------------
-- FR-DOC-001's claim, checked
-- ---------------------------------------------------------------------------
-- "Stored unaltered" is only a guarantee if something compares the stored
-- bytes against the hash taken at upload. This reports what the DATABASE can
-- see on its own - rows whose retention has quietly gone backwards, or whose
-- scan never completed - and the byte-level comparison belongs to the job in
-- apps/api/scripts, which is the only thing that can read the blob.
create or replace function documents.integrity_deviations(
    p_administration_id uuid default null
)
returns table (
    document_id       uuid,
    administration_id uuid,
    deviation         text,
    detail            text
)
language sql
stable
as $$
    select d.id, d.administration_id,
           case
               when d.retention_until < documents.retention_until(
                        f.end_date, d.retention_basis)
                   then 'retention_shortened'
               when d.scan_status = 'pending'
                    and d.uploaded_at < now() - interval '1 hour'
                   then 'scan_never_completed'
               when d.scan_status = 'infected' then 'infected_document_retained'
           end,
           case
               when d.retention_until < documents.retention_until(
                        f.end_date, d.retention_basis)
                   then format('retained until %s; the rule gives %s',
                               d.retention_until,
                               documents.retention_until(f.end_date, d.retention_basis))
               when d.scan_status = 'pending'
                   then format('uploaded %s and still unscanned', d.uploaded_at)
               else 'scanner reported an infection and the row is still present'
           end
      from document d
      join fiscal_year f on f.id = d.fiscal_year_id
     where (p_administration_id is null or d.administration_id = p_administration_id)
       and (
           d.retention_until < documents.retention_until(f.end_date, d.retention_basis)
           or (d.scan_status = 'pending' and d.uploaded_at < now() - interval '1 hour')
           or d.scan_status = 'infected'
       );
$$;

comment on function documents.integrity_deviations(uuid) is
    'FR-DOC-001/002. Documents whose retention or scan state is not what the '
    'rules give. Empty on a healthy archive.';

-- ===========================================================================
-- Ownership, grants and privileges
-- ===========================================================================
alter table document                  owner to ledgr_migrator;
alter table document_posting_link     owner to ledgr_migrator;
alter table document_deletion_request owner to ledgr_migrator;

-- No DELETE to ledgr_app on any of the three. FR-DOC-002's "not deletable by
-- users" is this line: the application role - the one every request runs as -
-- has no way to express the statement, quite apart from the triggers that
-- would refuse it. The triggers exist because ledgr_ops does have the grant
-- and CMP-009's reasoning applies here too.
grant select, insert, update on document                  to ledgr_app;
grant select, insert, update on document_posting_link     to ledgr_app;
grant select, insert, update on document_deletion_request to ledgr_app;

grant select on document, document_posting_link, document_deletion_request to ledgr_ops;
grant delete on document to ledgr_ops;

-- ledgr_ledger reads the link table for nothing today; the completeness report
-- runs as the caller. Named here so the absence is a decision rather than an
-- oversight: the ledger bounded context (non-negotiable #1) does not read the
-- document archive, and the archive does not write postings.

grant usage on schema documents to ledgr_app, ledgr_ops;
revoke all on all functions in schema documents from public;

grant execute on function documents.retention_until(date, text)
    to ledgr_app, ledgr_ops;
grant execute on function documents.postings_without_documents(uuid, uuid)
    to ledgr_app, ledgr_ops;
grant execute on function documents.integrity_deviations(uuid)
    to ledgr_app, ledgr_ops;
-- The privileged half of FR-DOC-005. Not granted to ledgr_app: a request
-- handler must not be able to delete a document even with an approval row in
-- front of it, because then a bug in one endpoint is a hole in the archive.
grant execute on function documents.delete_with_approval(uuid, uuid) to ledgr_ops;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
-- FORCE, so the definer function above is bound by these policies too. Without
-- it Postgres exempts a table's owner and documents.delete_with_approval
-- becomes a cross-tenant read path - the same trap 0020 documents.
alter table document                  enable row level security;
alter table document                  force row level security;
alter table document_posting_link     enable row level security;
alter table document_posting_link     force row level security;
alter table document_deletion_request enable row level security;
alter table document_deletion_request force row level security;

create policy document_select on document
    for select using (app.has_administration_access(administration_id));
create policy document_insert on document
    for insert with check (app.has_administration_access(administration_id));
create policy document_update on document
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
-- No DELETE policy, matching 0001's stance on administration: the statement
-- has no grant and no policy, so it fails twice.

create policy document_posting_link_select on document_posting_link
    for select using (app.has_administration_access(administration_id));
create policy document_posting_link_insert on document_posting_link
    for insert with check (app.has_administration_access(administration_id));
create policy document_posting_link_update on document_posting_link
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy document_deletion_request_select on document_deletion_request
    for select using (app.has_administration_access(administration_id));
create policy document_deletion_request_insert on document_deletion_request
    for insert with check (app.has_administration_access(administration_id));
create policy document_deletion_request_update on document_deletion_request
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

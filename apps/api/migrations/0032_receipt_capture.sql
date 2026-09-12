-- 0032_receipt_capture.sql
-- FR-EXP-001, FR-EXP-001a (PRD §6.7). See
-- docs/decisions/ADR-031-receipt-capture.md.
--
--   FR-EXP-001   Receipt capture by camera (single tap from the home screen,
--                multi-page, auto edge detection, deskew, glare and blur
--                warning with retake prompt) and by upload (drag-and-drop or
--                file picker, accepting JPEG, PNG, HEIC, PDF and multi-page
--                PDF). BOTH PATHS LAND IN THE SAME PLACE.
--   FR-EXP-001a  Batch capture: photograph or upload several receipts in one
--                session, each becoming a separate expense, with a review list
--                before posting.
--
-- ===========================================================================
-- Two different "multiple", and confusing them is the whole problem
-- ===========================================================================
--
-- FR-EXP-001 and FR-EXP-001a both say "several", and they mean opposite
-- things:
--
--   MULTI-PAGE (FR-EXP-001)   several images, ONE receipt, ONE expense. A
--                             restaurant bill photographed front and back; an
--                             invoice whose second page carries the VAT
--                             breakdown.
--   BATCH (FR-EXP-001a)       several receipts, SEVERAL expenses. A shoebox
--                             emptied in one sitting.
--
-- Get them the same way round and a three-page invoice becomes three expenses
-- claimed three times, or an afternoon's receipts collapse into one. Both are
-- silent: the numbers look plausible either way.
--
-- So the schema names them separately and cannot express the confusion:
--
--   capture_session  one sitting             (FR-EXP-001a's "session")
--     capture_item   one receipt, one expense (FR-EXP-001a's "each")
--       capture_page one stored original      (FR-EXP-001's "multi-page")
--
-- `expense` hangs off `capture_item` with a UNIQUE constraint, so "each
-- becoming a separate expense" is a property of the schema rather than of the
-- code that happens to write it.
--
-- ===========================================================================
-- A multi-page PDF is ONE page row
-- ===========================================================================
--
-- The counter-intuitive case, and it follows from FR-DOC-001. An uploaded
-- 4-page PDF is one file, and the original must be stored unaltered - so it is
-- one `document`, and therefore one `capture_page`. Splitting it into four
-- would mean storing four things that are not what the user gave us.
--
-- `capture_page` counts STORED ORIGINALS, not sheets of paper. Four camera
-- shots of a four-page invoice are four pages; a four-page PDF of the same
-- invoice is one. Both are one item and one expense, which is the answer that
-- matters.
--
-- ===========================================================================
-- "Both paths land in the same place"
-- ===========================================================================
--
-- `source` is recorded on the page and is used for NOTHING. There is no
-- branch on it here, none in api.expenses.capture, and one intake endpoint for
-- both paths. It exists so that "how did this arrive" is answerable in support
-- and in the MOB-003 offline queue, not so that anything can behave
-- differently - the moment a query says `where source = 'camera'` the two
-- paths have stopped landing in the same place.

begin;

create schema if not exists expenses;
comment on schema expenses is
    'FR-EXP. Functions over captured receipts: the review list FR-EXP-001a '
    'shows before posting, and the checks that stop a session being finalised '
    'into expenses that rest on nothing.';

-- ===========================================================================
-- capture_session
-- ===========================================================================
create table capture_session (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- The person emptying the shoebox. Every expense the session produces is
    -- attributed to them (§8.4's Expense Submitter submits their OWN).
    opened_by_user_id   uuid not null references users(id),
    opened_at           timestamptz not null default now(),

    -- FR-EXP-001a's "before posting": the review list is what happens between
    -- these two timestamps. Nothing can be added after the second.
    finalised_at        timestamptz,
    finalised_by_user_id uuid references users(id),

    constraint capture_session_finalised_recorded check (
        (finalised_at is null) = (finalised_by_user_id is null)
    )
);

create index capture_session_administration_idx
    on capture_session(administration_id, opened_at desc);
create index capture_session_open_idx
    on capture_session(administration_id, opened_by_user_id) where finalised_at is null;

comment on table capture_session is
    'FR-EXP-001a. One sitting: several receipts captured together, each '
    'becoming a separate expense, reviewed as a list before posting.';

-- ===========================================================================
-- capture_item - one receipt, and therefore one expense
-- ===========================================================================
create table capture_item (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    session_id          uuid not null references capture_session(id),

    -- Order in the session, so the review list is the order things were
    -- captured in. A person checking a list against a pile of paper is
    -- checking it in the order they put the paper down.
    position            integer not null check (position > 0),

    -- FR-EXP-001a's review list is where an item can be dropped: a blurred
    -- retake, a duplicate, something that turned out not to be a receipt.
    -- Discarded rather than deleted, so the documents already stored against
    -- it keep their provenance - they are inside their FR-DOC-002 retention
    -- period and are not ours to remove.
    discarded_at        timestamptz,
    discarded_by_user_id uuid references users(id),
    discarded_reason    text,

    created_at          timestamptz not null default now(),

    constraint capture_item_discarded_recorded check (
        (discarded_at is null) = (discarded_by_user_id is null)
    ),
    constraint capture_item_position_unique unique (session_id, position)
);

create index capture_item_session_idx on capture_item(session_id, position);
create index capture_item_administration_idx on capture_item(administration_id);

comment on table capture_item is
    'FR-EXP-001a. One receipt within a session. Becomes exactly one expense - '
    'see the UNIQUE constraint on expense.capture_item_id.';

-- ===========================================================================
-- capture_page - one stored original
-- ===========================================================================
create table capture_page (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    item_id             uuid not null references capture_item(id),

    -- FR-EXP-001d: "The image is retained as the source document under §6.10".
    -- Not a copy of the bytes - a reference to the archive that already
    -- guarantees they are unaltered, retained and write-once (0031).
    document_id         uuid not null references document(id),

    page_number         smallint not null check (page_number > 0),

    -- Recorded, never branched on. See the header.
    source              text not null check (source in ('camera', 'upload')),

    created_at          timestamptz not null default now(),

    constraint capture_page_number_unique unique (item_id, page_number),
    -- One document belongs to one page. Two pages citing one original would
    -- make a single upload count twice in a review list.
    constraint capture_page_document_unique unique (document_id)
);

create index capture_page_item_idx on capture_page(item_id, page_number);
create index capture_page_administration_idx on capture_page(administration_id);

comment on table capture_page is
    'FR-EXP-001. One stored original within a receipt. A multi-page PDF is ONE '
    'row: the original is the file, and FR-DOC-001 stores it unaltered.';

-- ===========================================================================
-- expense - FR-EXP-001a's "each becoming a separate expense"
-- ===========================================================================
-- Every field below except the linkage is NULLABLE, and that is FR-EXP-001c
-- rather than laziness:
--
--   "Fields are pre-filled on a best-effort basis in P0 and are always
--    editable. ... the product never blocks on extraction being available."
--
-- A draft expense with nothing filled in is the correct state for a receipt
-- that has just been photographed. Requiring an amount at capture time would
-- make the camera path block on either OCR or typing, which is the failure
-- FR-EXP-001c names.
--
-- The columns are FR-EXP-001b's enumerated list - "date, supplier, gross
-- amount, VAT rate, category" - plus FR-EXP-001e's payment method. THE FORM IS
-- NOT BUILT and neither is the VAT/net derivation; these are the columns those
-- will write. They are here now because "each becoming a separate expense"
-- needs somewhere for an expense to be.
create table expense (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- One item, one expense. The UNIQUE is what makes FR-EXP-001a's "each"
    -- structural: no code path can produce two expenses for one receipt or
    -- one expense for two.
    capture_item_id     uuid not null unique references capture_item(id),

    -- draft     captured, not yet reviewed
    -- ready     reviewed and released for posting (FR-EXP-002's approval
    --           workflow and FR-EXP-003's reimbursement run take it from here)
    status              text not null default 'draft'
                            check (status in ('draft', 'ready')),

    -- FR-EXP-001b's minimum, and FR-EXP-001e's payment method.
    expense_date        date,
    supplier            text,
    -- NFR-031 / CLAUDE.md rule four: decimal with a defined scale, matching
    -- journal_line.debit/credit in 0020. Never a float, at any point.
    gross_amount        numeric(19, 2),
    -- A percentage, not a fraction: 21.00 is 21%. Scale 2 because Dutch rates
    -- are whole numbers today and CMP-014 makes a future fractional rate a
    -- data change rather than a migration.
    vat_rate            numeric(5, 2) check (vat_rate is null or vat_rate >= 0),
    category            text,
    -- FR-EXP-001e: "captured at entry ... because it determines the posting
    -- and cannot be reliably inferred later".
    payment_method      text check (payment_method in (
                            'business_account', 'business_card', 'personal_reimbursable'
                        )),

    submitted_by_user_id uuid not null references users(id),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint expense_gross_amount_non_negative check (
        gross_amount is null or gross_amount >= 0
    )
);

create index expense_administration_idx on expense(administration_id, created_at desc);
create index expense_status_idx on expense(administration_id, status);
create index expense_submitter_idx on expense(administration_id, submitted_by_user_id);

comment on table expense is
    'FR-EXP-001a. One receipt''s expense. Fields are nullable because '
    'FR-EXP-001c says the product never blocks on extraction; the form that '
    'fills them is FR-EXP-001b and is not built.';

comment on column expense.gross_amount is
    'NFR-031: decimal, never float, matching journal_line''s scale.';

-- ===========================================================================
-- What a session may still do
-- ===========================================================================
-- FR-EXP-001a's "review list before posting" is a boundary: once the list has
-- been accepted, the session is closed to new evidence. Enforced here rather
-- than in the service, because a second request racing a finalisation would
-- otherwise slip a receipt in behind the review that was supposed to cover it.
create or replace function capture_session_is_open(p_session_id uuid) returns boolean
language sql
stable
as $$
    select finalised_at is null from capture_session where id = p_session_id;
$$;

create or replace function capture_item_session_open() returns trigger as $$
begin
    if not capture_session_is_open(new.session_id) then
        raise exception
            'capture session % has been finalised; its review list is what was '
            'accepted, and a receipt added afterwards would not have been in it '
            '(FR-EXP-001a)', new.session_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger capture_item_session_open_trg
    before insert on capture_item
    for each row execute function capture_item_session_open();

create or replace function capture_page_session_open() returns trigger as $$
declare
    v_session uuid;
begin
    select session_id into v_session from capture_item where id = new.item_id;
    if not capture_session_is_open(v_session) then
        raise exception
            'capture session % has been finalised; no further pages (FR-EXP-001a)',
            v_session;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger capture_page_session_open_trg
    before insert on capture_page
    for each row execute function capture_page_session_open();

-- ---------------------------------------------------------------------------
-- Everything in one session belongs to one administration
-- ---------------------------------------------------------------------------
-- CLAUDE.md rule 1. A page whose document belongs to another administration
-- would put one client's receipt into another client's expense - the
-- wrong-client failure FR-FRM-000a calls the worst in this product, arriving
-- through a table nobody was watching.
create or replace function capture_page_same_tenant() returns trigger as $$
declare
    v_doc_admin  uuid;
    v_item_admin uuid;
begin
    select administration_id into v_doc_admin  from document      where id = new.document_id;
    select administration_id into v_item_admin from capture_item  where id = new.item_id;

    if v_doc_admin is distinct from new.administration_id
       or v_item_admin is distinct from new.administration_id then
        raise exception
            'a capture page stays inside one administration: document is in %, '
            'item is in %, page claims % (CLAUDE.md rule 1)',
            v_doc_admin, v_item_admin, new.administration_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger capture_page_same_tenant_trg
    before insert on capture_page
    for each row execute function capture_page_same_tenant();

create or replace function expense_same_tenant_as_item() returns trigger as $$
declare
    v_item_admin uuid;
begin
    select administration_id into v_item_admin from capture_item where id = new.capture_item_id;
    if v_item_admin is distinct from new.administration_id then
        raise exception
            'an expense belongs to the administration its receipt was captured '
            'in: item is in %, expense claims % (CLAUDE.md rule 1)',
            v_item_admin, new.administration_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger expense_same_tenant_as_item_trg
    before insert or update on expense
    for each row execute function expense_same_tenant_as_item();

-- ===========================================================================
-- FR-EXP-001a: the review list
-- ===========================================================================
-- One row per item, with what a person needs to decide whether it is right:
-- how many originals it holds, what they are, and whether the same file was
-- captured twice in this sitting.
--
-- The duplicate column is EXACT - same bytes, same hash - and that is not
-- FR-EXP-001g. That requirement matches on supplier, date and amount, which
-- needs extraction (FR-EXP-001c, P1) and is not built. What is here is the
-- case extraction is not needed for and which a batch makes common: the same
-- receipt photographed twice, or a file dropped in twice.
create or replace function expenses.review_list(p_session_id uuid)
returns table (
    item_id          uuid,
    position         integer,
    expense_id       uuid,
    page_count       bigint,
    content_types    text[],
    total_bytes      bigint,
    discarded        boolean,
    duplicate_of     uuid
)
language sql
stable
as $$
    with pages as (
        select p.item_id,
               count(*)                            as page_count,
               array_agg(d.content_type order by p.page_number) as content_types,
               sum(d.byte_size)                    as total_bytes,
               min(encode(d.content_hash, 'hex'))  as first_hash
          from capture_page p
          join document d on d.id = p.document_id
         group by p.item_id
    ),
    -- The earliest item in this session carrying the same leading hash. An
    -- item is a duplicate OF that one, and the first is not a duplicate of
    -- itself.
    duplicates as (
        select i.id as item_id,
               first_value(i.id) over (
                   partition by pg.first_hash order by i.position
               ) as canonical_item
          from capture_item i
          join pages pg on pg.item_id = i.id
         where i.session_id = p_session_id
    )
    select i.id, i.position, e.id,
           coalesce(pg.page_count, 0), pg.content_types, coalesce(pg.total_bytes, 0),
           i.discarded_at is not null,
           case when dup.canonical_item <> i.id then dup.canonical_item end
      from capture_item i
      left join pages pg      on pg.item_id = i.id
      left join expense e     on e.capture_item_id = i.id
      left join duplicates dup on dup.item_id = i.id
     where i.session_id = p_session_id
     order by i.position;
$$;

comment on function expenses.review_list(uuid) is
    'FR-EXP-001a. The review list shown before posting: one row per receipt, '
    'with its page count, its stored types, and any exact duplicate captured '
    'earlier in the same session.';

-- ---------------------------------------------------------------------------
-- Items that would produce an expense with no evidence behind it
-- ---------------------------------------------------------------------------
-- An item with no pages is a receipt nobody actually captured - a request that
-- opened an item and then failed. Finalising a session containing one would
-- create an expense resting on nothing, which is the state FR-DOC-003's
-- completeness report exists to find after the fact. Cheaper to refuse.
create or replace function expenses.items_without_pages(p_session_id uuid)
returns setof uuid
language sql
stable
as $$
    select i.id
      from capture_item i
     where i.session_id = p_session_id
       and i.discarded_at is null
       and not exists (select 1 from capture_page p where p.item_id = i.id);
$$;

-- ===========================================================================
-- Ownership, grants and privileges
-- ===========================================================================
alter table capture_session owner to ledgr_migrator;
alter table capture_item    owner to ledgr_migrator;
alter table capture_page    owner to ledgr_migrator;
alter table expense         owner to ledgr_migrator;

-- No DELETE, following this schema's convention throughout: an item is
-- discarded and a session is finalised. A capture that happened is a fact
-- about what somebody did, and the documents behind it are inside their
-- FR-DOC-002 retention period regardless.
grant select, insert, update on capture_session to ledgr_app;
grant select, insert, update on capture_item    to ledgr_app;
grant select, insert, update on capture_page    to ledgr_app;
grant select, insert, update on expense         to ledgr_app;

grant select on capture_session, capture_item, capture_page, expense to ledgr_ops;

grant usage on schema expenses to ledgr_app, ledgr_ops;
revoke all on all functions in schema expenses from public;
grant execute on function expenses.review_list(uuid)          to ledgr_app, ledgr_ops;
grant execute on function expenses.items_without_pages(uuid)  to ledgr_app, ledgr_ops;
grant execute on function capture_session_is_open(uuid)       to ledgr_app, ledgr_ops;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
alter table capture_session enable row level security;
alter table capture_session force row level security;
alter table capture_item    enable row level security;
alter table capture_item    force row level security;
alter table capture_page    enable row level security;
alter table capture_page    force row level security;
alter table expense         enable row level security;
alter table expense         force row level security;

create policy capture_session_select on capture_session
    for select using (app.has_administration_access(administration_id));
create policy capture_session_insert on capture_session
    for insert with check (app.has_administration_access(administration_id));
create policy capture_session_update on capture_session
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy capture_item_select on capture_item
    for select using (app.has_administration_access(administration_id));
create policy capture_item_insert on capture_item
    for insert with check (app.has_administration_access(administration_id));
create policy capture_item_update on capture_item
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy capture_page_select on capture_page
    for select using (app.has_administration_access(administration_id));
create policy capture_page_insert on capture_page
    for insert with check (app.has_administration_access(administration_id));

create policy expense_select on expense
    for select using (app.has_administration_access(administration_id));
create policy expense_insert on expense
    for insert with check (app.has_administration_access(administration_id));
create policy expense_update on expense
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

-- 0024_chart_of_accounts.sql
-- FR-GL-005, FR-ONB-004, FR-ONB-005, CMP-003 (PRD §6.1, §6.2, §11).
-- See docs/decisions/ADR-026-chart-of-accounts.md.
--
--   FR-GL-005   chart of accounts with account type, RGS reference code, VAT
--               default, and blocked/active state
--   FR-ONB-004  legal form drives the default chart of accounts
--   FR-ONB-005  chart seeded from the RGS MKB profile matching the legal form;
--               the user may extend it but not break RGS mapping integrity
--   CMP-003     RGS 3.8 (and successors) maintained on the chart, with a
--               versioned upgrade path when a new RGS version is released
--   CMP-014     rule changes are effective-dated; history keeps the rules that
--               applied at the time
--
-- 0020 already created ledger_account with account_type, rgs_code,
-- default_vat_code and status, so FR-GL-005's COLUMNS exist. What did not
-- exist is any of the meaning: rgs_code was free text with no referent, so
-- 'BLimKas', 'BLIMKAS' and 'not sure yet' were equally valid, and nothing
-- connected an account to the RGS version its code came from.
--
-- ===========================================================================
-- The whole design, in one sentence
-- ===========================================================================
--
-- An RGS version is a row, its elements are rows, the MKB profile per legal
-- form is rows, and the map from one version to the next is rows - so
-- releasing support for RGS 4.0 is loading a file, not shipping a migration.
--
-- CMP-003 asks for "a versioned upgrade path when a new RGS version is
-- released". A hardcoded seed cannot have one: to upgrade you would edit the
-- constant, deploy, and hope every administration's chart still meant what it
-- said. With the version as data, an upgrade is:
--
--   1. load the new version's file          (ledger.load_rgs_version)
--   2. ask what it would do to this chart   (ledger.plan_rgs_upgrade)
--   3. apply it, or refuse and show what needs a human
--                                           (ledger.apply_rgs_upgrade)
--
-- and the old version stays in the database, because CMP-014 wants historical
-- periods to keep the rules that applied at the time. Nothing is ever
-- rewritten in place.
--
-- ===========================================================================
-- Reference data is append-only, for the same reason postings are
-- ===========================================================================
--
-- A published RGS version is a fixed publication. If an element's account_type
-- could be edited after accounts map to it, every one of those accounts would
-- silently change meaning - the XAF export (CMP-002) and the SBR filing
-- (CMP-004) would both move, with nothing recording that they had.
--
-- So rgs_element, rgs_profile_account and rgs_code_mapping take no UPDATE and
-- no DELETE from anyone, enforced the way 0019 and 0020 do it: unconditional
-- RAISE triggers plus withheld privileges plus a NOLOGIN owner. Correcting a
-- published version means publishing another one. rgs_version itself takes one
-- narrow UPDATE - its lifecycle status - through a guarded transition.
--
-- ===========================================================================
-- "May extend but not break RGS mapping integrity"
-- ===========================================================================
--
-- FR-ONB-005's second clause is the interesting half, because "integrity" has
-- to mean something checkable. Four things, each a trigger so that they bind
-- every writer rather than only callers of the API:
--
--   1. An account's rgs_code must EXIST, in the RGS version the administration
--      is pinned to. A composite foreign key, not a text column.
--   2. The version is DERIVED from the administration's pin, never supplied.
--      A caller that could name its own version could map an account to a
--      withdrawn code from a superseded release.
--   3. The element must be POSTABLE. RGS headings exist to hold a subtree; an
--      account mapped to one puts an amount at a level the taxonomy expects to
--      be a sum of its children.
--   4. The element's account_type must MATCH the account's. This is the one
--      that matters most in practice: an expense account mapped to a revenue
--      element balances perfectly, reports perfectly, and files wrongly.
--
-- What the user CAN do, and must be able to: add accounts, block accounts, and
-- correct a mapping to another element of the same type. An account with no
-- RGS code at all also stays legal - 0020 made rgs_code nullable deliberately,
-- because a customer may carry an account with no RGS equivalent - but it is
-- reported by ledger.rgs_readiness() rather than passing silently.

begin;

-- ===========================================================================
-- Legal form vocabulary (FR-ONB-004)
-- ===========================================================================
-- administration.legal_form is free text and already holds values like 'BV',
-- which arrived from the KvK API (FR-ONB-002) in whatever shape the KvK used.
-- Constraining that column now would fail on existing rows and break
-- NFR-044's backward compatibility, and it would be the wrong fix anyway: the
-- KvK's vocabulary is not ours to define.
--
-- So the raw value stays as captured, and this table maps it to the canonical
-- form the profiles are keyed on. A form nobody has an alias for resolves to
-- NULL, and seeding raises a message naming the value it could not place -
-- which is a better failure than seeding the wrong chart.
create table legal_form_alias (
    alias           text primary key,
    legal_form      text not null check (
        legal_form in ('eenmanszaak', 'vof', 'bv', 'stichting', 'vereniging')
    )
);

comment on table legal_form_alias is
    'FR-ONB-004. Maps captured legal_form text (KvK vocabulary, user input) to '
    'the five forms the PRD names. Lookup is case-insensitive.';

insert into legal_form_alias (alias, legal_form) values
    ('eenmanszaak', 'eenmanszaak'),
    ('ez', 'eenmanszaak'),
    ('zzp', 'eenmanszaak'),
    ('sole trader', 'eenmanszaak'),
    ('vof', 'vof'),
    ('v.o.f.', 'vof'),
    ('vennootschap onder firma', 'vof'),
    ('maatschap', 'vof'),
    ('bv', 'bv'),
    ('b.v.', 'bv'),
    ('besloten vennootschap', 'bv'),
    ('besloten vennootschap met beperkte aansprakelijkheid', 'bv'),
    ('stichting', 'stichting'),
    ('foundation', 'stichting'),
    ('vereniging', 'vereniging'),
    ('association', 'vereniging');

create or replace function app.canonical_legal_form(p_raw text)
returns text
language sql
stable
as $$
    select a.legal_form
      from legal_form_alias a
     where a.alias = lower(btrim(coalesce(p_raw, '')));
$$;

-- ===========================================================================
-- RGS versions (CMP-003)
-- ===========================================================================
create table rgs_version (
    id              uuid primary key default gen_random_uuid(),

    -- '3.8', '4.0'. The publication's own identifier, not ours.
    version         text not null unique,

    status          text not null default 'draft' check (
        status in ('draft', 'current', 'superseded')
    ),

    -- CMP-014. Nullable because a provisional dataset has no publication date
    -- to claim, and inventing one would be worse than leaving it empty.
    published_at    date,
    effective_from  date,

    -- Provenance, and the reason it is a column rather than a comment: an
    -- administration pinned to a dataset that was never verified against the
    -- official publication must be reportable. See ledger.rgs_readiness().
    source          text not null check (
        source in ('official-publication', 'provisional-subset')
    ),
    source_note     text,
    -- sha256 of the loaded document. Re-loading the same file is a no-op;
    -- loading a DIFFERENT file under the same version name is refused, because
    -- a published version is frozen.
    source_checksum text not null,

    -- The version this one supersedes, which is what rgs_code_mapping's rows
    -- are keyed against. NULL for the first version loaded.
    supersedes_id   uuid references rgs_version(id),

    loaded_at       timestamptz not null default now()
);

create unique index rgs_version_one_current_idx
    on rgs_version((status))
    where status = 'current';

comment on index rgs_version_one_current_idx is
    'CMP-003. At most one version is current at a time; a new release makes '
    'the previous one superseded rather than deleting it (CMP-014 - history '
    'keeps the rules that applied at the time).';

-- ---------------------------------------------------------------------------
-- rgs_element - one reference code within a version
-- ---------------------------------------------------------------------------
create table rgs_element (
    id              uuid primary key default gen_random_uuid(),
    rgs_version_id  uuid not null references rgs_version(id),

    -- The referentiecode: 'BLimKas', 'WOmzNeoBin'. B for balans, W for
    -- winst-en-verliesrekening, and each level appends a group.
    code            text not null,

    description_nl  text not null,
    description_en  text,

    -- RGS's own D/C indicator, kept because XAF (CMP-002) carries it.
    debit_credit    text check (debit_credit in ('D', 'C')),

    level           smallint not null check (level between 1 and 6),
    parent_code     text,

    -- Only leaves take postings. A heading exists to hold a subtree, and an
    -- account mapped to one puts an amount where the taxonomy expects a sum.
    is_postable     boolean not null default false,

    -- FR-GL-005's classification. A MAPPING decision from RGS's structure to
    -- the five types the PRD names, and therefore data rather than a rule in
    -- code. Required for a postable element, since that is exactly where an
    -- account's own type has to agree with it.
    account_type    text check (
        account_type in ('asset', 'liability', 'equity', 'revenue', 'expense')
    ),

    sort_order      integer not null default 0,

    constraint rgs_element_code_unique unique (rgs_version_id, code),
    constraint rgs_element_postable_is_typed check (
        not is_postable or account_type is not null
    )
);

create index rgs_element_version_idx on rgs_element(rgs_version_id);
create index rgs_element_parent_idx on rgs_element(rgs_version_id, parent_code);
create index rgs_element_postable_idx
    on rgs_element(rgs_version_id, account_type)
    where is_postable;

-- rgs_element_code_unique above is also the index ledger_account's composite
-- foreign key resolves against - an account carries (version, code), never a
-- code alone - so there is deliberately no second index here.

-- ---------------------------------------------------------------------------
-- rgs_profile_account - the MKB profile, per legal form (FR-ONB-005)
-- ---------------------------------------------------------------------------
-- One row per account the seeded chart will contain. The profile is not a list
-- of RGS codes: it is a list of ACCOUNTS, each carrying the grootboeknummer,
-- the name in both languages, the VAT default and whether it is a control
-- account. All of that is the seed's content, and all of it is data.
create table rgs_profile_account (
    id                  uuid primary key default gen_random_uuid(),
    rgs_version_id      uuid not null references rgs_version(id),

    -- RGS publishes several profiles; FR-ONB-005 names MKB. Kept as a column
    -- so ZZP or an extended profile is another value, not another table.
    profile_code        text not null default 'mkb',
    legal_form          text not null check (
        legal_form in ('eenmanszaak', 'vof', 'bv', 'stichting', 'vereniging')
    ),

    -- Dutch practice: 0 fixed assets and equity, 1 current assets and
    -- liabilities, 2 suspense, 4 operating expenses, 7 cost of sales,
    -- 8 revenue. A convention, not RGS - RGS is rgs_code beside it.
    account_code        text not null,
    rgs_code            text not null,

    name_nl             text not null,
    name_en             text,

    default_vat_code    text,
    control_kind        text check (
        control_kind in ('accounts_receivable', 'accounts_payable')
    ),
    sort_order          integer not null default 0,

    constraint rgs_profile_account_unique
        unique (rgs_version_id, profile_code, legal_form, account_code),

    -- The profile cannot name a code the version does not define.
    constraint rgs_profile_account_element_fk
        foreign key (rgs_version_id, rgs_code)
        references rgs_element (rgs_version_id, code)
);

create index rgs_profile_account_lookup_idx
    on rgs_profile_account(rgs_version_id, profile_code, legal_form, sort_order);

-- At most one control account of each kind per profile, mirroring
-- ledger_account_one_control_per_kind_idx. A profile that seeded two AR
-- control accounts would make FR-GL-006 ambiguous from the first minute of the
-- administration's life.
create unique index rgs_profile_one_control_per_kind_idx
    on rgs_profile_account(rgs_version_id, profile_code, legal_form, control_kind)
    where control_kind is not null;

-- ---------------------------------------------------------------------------
-- rgs_code_mapping - CMP-003's upgrade path, as data
-- ---------------------------------------------------------------------------
-- What became of each code when a version superseded another. This is the
-- whole of the upgrade knowledge: ledger.plan_rgs_upgrade() reads these rows
-- and nothing else, so supporting a future RGS release needs no code.
create table rgs_code_mapping (
    id              uuid primary key default gen_random_uuid(),
    from_version_id uuid not null references rgs_version(id),
    to_version_id   uuid not null references rgs_version(id),

    from_code       text not null,
    -- NULL for 'withdrawn': the code is gone and its accounts need a human.
    to_code         text,

    change_kind     text not null check (
        change_kind in ('unchanged', 'renamed', 'split', 'merged', 'withdrawn')
    ),
    note            text,

    constraint rgs_code_mapping_versions_differ check (from_version_id <> to_version_id),
    constraint rgs_code_mapping_withdrawn_has_no_target check (
        (change_kind = 'withdrawn') = (to_code is null)
    )
);

create index rgs_code_mapping_lookup_idx
    on rgs_code_mapping(from_version_id, to_version_id, from_code);

-- 'split' is the one kind that legitimately has several rows for one from_code
-- - one code becoming three is a decision only a human can make. Every other
-- kind must be unambiguous, or an upgrade would pick a target arbitrarily.
create unique index rgs_code_mapping_unambiguous_idx
    on rgs_code_mapping(from_version_id, to_version_id, from_code)
    where change_kind <> 'split';

-- ---------------------------------------------------------------------------
-- administration_rgs_version - the pin
-- ---------------------------------------------------------------------------
-- Which RGS version this administration's chart is expressed in. Tenant data,
-- so it carries organization_id and is RLS-protected like everything else
-- (CLAUDE.md rule 1).
--
-- One row per administration, replaced on upgrade. The previous version and
-- the timestamp are kept on the row: "what did we upgrade from, and when" is
-- the first question after an upgrade goes wrong, and the audit log answers it
-- too but is a hash chain rather than something to query.
create table administration_rgs_version (
    administration_id       uuid primary key references administration(id),
    organization_id         uuid not null references organization(id),
    rgs_version_id          uuid not null references rgs_version(id),
    profile_code            text not null default 'mkb',
    legal_form              text not null,

    previous_rgs_version_id uuid references rgs_version(id),
    pinned_at               timestamptz not null default now(),
    pinned_by_user_id       uuid references users(id)
);

create index administration_rgs_version_organization_idx
    on administration_rgs_version(organization_id);
create index administration_rgs_version_version_idx
    on administration_rgs_version(rgs_version_id);

-- ===========================================================================
-- ledger_account gains its referent
-- ===========================================================================
-- rgs_code stays where 0020 put it. What is added is the version it belongs
-- to, and a composite foreign key that makes the pair mean something.
alter table ledger_account
    add column rgs_version_id uuid references rgs_version(id);

alter table ledger_account
    add constraint ledger_account_rgs_element_fk
    foreign key (rgs_version_id, rgs_code)
    references rgs_element (rgs_version_id, code);

-- Both or neither. Half a reference - a code with no version - is exactly the
-- state 0020 had for every account, and the state this migration exists to
-- end.
alter table ledger_account
    add constraint ledger_account_rgs_pair_complete
    check ((rgs_version_id is null) = (rgs_code is null));

create index ledger_account_rgs_idx
    on ledger_account(rgs_version_id, rgs_code)
    where rgs_code is not null;

-- FR-GL-005's "VAT default", constrained to FR-AR-002's list. It was free text
-- and a seed writing 'btw_21' next to a hand-entered '21%' would give the VAT
-- return two vocabularies to reconcile. The full VAT code table with rates and
-- rubriek mapping is FR-VAT's; this is the closed set of TREATMENTS the PRD
-- names, which is what an account can default to.
alter table ledger_account
    add constraint ledger_account_vat_default_known
    check (default_vat_code is null or default_vat_code in (
        'btw_21', 'btw_9', 'btw_0', 'btw_vrijgesteld',
        'btw_verlegd', 'btw_icp', 'btw_export', 'btw_marge'
    ));

alter table rgs_profile_account
    add constraint rgs_profile_account_vat_default_known
    check (default_vat_code is null or default_vat_code in (
        'btw_21', 'btw_9', 'btw_0', 'btw_vrijgesteld',
        'btw_verlegd', 'btw_icp', 'btw_export', 'btw_marge'
    ));

-- ---------------------------------------------------------------------------
-- The mapping-integrity trigger (FR-ONB-005)
-- ---------------------------------------------------------------------------
-- "May extend but not break RGS mapping integrity", as four checks that bind
-- every writer including the ledger's own definer functions.
create or replace function ledger_account_rgs_integrity() returns trigger as $$
declare
    v_pin     administration_rgs_version%rowtype;
    v_element rgs_element%rowtype;
begin
    if new.rgs_code is null then
        -- An account with no RGS code is legal (0020 made the column nullable
        -- deliberately). It must not carry half a reference, and it must not
        -- keep a stale version after being unmapped.
        new.rgs_version_id := null;
        return new;
    end if;

    select * into v_pin
      from administration_rgs_version
     where administration_id = new.administration_id;
    if not found then
        raise exception
            'administration % has no RGS version; seed the chart of accounts '
            'before mapping an account to RGS code % (FR-ONB-005)',
            new.administration_id, new.rgs_code;
    end if;

    -- Derive-don't-trust, the same rule journal_entry_validate() applies to
    -- entry numbers. A caller that could name its own version could map an
    -- account to a code withdrawn two releases ago and still satisfy the
    -- foreign key.
    new.rgs_version_id := v_pin.rgs_version_id;

    select * into v_element
      from rgs_element
     where rgs_version_id = new.rgs_version_id
       and code = new.rgs_code;
    if not found then
        raise exception
            'RGS code % does not exist in the version this administration uses '
            '(FR-ONB-005)', new.rgs_code;
    end if;

    if not v_element.is_postable then
        raise exception
            'RGS code % (%) is a heading, not a postable element; an account '
            'mapped to it would put an amount where the taxonomy expects the '
            'sum of its children (FR-ONB-005)',
            v_element.code, v_element.description_nl;
    end if;

    -- The check that matters most in practice. An expense account mapped to a
    -- revenue element balances, reports, and files wrongly.
    if v_element.account_type is distinct from new.account_type then
        raise exception
            'account % is % but RGS code % is %; a chart may be extended, not '
            'remapped across types (FR-ONB-005)',
            new.code, new.account_type, v_element.code, v_element.account_type;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger ledger_account_rgs_integrity_trg
    before insert or update on ledger_account
    for each row execute function ledger_account_rgs_integrity();

-- ---------------------------------------------------------------------------
-- Reference data is append-only
-- ---------------------------------------------------------------------------
-- Unconditional, with no branch and no exemption, for the reason 0019 and 0020
-- both give: a function whose only statement is RAISE cannot be talked into
-- permitting anything. These fire for ledgr_app, for ledgr_ops, for the
-- loader's own definer function, and for a superuser at a psql prompt alike.
create or replace function rgs_reference_immutable() returns trigger as $$
begin
    raise exception
        'RGS reference data is append-only (CMP-003): % cannot be % once '
        'loaded. A published version is fixed; correct it by publishing '
        'another version.',
        tg_table_name, lower(tg_op);
end;
$$ language plpgsql;

create trigger rgs_element_no_update_trg
    before update on rgs_element
    for each row execute function rgs_reference_immutable();
create trigger rgs_element_no_delete_trg
    before delete on rgs_element
    for each row execute function rgs_reference_immutable();
create trigger rgs_element_no_truncate_trg
    before truncate on rgs_element
    for each statement execute function rgs_reference_immutable();

create trigger rgs_profile_account_no_update_trg
    before update on rgs_profile_account
    for each row execute function rgs_reference_immutable();
create trigger rgs_profile_account_no_delete_trg
    before delete on rgs_profile_account
    for each row execute function rgs_reference_immutable();
create trigger rgs_profile_account_no_truncate_trg
    before truncate on rgs_profile_account
    for each statement execute function rgs_reference_immutable();

create trigger rgs_code_mapping_no_update_trg
    before update on rgs_code_mapping
    for each row execute function rgs_reference_immutable();
create trigger rgs_code_mapping_no_delete_trg
    before delete on rgs_code_mapping
    for each row execute function rgs_reference_immutable();

create trigger rgs_version_no_delete_trg
    before delete on rgs_version
    for each row execute function rgs_reference_immutable();
create trigger rgs_version_no_truncate_trg
    before truncate on rgs_version
    for each statement execute function rgs_reference_immutable();

-- rgs_version takes exactly one UPDATE: its lifecycle status. Same shape as
-- period_status_transition() in 0021 - which transitions exist at all, which
-- no privilege can widen.
create or replace function rgs_version_transition() returns trigger as $$
begin
    if new.version is distinct from old.version
       or new.source_checksum is distinct from old.source_checksum
       or new.source is distinct from old.source
       or new.supersedes_id is distinct from old.supersedes_id
       or new.published_at is distinct from old.published_at
       or new.effective_from is distinct from old.effective_from then
        raise exception
            'only an RGS version''s status may change; its identity and '
            'provenance are fixed at load (CMP-003)';
    end if;

    if new.status = old.status then
        return new;
    end if;

    -- A superseded version stays superseded. Accounts and postings from the
    -- periods it covered still reference it (CMP-014), so reviving it would
    -- make two versions current for the same administration's history.
    if not (
        (old.status = 'draft'   and new.status in ('current', 'superseded')) or
        (old.status = 'current' and new.status = 'superseded')
    ) then
        raise exception 'unsupported RGS version transition % -> % (CMP-003)',
            old.status, new.status;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger rgs_version_transition_trg
    before update on rgs_version
    for each row execute function rgs_version_transition();

-- ===========================================================================
-- Loading a version (CMP-003, and the whole point of "as data")
-- ===========================================================================
-- One jsonb document in, one version out. Atomic, idempotent on the checksum,
-- and refusing to overwrite: a version already loaded from a DIFFERENT
-- document is a published release someone is trying to change under the same
-- name, which is the failure this whole design exists to prevent.
--
-- The document's shape is apps/api/data/rgs/rgs-3.8-mkb.json. `common`
-- accounts are merged into every profile here rather than in the file, so the
-- file shows what DIFFERS between legal forms, which is what FR-ONB-004 is
-- about.
create or replace function ledger.load_rgs_version(
    p_document           jsonb,
    p_source_checksum    text,
    p_allow_provisional  boolean default false
)
returns rgs_version
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_version    rgs_version%rowtype;
    v_supersedes rgs_version%rowtype;
    v_source     text := p_document->>'source';
    v_name       text := p_document->>'rgs_version';
    v_profile    jsonb;
    v_account    jsonb;
    v_legal_form text;
begin
    if v_name is null or btrim(v_name) = '' then
        raise exception 'the document names no rgs_version';
    end if;
    if v_source is null then
        raise exception 'the document declares no source; provenance is not optional';
    end if;

    -- The gate on the provisional dataset. It exists so that shipping the
    -- starter file cannot quietly become shipping a filing basis: CMP-002's
    -- XAF export and CMP-004's SBR filing both carry these codes to a reader
    -- outside this system.
    if v_source = 'provisional-subset' and not p_allow_provisional then
        raise exception
            'RGS document % is a provisional subset, not the official '
            'publication. Load it only with p_allow_provisional => true, and '
            'never as the basis for an XAF export (CMP-002) or an SBR filing '
            '(CMP-004).', v_name;
    end if;

    select * into v_version from rgs_version where version = v_name;
    if found then
        if v_version.source_checksum = p_source_checksum then
            return v_version;   -- idempotent: the same file, already loaded
        end if;
        raise exception
            'RGS version % is already loaded from a different document '
            '(checksum % on file, % offered). A published version is frozen; '
            'publish a new version instead (CMP-003).',
            v_name, v_version.source_checksum, p_source_checksum;
    end if;

    if p_document->>'supersedes' is not null then
        select * into v_supersedes
          from rgs_version where version = p_document->>'supersedes';
        if not found then
            raise exception
                'document % supersedes version %, which is not loaded',
                v_name, p_document->>'supersedes';
        end if;
    end if;

    insert into rgs_version (
        version, status, published_at, effective_from,
        source, source_note, source_checksum, supersedes_id
    ) values (
        v_name,
        'draft',
        (p_document->>'published_at')::date,
        (p_document->>'effective_from')::date,
        v_source,
        p_document->>'source_note',
        p_source_checksum,
        v_supersedes.id
    )
    returning * into v_version;

    insert into rgs_element (
        rgs_version_id, code, description_nl, description_en, debit_credit,
        level, parent_code, is_postable, account_type, sort_order
    )
    select v_version.id,
           e->>'code',
           e->>'description_nl',
           e->>'description_en',
           e->>'debit_credit',
           (e->>'level')::smallint,
           e->>'parent_code',
           coalesce((e->>'postable')::boolean, false),
           e->>'account_type',
           coalesce((e->>'sort_order')::integer, 0)
      from jsonb_array_elements(coalesce(p_document->'elements', '[]'::jsonb)) as e;

    -- Every parent_code must resolve inside the same version. Checked here
    -- rather than as a self-referencing foreign key because the rows arrive in
    -- document order, and a parent may legitimately follow its child in the
    -- file.
    if exists (
        select 1
          from rgs_element c
         where c.rgs_version_id = v_version.id
           and c.parent_code is not null
           and not exists (
               select 1 from rgs_element p
                where p.rgs_version_id = v_version.id and p.code = c.parent_code
           )
    ) then
        raise exception
            'the document has elements whose parent_code names no element in '
            'the same version';
    end if;

    for v_profile in
        select * from jsonb_array_elements(coalesce(p_document->'profiles', '[]'::jsonb))
    loop
        v_legal_form := v_profile->>'legal_form';

        for v_account in
            select *
              from jsonb_array_elements(
                       coalesce(p_document->'common', '[]'::jsonb)
                       || coalesce(v_profile->'accounts', '[]'::jsonb)
                   )
        loop
            insert into rgs_profile_account (
                rgs_version_id, profile_code, legal_form, account_code,
                rgs_code, name_nl, name_en, default_vat_code, control_kind,
                sort_order
            ) values (
                v_version.id,
                coalesce(p_document->>'profile_code', 'mkb'),
                v_legal_form,
                v_account->>'account_code',
                v_account->>'rgs_code',
                v_account->>'name_nl',
                v_account->>'name_en',
                v_account->>'default_vat_code',
                v_account->>'control_kind',
                coalesce((v_account->>'sort_order')::integer, 0)
            );
        end loop;
    end loop;

    insert into rgs_code_mapping (
        from_version_id, to_version_id, from_code, to_code, change_kind, note
    )
    select v_supersedes.id, v_version.id,
           m->>'from_code', m->>'to_code', m->>'change_kind', m->>'note'
      from jsonb_array_elements(coalesce(p_document->'mappings', '[]'::jsonb)) as m;

    return v_version;
end;
$$;

-- Publishing: draft -> current, and whatever was current becomes superseded.
-- Two statements that must not be separable, which is why it is a function
-- rather than two calls: a moment with no current version would make
-- ledger.seed_chart_of_accounts() fail for every new administration.
create or replace function ledger.publish_rgs_version(p_version_id uuid)
returns rgs_version
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_version rgs_version%rowtype;
begin
    select * into v_version from rgs_version where id = p_version_id;
    if not found then
        raise exception 'RGS version % does not exist', p_version_id;
    end if;
    if v_version.status = 'current' then
        return v_version;
    end if;

    update rgs_version set status = 'superseded'
     where status = 'current' and id <> p_version_id;

    update rgs_version set status = 'current'
     where id = p_version_id
    returning * into v_version;

    return v_version;
end;
$$;

-- ===========================================================================
-- Seeding the chart (FR-ONB-005)
-- ===========================================================================
-- Idempotent by account code: re-running adds what is missing and touches
-- nothing that exists. That matters because FR-ONB-010 makes onboarding
-- resumable, so this WILL be called twice, and because an administration that
-- has already posted must not have its chart rebuilt underneath it.
create or replace function ledger.seed_chart_of_accounts(
    p_administration_id uuid,
    p_rgs_version_id    uuid default null,
    p_actor_user_id     uuid default null,
    p_profile_code      text default 'mkb'
)
-- The output columns are named *_name / *_code rather than `rgs_version` and
-- `legal_form`. In plpgsql a `returns table` column is a variable in scope for
-- the whole body, and one called `rgs_version` sitting over a table of the
-- same name is an ambiguity waiting for whoever edits this next.
returns table (
    seeded_count     integer,
    skipped_count    integer,
    rgs_version_name text,
    legal_form_code  text
)
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_admin      administration%rowtype;
    v_version    rgs_version%rowtype;
    v_legal_form text;
    v_pin        administration_rgs_version%rowtype;
    v_seeded     integer := 0;
    v_skipped    integer := 0;
    v_row        rgs_profile_account%rowtype;
begin
    select * into v_admin from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    v_legal_form := app.canonical_legal_form(v_admin.legal_form);
    if v_legal_form is null then
        raise exception
            -- RAISE understands % and nothing else; %L is format()'s, and
            -- writing it here would print the value followed by a stray "L".
            'legal form "%" has no RGS profile; FR-ONB-004 recognises '
            'eenmanszaak, VOF, BV, stichting and vereniging, and '
            'legal_form_alias maps captured spellings onto those',
            v_admin.legal_form;
    end if;

    if p_rgs_version_id is null then
        select * into v_version from rgs_version where status = 'current';
        if not found then
            raise exception
                'no current RGS version is loaded; load one with '
                'ledger.load_rgs_version() before seeding a chart (CMP-003)';
        end if;
    else
        select * into v_version from rgs_version where id = p_rgs_version_id;
        if not found then
            raise exception 'RGS version % does not exist', p_rgs_version_id;
        end if;
        if v_version.status = 'superseded' then
            raise exception
                'RGS version % is superseded; a new chart is seeded from the '
                'current version (CMP-003)', v_version.version;
        end if;
    end if;

    if not exists (
        select 1 from rgs_profile_account
         where rgs_version_id = v_version.id
           and profile_code = p_profile_code
           and legal_form = v_legal_form
    ) then
        raise exception
            'RGS version % has no % profile for legal form %',
            v_version.version, p_profile_code, v_legal_form;
    end if;

    -- The pin comes FIRST. ledger_account_rgs_integrity() derives an account's
    -- version from it, so seeding into an unpinned administration would fail
    -- on the first row.
    select * into v_pin
      from administration_rgs_version where administration_id = p_administration_id;
    if not found then
        insert into administration_rgs_version (
            administration_id, organization_id, rgs_version_id, profile_code,
            legal_form, pinned_by_user_id
        ) values (
            p_administration_id, v_admin.organization_id, v_version.id,
            p_profile_code, v_legal_form, p_actor_user_id
        );
    elsif v_pin.rgs_version_id <> v_version.id then
        raise exception
            'administration % is pinned to RGS version %; changing it is an '
            'upgrade, not a re-seed - see ledger.apply_rgs_upgrade (CMP-003)',
            p_administration_id,
            (select version from rgs_version where id = v_pin.rgs_version_id);
    end if;

    for v_row in
        select *
          from rgs_profile_account
         where rgs_version_id = v_version.id
           and profile_code = p_profile_code
           and legal_form = v_legal_form
         order by sort_order, account_code
    loop
        -- Idempotence is by ACCOUNT CODE, which is also the unique constraint
        -- ledger_account_code_unique enforces. An account the user created at
        -- the same code is left exactly as it is: the seed adds, it never
        -- overwrites a decision somebody already made.
        if exists (
            select 1 from ledger_account
             where administration_id = p_administration_id and code = v_row.account_code
        ) then
            v_skipped := v_skipped + 1;
            continue;
        end if;

        insert into ledger_account (
            organization_id, administration_id, code, name, account_type,
            rgs_code, default_vat_code, control_kind
        )
        select v_admin.organization_id,
               p_administration_id,
               v_row.account_code,
               v_row.name_nl,
               e.account_type,
               v_row.rgs_code,
               v_row.default_vat_code,
               v_row.control_kind
          from rgs_element e
         where e.rgs_version_id = v_version.id and e.code = v_row.rgs_code;

        v_seeded := v_seeded + 1;
    end loop;

    return query select v_seeded, v_skipped, v_version.version, v_legal_form;
end;
$$;

-- ---------------------------------------------------------------------------
-- Correcting one account's mapping (FR-ONB-005's "may extend")
-- ---------------------------------------------------------------------------
-- `ledgr_app` holds no UPDATE on ledger_account, so without this there is no
-- way for a user to fix a mapping at all - and "may extend but not break
-- integrity" has to leave room for the first half.
--
-- What it does NOT do is relax anything. ledger_account_rgs_integrity() runs
-- on the UPDATE exactly as it does on the INSERT, so the new code must exist
-- in the pinned version, be postable, and match the account's type. Passing
-- NULL unmaps the account, which stays legal and is reported by
-- ledger.rgs_readiness() as an unmapped account rather than silently.
create or replace function ledger.set_account_rgs_code(
    p_account_id uuid,
    p_rgs_code   text
)
returns ledger_account
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_account ledger_account%rowtype;
begin
    update ledger_account set rgs_code = p_rgs_code
     where id = p_account_id
    returning * into v_account;
    if not found then
        raise exception 'account % does not exist', p_account_id;
    end if;
    return v_account;
end;
$$;

-- ===========================================================================
-- The upgrade path (CMP-003)
-- ===========================================================================
-- Plan first, apply second, and the plan is a query rather than a dry run:
-- every row says what would happen to one account and why. An administration
-- can be shown this before anything is written, which is what makes the
-- upgrade a decision rather than an event.
create or replace function ledger.plan_rgs_upgrade(
    p_administration_id uuid,
    p_to_version_id     uuid
)
returns table (
    account_id      uuid,
    account_code    text,
    account_name    text,
    account_type    text,
    from_rgs_code   text,
    to_rgs_code     text,
    change_kind     text,
    resolution      text,
    detail          text
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select a.id,
           a.code,
           a.name,
           a.account_type,
           a.rgs_code,
           m.to_code,
           coalesce(m.change_kind, case when a.rgs_code is null then 'unmapped'
                                        else 'no_mapping' end),
           case
               when a.rgs_code is null                 then 'nothing_to_do'
               when m.change_kind = 'split'            then 'needs_a_decision'
               when m.change_kind = 'withdrawn'        then 'needs_a_decision'
               when m.id is null                       then 'needs_a_decision'
               when t.code is null                     then 'needs_a_decision'
               when not t.is_postable                  then 'needs_a_decision'
               when t.account_type is distinct from a.account_type
                                                       then 'needs_a_decision'
               when m.change_kind = 'unchanged'        then 'automatic'
               else 'automatic'
           end,
           case
               when a.rgs_code is null then
                   'the account carries no RGS code and is unaffected'
               when m.id is null then
                   format('RGS code %s has no mapping into the new version', a.rgs_code)
               when m.change_kind = 'split' then
                   format('RGS code %s was split; which target applies is an '
                          'accounting decision', a.rgs_code)
               when m.change_kind = 'withdrawn' then
                   format('RGS code %s was withdrawn', a.rgs_code)
               when t.code is null then
                   format('the new version does not define %s', m.to_code)
               when not t.is_postable then
                   format('%s is a heading in the new version', m.to_code)
               when t.account_type is distinct from a.account_type then
                   format('%s is %s in the new version but the account is %s',
                          m.to_code, t.account_type, a.account_type)
               else
                   format('%s -> %s (%s)', a.rgs_code, m.to_code, m.change_kind)
           end
      from ledger_account a
      left join administration_rgs_version p
             on p.administration_id = a.administration_id
      left join rgs_code_mapping m
             on m.from_version_id = p.rgs_version_id
            and m.to_version_id = p_to_version_id
            and m.from_code = a.rgs_code
      left join rgs_element t
             on t.rgs_version_id = p_to_version_id
            and t.code = m.to_code
     where a.administration_id = p_administration_id
     order by a.code;
$$;

comment on function ledger.plan_rgs_upgrade(uuid, uuid) is
    'CMP-003. What an RGS upgrade would do to this chart, per account, before '
    'anything is written. resolution is automatic | needs_a_decision | '
    'nothing_to_do.';

-- Applies the plan. Refuses while anything needs a decision, rather than
-- guessing or skipping: a half-upgraded chart is a chart where some accounts
-- file under the new taxonomy and some under the old, and nothing on the
-- screen says which.
create or replace function ledger.apply_rgs_upgrade(
    p_administration_id uuid,
    p_to_version_id     uuid,
    p_actor_user_id     uuid default null
)
returns table (
    remapped_count   integer,
    unmapped_count   integer,
    rgs_version_name text
)
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_pin       administration_rgs_version%rowtype;
    v_to        rgs_version%rowtype;
    v_blocked   integer;
    v_remapped  integer := 0;
    v_unmapped  integer := 0;
    v_plan      record;
begin
    select * into v_pin
      from administration_rgs_version where administration_id = p_administration_id;
    if not found then
        raise exception
            'administration % has no RGS version to upgrade from; seed its '
            'chart first (FR-ONB-005)', p_administration_id;
    end if;

    select * into v_to from rgs_version where id = p_to_version_id;
    if not found then
        raise exception 'RGS version % does not exist', p_to_version_id;
    end if;
    if v_to.id = v_pin.rgs_version_id then
        raise exception 'administration % is already on RGS version %',
            p_administration_id, v_to.version;
    end if;
    if v_to.status = 'superseded' then
        raise exception
            'RGS version % is superseded; upgrading onto it would move the '
            'chart backwards (CMP-003)', v_to.version;
    end if;

    select count(*) into v_blocked
      from ledger.plan_rgs_upgrade(p_administration_id, p_to_version_id)
     where resolution = 'needs_a_decision';

    if v_blocked > 0 then
        raise exception
            '% account(s) need a decision before this upgrade can be applied; '
            'read ledger.plan_rgs_upgrade(''%'', ''%'') and remap them first '
            '(CMP-003)',
            v_blocked, p_administration_id, p_to_version_id;
    end if;

    -- The pin moves FIRST: ledger_account_rgs_integrity() derives each
    -- account's version from it, so remapping before the pin moved would
    -- validate every new code against the OLD version and fail.
    update administration_rgs_version
       set rgs_version_id = p_to_version_id,
           previous_rgs_version_id = v_pin.rgs_version_id,
           pinned_at = now(),
           pinned_by_user_id = coalesce(p_actor_user_id, pinned_by_user_id)
     where administration_id = p_administration_id;

    for v_plan in
        select a.id, m.to_code
          from ledger_account a
          join rgs_code_mapping m
            on m.from_version_id = v_pin.rgs_version_id
           and m.to_version_id = p_to_version_id
           and m.from_code = a.rgs_code
         where a.administration_id = p_administration_id
           and a.rgs_code is not null
    loop
        update ledger_account
           set rgs_code = v_plan.to_code
         where id = v_plan.id;
        v_remapped := v_remapped + 1;
    end loop;

    select count(*) into v_unmapped
      from ledger_account
     where administration_id = p_administration_id and rgs_code is null;

    return query select v_remapped, v_unmapped, v_to.version;
end;
$$;

-- ===========================================================================
-- Readiness (CMP-003, CMP-013)
-- ===========================================================================
-- Two questions an operator has to be able to answer without opening a
-- console: which administrations are on a dataset that was never verified
-- against the official publication, and which are behind the current version.
--
-- Deliberately NOT folded into the NFR-033 integrity job. That job verifies
-- three named things and returning a fourth kind of finding from it would make
-- its contract "whatever we thought of", which is how a check nobody reads
-- gets born. This is its own report, with its own audience.
create or replace function ledger.rgs_readiness(
    p_administration_id uuid default null
)
returns table (
    administration_id   uuid,
    legal_form_code     text,
    rgs_version_name    text,
    version_status      text,
    version_source      text,
    is_provisional      boolean,
    is_behind_current   boolean,
    accounts_total      bigint,
    accounts_unmapped   bigint
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select p.administration_id,
           p.legal_form,
           v.version,
           v.status,
           v.source,
           v.source = 'provisional-subset',
           v.status <> 'current',
           count(a.id),
           count(a.id) filter (where a.rgs_code is null)
      from administration_rgs_version p
      join rgs_version v on v.id = p.rgs_version_id
      left join ledger_account a on a.administration_id = p.administration_id
     where p_administration_id is null
        or p.administration_id = p_administration_id
     group by p.administration_id, p.legal_form, v.version, v.status, v.source;
$$;

-- Accounts whose stored version disagrees with their administration's pin.
-- Structurally impossible: the integrity trigger derives the version from the
-- pin on every write, and the upgrade moves both inside one transaction. Built
-- for the reason 0020 built ledger.numbering_gaps() - a report that can only
-- ever be empty is the check on the thing that makes it empty.
create or replace function ledger.rgs_mapping_deviations(
    p_administration_id uuid default null
)
returns table (
    account_id          uuid,
    administration_id   uuid,
    account_code        text,
    deviation           text,
    detail              text
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select a.id, a.administration_id, a.code,
           case
               when p.administration_id is null then 'chart_not_pinned'
               when a.rgs_version_id <> p.rgs_version_id then 'stale_rgs_version'
               when e.code is null then 'rgs_code_not_in_version'
               when not e.is_postable then 'rgs_code_not_postable'
               else 'rgs_type_mismatch'
           end,
           case
               when p.administration_id is null then
                   'the account carries an RGS code but its administration is '
                   'pinned to no version'
               when a.rgs_version_id <> p.rgs_version_id then
                   'the account was left on a version its administration has '
                   'upgraded away from'
               when e.code is null then
                   format('%s is not defined in the pinned version', a.rgs_code)
               when not e.is_postable then
                   format('%s is a heading, not a postable element', a.rgs_code)
               else
                   format('the account is %s but %s is %s',
                          a.account_type, a.rgs_code, e.account_type)
           end
      from ledger_account a
      left join administration_rgs_version p
             on p.administration_id = a.administration_id
      left join rgs_element e
             on e.rgs_version_id = a.rgs_version_id and e.code = a.rgs_code
     where a.rgs_code is not null
       and (p_administration_id is null or a.administration_id = p_administration_id)
       and (
            p.administration_id is null
         or a.rgs_version_id <> p.rgs_version_id
         or e.code is null
         or not e.is_postable
         or e.account_type is distinct from a.account_type
       );
$$;

-- ===========================================================================
-- Reading the chart and the reference data
-- ===========================================================================
create or replace function ledger.chart_of_accounts(
    p_administration_id uuid,
    p_include_blocked   boolean default true
)
returns table (
    account_id          uuid,
    code                text,
    name                text,
    account_type        text,
    status              text,
    rgs_code            text,
    rgs_description_nl  text,
    rgs_description_en  text,
    rgs_version_name    text,
    default_vat_code    text,
    control_kind        text,
    is_seeded           boolean
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select a.id, a.code, a.name, a.account_type, a.status,
           a.rgs_code, e.description_nl, e.description_en, v.version,
           a.default_vat_code, a.control_kind,
           -- FR-ONB-005: which accounts came from the profile and which the
           -- user added. Derived rather than stored, so it stays true when a
           -- later profile adds an account the user had already created.
           exists (
               select 1 from rgs_profile_account pa
                where pa.rgs_version_id = a.rgs_version_id
                  and pa.profile_code = p.profile_code
                  and pa.legal_form = p.legal_form
                  and pa.account_code = a.code
           )
      from ledger_account a
      left join administration_rgs_version p on p.administration_id = a.administration_id
      left join rgs_element e
             on e.rgs_version_id = a.rgs_version_id and e.code = a.rgs_code
      left join rgs_version v on v.id = a.rgs_version_id
     where a.administration_id = p_administration_id
       and (p_include_blocked or a.status = 'active')
     order by a.code;
$$;

-- The elements an account of a given type may legally be mapped to. This is
-- what a "change the RGS code" picker is populated from, and populating it
-- from the same rule the trigger enforces is what stops the UI offering a
-- choice the database will refuse (PRD D5: no dead ends).
create or replace function ledger.rgs_options(
    p_administration_id uuid,
    p_account_type      text
)
returns table (
    code            text,
    description_nl  text,
    description_en  text,
    level           smallint,
    parent_code     text
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select e.code, e.description_nl, e.description_en, e.level, e.parent_code
      from administration_rgs_version p
      join rgs_element e on e.rgs_version_id = p.rgs_version_id
     where p.administration_id = p_administration_id
       and e.is_postable
       and e.account_type = p_account_type
     order by e.sort_order, e.code;
$$;

-- ===========================================================================
-- Ownership and privileges
-- ===========================================================================
-- The reference tables are owned by ledgr_ledger for the same reason the
-- posting tables are: ledgr_app holds SELECT and nothing else, so application
-- code cannot invent an RGS code and then map an account to it. That would
-- satisfy every foreign key and break FR-ONB-005 completely.
alter table rgs_version                owner to ledgr_ledger;
alter table rgs_element                owner to ledgr_ledger;
alter table rgs_profile_account        owner to ledgr_ledger;
alter table rgs_code_mapping           owner to ledgr_ledger;
alter table administration_rgs_version owner to ledgr_ledger;

-- legal_form_alias goes to ledgr_migrator rather than ledgr_ledger: it is
-- LEDGR's own vocabulary for reading the KvK's, not part of the ledger's
-- integrity story, and a new alias is an ordinary migration. Reassigned
-- explicitly, like every other non-ledger table in this schema - a table left
-- with the superuser that ran the migration is one no migration role can
-- later alter.
alter table legal_form_alias owner to ledgr_migrator;

alter function ledger_account_rgs_integrity() owner to ledgr_ledger;
alter function rgs_reference_immutable()      owner to ledgr_ledger;
alter function rgs_version_transition()       owner to ledgr_ledger;

alter function ledger.load_rgs_version(jsonb, text, boolean)      owner to ledgr_ledger;
alter function ledger.publish_rgs_version(uuid)                   owner to ledgr_ledger;
alter function ledger.seed_chart_of_accounts(uuid, uuid, uuid, text) owner to ledgr_ledger;
alter function ledger.set_account_rgs_code(uuid, text)            owner to ledgr_ledger;
alter function ledger.plan_rgs_upgrade(uuid, uuid)                owner to ledgr_ledger;
alter function ledger.apply_rgs_upgrade(uuid, uuid, uuid)         owner to ledgr_ledger;
alter function ledger.rgs_readiness(uuid)                         owner to ledgr_ledger;
alter function ledger.rgs_mapping_deviations(uuid)                owner to ledgr_ledger;
alter function ledger.chart_of_accounts(uuid, boolean)            owner to ledgr_ledger;
alter function ledger.rgs_options(uuid, text)                     owner to ledgr_ledger;

revoke all on rgs_version, rgs_element, rgs_profile_account, rgs_code_mapping,
              administration_rgs_version from public;

-- Belt and braces on top of rgs_reference_immutable(), the same shape 0020
-- uses: Postgres does not privilege-check a table's owner, so this is not what
-- stops ledgr_ledger - the triggers are. It stops the privilege being
-- inherited or granted onward by accident.
revoke update, delete, truncate on rgs_element, rgs_profile_account,
                                   rgs_code_mapping from ledgr_ledger;
revoke delete, truncate on rgs_version from ledgr_ledger;

grant select on rgs_version, rgs_element, rgs_profile_account, rgs_code_mapping,
                administration_rgs_version, legal_form_alias
    to ledgr_app, ledgr_ops;

grant select on legal_form_alias to ledgr_ledger;
grant execute on function app.canonical_legal_form(text)
    to ledgr_app, ledgr_ops, ledgr_ledger;

revoke all on all functions in schema ledger from public;

-- ---------------------------------------------------------------------------
-- Who may call what
-- ---------------------------------------------------------------------------
-- load_rgs_version and publish_rgs_version go to ledgr_ops and NOT to
-- ledgr_app. Loading a new RGS release is an operator action on a schedule
-- CMP-013 sets - it is not something a request should be able to do, and a
-- tenant certainly should not be able to introduce reference data every other
-- tenant then maps accounts to.
--
-- This is the one write privilege ledgr_ops holds anywhere near the ledger,
-- and it is bounded by the append-only triggers: ops can add a version, and
-- cannot alter one, cannot touch an administration's chart, and cannot post
-- (CMP-009).
grant execute on function ledger.load_rgs_version(jsonb, text, boolean) to ledgr_ops;
grant execute on function ledger.publish_rgs_version(uuid) to ledgr_ops;

-- The tenant-facing surface. Authorization is the shared library's
-- (CLAUDE.md rule 3) - these grants are what makes the call possible at all,
-- not what makes it permitted.
grant execute on function ledger.seed_chart_of_accounts(uuid, uuid, uuid, text)
    to ledgr_app;
grant execute on function ledger.set_account_rgs_code(uuid, text) to ledgr_app;
grant execute on function ledger.apply_rgs_upgrade(uuid, uuid, uuid) to ledgr_app;

grant execute on function ledger.plan_rgs_upgrade(uuid, uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_readiness(uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_mapping_deviations(uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.chart_of_accounts(uuid, boolean) to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_options(uuid, text) to ledgr_app, ledgr_ops;

-- 0020's, 0021's and 0023's grants are re-issued, following the convention
-- 0021 set: the revoke above strips PUBLIC, and this block is what makes the
-- file's grant list the complete picture of who may call what in the `ledger`
-- schema rather than a delta composed from four files.
grant execute on function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text, uuid
) to ledgr_app;
grant execute on function ledger.reverse_entry(
    uuid, uuid, date, text, uuid, text, text
) to ledgr_app;
grant execute on function ledger.create_account(
    uuid, text, text, text, text, text, text
) to ledgr_app;
grant execute on function ledger.set_account_status(uuid, text) to ledgr_app;
grant execute on function ledger.create_journal(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.set_journal_status(uuid, text) to ledgr_app;
grant execute on function ledger.create_party(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.lock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.unlock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.mark_period_filed(uuid, uuid, text) to ledgr_app;
grant execute on function ledger.open_suppletie(uuid, text, uuid) to ledgr_app;
grant execute on function ledger.close_suppletie(uuid, text, text) to ledgr_app;

grant execute on function ledger.suppletie_corrections(uuid)          to ledgr_app, ledgr_ops;
grant execute on function ledger.numbering_gaps(uuid, uuid)           to ledgr_app, ledgr_ops;
grant execute on function ledger.trial_balance(uuid, uuid)            to ledgr_app, ledgr_ops;
grant execute on function ledger.subledger_balance(uuid, text)        to ledgr_app, ledgr_ops;
grant execute on function ledger.control_account_reconciliation(uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.money_text(numeric)                  to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_balance(uuid)              to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_control_accounts(uuid)     to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_numbering(uuid)            to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_scope(uuid)                to ledgr_app, ledgr_ops;
grant execute on function ledger.integrity_findings(uuid)             to ledgr_app, ledgr_ops;

-- The definer functions above read and write tenancy-scoped tables as
-- ledgr_ledger, which needs the privileges to do it.
grant select, insert, update on administration_rgs_version to ledgr_ledger;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
-- The reference tables carry NO tenant column and NO policy, deliberately.
-- RGS is a public taxonomy: every tenant reads the same rows, and a policy
-- keyed on app.current_org_id() would make the chart of accounts invisible to
-- the tenant that owns it. What protects them is that nobody but ledgr_ops,
-- through one function, can write them.
--
-- administration_rgs_version is tenant data and is treated as such.
alter table administration_rgs_version enable row level security;
alter table administration_rgs_version force row level security;

create policy administration_rgs_version_select on administration_rgs_version
    for select using (app.has_administration_access(administration_id));
create policy administration_rgs_version_insert on administration_rgs_version
    for insert with check (app.has_administration_access(administration_id));
create policy administration_rgs_version_update on administration_rgs_version
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- No DELETE policy and no DELETE grant. Unpinning an administration would
-- orphan every RGS code on its chart, and there is no reason to: an
-- administration moves between versions through the upgrade path.

commit;

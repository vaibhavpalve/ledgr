-- 0028_effective_dated_tax_rules.sql
-- CMP-014 (PRD §11), with CMP-013's watch list as the scope.
-- See docs/decisions/ADR-027-effective-dated-tax-rules.md.
--
--   CMP-014  Rate and rule changes are effective-dated, so historical periods
--            keep the rules that applied at the time.
--   CMP-013  A regulatory watch function tracks changes to VAT rates,
--            rubrieken, taxonomy versions, RGS versions and e-invoicing
--            mandates, with a defined lead time for product changes.
--
-- Three kinds of rule, one mechanism: VAT rates, rubriek definitions, and RGS
-- versions (0024 gave rgs_version an effective_from column and nothing that
-- read it; this adds the resolution).
--
-- ===========================================================================
-- valid_from, and deliberately no valid_to
-- ===========================================================================
--
-- The rule in force on a date is the row with the greatest valid_from on or
-- before it. Superseding a rule is an INSERT and nothing else.
--
-- The alternative - a closed [valid_from, valid_to) range - requires writing an
-- end date onto an existing row when its successor arrives. That makes the
-- normal, expected, monthly act of publishing a rate change into an UPDATE of
-- a historical row, and a table you routinely edit is one where history can be
-- rewritten by accident. With valid_from alone these tables are append-only in
-- the same sense the ledger is, and the same triggers enforce it.
--
-- The cost is real and worth naming: a gap is not representable. There is no
-- way to say "no rate applied between these dates". For VAT rates that is
-- correct - some rate always applies - and where it would not be, the answer
-- is a rule row that says so explicitly rather than an absence.
--
-- ===========================================================================
-- "Retroactively changing a rate must not alter a filed period"
-- ===========================================================================
--
-- Effective dating alone does not deliver this. A row inserted TODAY with
-- valid_from = 2019-01-01 changes what applied in 2019, and every return filed
-- since would silently restate. So the requirement needs an actual barrier,
-- and it is enforced in two independent ways:
--
--   PREVENTED  vat_rule_not_behind_a_filing() refuses any rule row whose
--              valid_from falls on or before the last day any period has been
--              VAT-filed through. Rules may only be introduced ahead of the
--              filed frontier.
--
--   DETECTED   a period records vat_rules_fingerprint when it is filed - a
--              hash of every rule in force on its end date. vat.filed_period_
--              drift() recomputes it and reports any period whose rules have
--              moved underneath it. It should always be empty, and it is the
--              check ON the barrier, in the same spirit as 0020's gap report
--              and 0023's integrity job.
--
-- Prevention is the guarantee; detection is what notices if a future migration
-- drops the trigger, or a superuser writes around it.
--
-- ===========================================================================
-- The frontier is a watermark, not a scan of `period`
-- ===========================================================================
--
-- The guard has to know the latest date covered by ANY filed period, across
-- every tenant, because a VAT rate is national and one row affects all of
-- them. `period` is RLS-protected, so a trigger reading it would see only the
-- caller's tenant and would happily let a rate be backdated over somebody
-- else's filed return.
--
-- vat_filing_watermark holds that one date, advanced by ledger.mark_period_
-- filed as periods are filed. It carries no tenant column because it is not
-- tenant data: it is a single fact about the whole system, and the guard needs
-- it to be exactly that. vat.watermark_drift() recomputes it from `period` for
-- an operator who can see across tenants, so the derived value is checkable.

begin;

create schema if not exists vat;

comment on schema vat is
    'Effective-dated tax rules (CMP-014). Everything here is reference data: '
    'no tenant owns it, every tenant reads it, and nobody updates it.';

-- ===========================================================================
-- Provenance
-- ===========================================================================
-- Same shape as rgs_version in 0024, and for the same reason: a dataset that
-- was never checked against the authority it claims to represent has to be
-- reportable rather than indistinguishable from one that was.
create table vat_ruleset (
    id              uuid primary key default gen_random_uuid(),
    jurisdiction    text not null,
    version         text not null,
    source          text not null check (
        source in ('official-publication', 'provisional-subset')
    ),
    source_note     text,
    source_checksum text not null,
    loaded_at       timestamptz not null default now(),

    constraint vat_ruleset_version_unique unique (jurisdiction, version)
);

-- ===========================================================================
-- Treatments (FR-AR-002)
-- ===========================================================================
-- The eight treatments themselves are not effective-dated: the SET is fixed by
-- FR-AR-002 and mirrored by a CHECK on ledger_account.default_vat_code in
-- 0024. What changes over time is a treatment's rate and its rubriek, and both
-- of those are separate tables below.
create table vat_treatment (
    code            text primary key,

    -- The stable semantics. `code` is an identifier and two of them - btw_21
    -- and btw_9 - have a rate baked into a name that has already outlived it:
    -- btw_9 was 6% until 2019. Anything reasoning about a treatment should
    -- read `role`. See ADR-027 for why the codes were not renamed here.
    role            text not null unique check (
        role in ('standard', 'reduced', 'zero', 'exempt',
                 'reverse_charge', 'intra_community', 'export', 'margin')
    ),
    description_nl  text not null,
    description_en  text,

    constraint vat_treatment_code_known check (code in (
        'btw_21', 'btw_9', 'btw_0', 'btw_vrijgesteld',
        'btw_verlegd', 'btw_icp', 'btw_export', 'btw_marge'
    ))
);

-- ===========================================================================
-- Rates (CMP-014's headline case)
-- ===========================================================================
create table vat_rate (
    treatment_code  text not null references vat_treatment(code),
    valid_from      date not null,

    -- A percentage: 21.000, not 0.21. numeric, never float - NFR-031 covers
    -- the whole calculation path and a rate is its first multiplier. Three
    -- decimals because a rate is not always an integer percentage.
    rate            numeric(6, 3) not null check (rate >= 0 and rate <= 100),

    note            text,
    ruleset_id      uuid not null references vat_ruleset(id),

    primary key (treatment_code, valid_from)
);

create index vat_rate_lookup_idx on vat_rate(treatment_code, valid_from desc);

-- ===========================================================================
-- Rubriek definitions (FR-VAT-001)
-- ===========================================================================
create table vat_rubriek (
    code            text not null,
    valid_from      date not null,

    kind            text not null check (
        kind in ('turnover', 'vat', 'input', 'subtotal')
    ),
    description_nl  text not null,
    description_en  text,
    ruleset_id      uuid not null references vat_ruleset(id),

    primary key (code, valid_from)
);

create index vat_rubriek_lookup_idx on vat_rubriek(code, valid_from desc);

-- Which box a treatment reports into, which is the part of FR-VAT-001 most
-- likely to change when the form does.
create table vat_treatment_rubriek (
    treatment_code   text not null references vat_treatment(code),
    valid_from       date not null,

    turnover_rubriek text not null,
    -- NULL where the treatment produces no output VAT for the seller, which is
    -- not the same as producing zero. A reverse-charged supply has no VAT to
    -- report; a 0% supply has an amount that happens to be zero.
    vat_rubriek      text,
    ruleset_id       uuid not null references vat_ruleset(id),

    primary key (treatment_code, valid_from)
);

create index vat_treatment_rubriek_lookup_idx
    on vat_treatment_rubriek(treatment_code, valid_from desc);

-- ===========================================================================
-- The filed frontier
-- ===========================================================================
create table vat_filing_watermark (
    jurisdiction  text primary key,
    -- The latest period end date that has been VAT-filed anywhere. Rules may
    -- only be introduced after this date.
    filed_through date not null,
    updated_at    timestamptz not null default now()
);

comment on table vat_filing_watermark is
    'CMP-014. The last day covered by any VAT-filed period, across all '
    'tenants. Advanced by ledger.mark_period_filed; never moves backwards.';

-- ===========================================================================
-- The barrier
-- ===========================================================================
create or replace function vat_rule_not_behind_a_filing() returns trigger as $$
declare
    v_filed_through date;
begin
    select filed_through into v_filed_through
      from vat_filing_watermark where jurisdiction = 'NL';

    if v_filed_through is null then
        return new;   -- nothing has been filed; there is nothing to protect
    end if;

    if new.valid_from <= v_filed_through then
        raise exception
            'a tax rule effective % would change periods already filed through '
            '% (CMP-014). Rules take effect ahead of the filed frontier, never '
            'behind it; correcting a filed period is a suppletie (FR-VAT-005), '
            'not a rate edit.',
            new.valid_from, v_filed_through;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger vat_rate_not_behind_a_filing_trg
    before insert on vat_rate
    for each row execute function vat_rule_not_behind_a_filing();

create trigger vat_rubriek_not_behind_a_filing_trg
    before insert on vat_rubriek
    for each row execute function vat_rule_not_behind_a_filing();

create trigger vat_treatment_rubriek_not_behind_a_filing_trg
    before insert on vat_treatment_rubriek
    for each row execute function vat_rule_not_behind_a_filing();

-- ===========================================================================
-- Append-only
-- ===========================================================================
-- The same shape 0019, 0020 and 0024 use. A rule row is a historical fact:
-- editing one does not correct history, it falsifies it.
create or replace function vat_rule_immutable() returns trigger as $$
begin
    raise exception
        'tax rules are append-only (CMP-014): % cannot be % once loaded. A rule '
        'is superseded by a later one, never edited.',
        tg_table_name, lower(tg_op);
end;
$$ language plpgsql;

create trigger vat_rate_no_update_trg before update on vat_rate
    for each row execute function vat_rule_immutable();
create trigger vat_rate_no_delete_trg before delete on vat_rate
    for each row execute function vat_rule_immutable();
create trigger vat_rate_no_truncate_trg before truncate on vat_rate
    for each statement execute function vat_rule_immutable();

create trigger vat_rubriek_no_update_trg before update on vat_rubriek
    for each row execute function vat_rule_immutable();
create trigger vat_rubriek_no_delete_trg before delete on vat_rubriek
    for each row execute function vat_rule_immutable();

create trigger vat_treatment_rubriek_no_update_trg before update on vat_treatment_rubriek
    for each row execute function vat_rule_immutable();
create trigger vat_treatment_rubriek_no_delete_trg before delete on vat_treatment_rubriek
    for each row execute function vat_rule_immutable();

-- The watermark is derived state and moves forward only. Not append-only, but
-- monotonic: letting it retreat would reopen every rule date it had passed.
create or replace function vat_watermark_only_advances() returns trigger as $$
begin
    if new.filed_through < old.filed_through then
        raise exception
            'the filed frontier cannot move backwards: % is earlier than % '
            '(CMP-014)', new.filed_through, old.filed_through;
    end if;
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger vat_watermark_only_advances_trg
    before update on vat_filing_watermark
    for each row execute function vat_watermark_only_advances();

-- ===========================================================================
-- Resolution: what applied on a date
-- ===========================================================================
create or replace function vat.rate_on(p_treatment text, p_on date)
returns numeric
language sql
stable
set search_path = public, app, pg_temp
as $$
    select r.rate
      from vat_rate r
     where r.treatment_code = p_treatment
       and r.valid_from <= p_on
     order by r.valid_from desc
     limit 1;
$$;

comment on function vat.rate_on(text, date) is
    'CMP-014. The rate in force for a treatment on a date - the greatest '
    'valid_from at or before it. NULL means no rule covers that date, which '
    'is a data gap rather than a rate of zero.';

create or replace function vat.rubriek_on(p_treatment text, p_on date)
returns table (turnover_rubriek text, vat_rubriek text)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select m.turnover_rubriek, m.vat_rubriek
      from vat_treatment_rubriek m
     where m.treatment_code = p_treatment
       and m.valid_from <= p_on
     order by m.valid_from desc
     limit 1;
$$;

-- The whole rule set in force on a date, which is what a return builder reads
-- and what the fingerprint below hashes.
create or replace function vat.rules_on(p_on date)
returns table (
    treatment_code    text,
    role              text,
    rate              numeric,
    turnover_rubriek  text,
    vat_rubriek       text
)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select t.code,
           t.role,
           vat.rate_on(t.code, p_on),
           (vat.rubriek_on(t.code, p_on)).turnover_rubriek,
           (vat.rubriek_on(t.code, p_on)).vat_rubriek
      from vat_treatment t
     order by t.code;
$$;

create or replace function vat.rubrieken_on(p_on date)
returns table (code text, kind text, description_nl text, description_en text)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select distinct on (r.code) r.code, r.kind, r.description_nl, r.description_en
      from vat_rubriek r
     where r.valid_from <= p_on
     order by r.code, r.valid_from desc;
$$;

-- ---------------------------------------------------------------------------
-- The fingerprint
-- ---------------------------------------------------------------------------
-- A hash of every rule in force on a date. Recorded on a period when it is
-- filed, so "have the rules under this return moved" is a comparison rather
-- than an argument.
--
-- numeric::text, not to_char: numeric's own text output is locale-independent,
-- where to_char's D pattern is not. A fingerprint that changed with lc_numeric
-- would report drift on every server that disagreed about a decimal point.
create or replace function vat.rules_fingerprint(p_on date)
returns text
language sql
stable
set search_path = public, app, pg_temp
as $$
    select encode(
        digest(
            coalesce(
                string_agg(
                    r.treatment_code || '|' || r.role || '|' ||
                    coalesce(r.rate::text, '-') || '|' ||
                    coalesce(r.turnover_rubriek, '-') || '|' ||
                    coalesce(r.vat_rubriek, '-'),
                    E'\n' order by r.treatment_code
                ),
                ''
            )
            || E'\n--\n'
            || coalesce(
                (select string_agg(b.code || '|' || b.kind, E'\n' order by b.code)
                   from vat.rubrieken_on(p_on) b),
                ''
            ),
            'sha256'
        ),
        'hex'
    )
      from vat.rules_on(p_on) r;
$$;

comment on function vat.rules_fingerprint(date) is
    'CMP-014. Hash of every VAT rule in force on a date. Stored on a period at '
    'filing time; vat.filed_period_drift() compares it back.';

-- ===========================================================================
-- RGS versions, effective-dated (CMP-014's third clause)
-- ===========================================================================
-- 0024 gave rgs_version an effective_from and nothing read it. This is the
-- resolution, and the report that says which versions cannot be resolved.
create or replace function ledger.rgs_version_on(p_on date)
returns rgs_version
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select v.*
      from rgs_version v
     where v.effective_from is not null
       and v.effective_from <= p_on
     order by v.effective_from desc
     limit 1;
$$;

comment on function ledger.rgs_version_on(date) is
    'CMP-014. The RGS version in force on a date. NULL when no loaded version '
    'declares an effective_from at or before it - see '
    'ledger.rgs_versions_without_effective_from().';

-- A version with no effective_from cannot be placed in time, so a period can
-- never resolve to it. That is a property of the DATASET, not of this code,
-- and the provisional 3.8 subset shipped in data/rgs/ has exactly that gap -
-- it declares no publication date because inventing one would be worse.
create or replace function ledger.rgs_versions_without_effective_from()
returns table (version text, status text, source text)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select v.version, v.status, v.source
      from rgs_version v
     where v.effective_from is null
     order by v.version;
$$;

-- ===========================================================================
-- Recording the rules a filing was made under
-- ===========================================================================
alter table period add column vat_rules_fingerprint text;

comment on column period.vat_rules_fingerprint is
    'CMP-014. The rule set in force on end_date at the moment this period was '
    'VAT-filed. Set by ledger.mark_period_filed; compared by '
    'vat.filed_period_drift().';

-- Replaces 0021's version: same signature, and it now records the fingerprint
-- and advances the filed frontier. Both belong in the same statement as the
-- status change - a period that is filed without moving the watermark would
-- leave the barrier open behind it.
create or replace function ledger.mark_period_filed(
    p_period_id         uuid,
    p_user_id           uuid,
    p_filing_reference  text default null
)
returns period
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_period period%rowtype;
begin
    select * into v_period from period where id = p_period_id;
    if not found then
        raise exception 'period % does not exist', p_period_id;
    end if;

    update period
       set status = 'vat_filed',
           filed_by_user_id = p_user_id,
           filing_reference = p_filing_reference,
           -- CMP-014: what the figures were computed under, captured at the
           -- moment they became a filing rather than derived later from rules
           -- that may since have moved.
           vat_rules_fingerprint = vat.rules_fingerprint(v_period.end_date)
     where id = p_period_id
    returning * into v_period;

    -- Advance the frontier. greatest() rather than a plain assignment because
    -- periods are not filed in date order - a late Q1 filing must not pull the
    -- barrier back over Q2.
    insert into vat_filing_watermark (jurisdiction, filed_through)
    values ('NL', v_period.end_date)
    on conflict (jurisdiction) do update
       set filed_through = greatest(
               vat_filing_watermark.filed_through, excluded.filed_through
           );

    return v_period;
end;
$$;

-- ===========================================================================
-- Detection
-- ===========================================================================
-- Filed periods whose rules have moved since they were filed. Structurally
-- impossible while vat_rule_not_behind_a_filing() stands, and reported anyway
-- for the reason 0020 gives about its gap report: a check that can only ever
-- be empty is the check on the thing that makes it empty.
create or replace function vat.filed_period_drift()
returns table (
    period_id           uuid,
    administration_id   uuid,
    end_date            date,
    filed_at            timestamptz,
    recorded_fingerprint text,
    current_fingerprint  text
)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select p.id, p.administration_id, p.end_date, p.filed_at,
           p.vat_rules_fingerprint,
           vat.rules_fingerprint(p.end_date)
      from period p
     where p.status = 'vat_filed'
       and p.vat_rules_fingerprint is not null
       and p.vat_rules_fingerprint is distinct from vat.rules_fingerprint(p.end_date);
$$;

comment on function vat.filed_period_drift() is
    'CMP-014. Filed periods whose VAT rules have changed underneath them. '
    'Always empty; a row means the barrier was bypassed. SECURITY INVOKER, so '
    'it answers for what the caller can see - run it as ledgr_ops (BYPASSRLS) '
    'for the cross-tenant truth, exactly like the NFR-033 integrity job.';

-- The watermark is derived, so it is checkable. Reads `period` directly, which
-- means it answers for whatever the caller can see - run it as ledgr_ops for
-- the cross-tenant truth.
create or replace function vat.watermark_drift()
returns table (recorded date, computed date)
language sql
stable
set search_path = public, app, pg_temp
as $$
    select w.filed_through,
           (select max(p.end_date) from period p where p.status = 'vat_filed')
      from vat_filing_watermark w
     where w.jurisdiction = 'NL'
       and w.filed_through is distinct from
           (select max(p.end_date) from period p where p.status = 'vat_filed');
$$;

-- ===========================================================================
-- Loading a ruleset
-- ===========================================================================
-- One jsonb document in, one ruleset out, idempotent on the checksum - the
-- same contract as ledger.load_rgs_version. A rate change is a new document.
--
-- Every rule row it inserts passes vat_rule_not_behind_a_filing(), so a
-- document that tries to restate history is refused as a whole: the function
-- is one transaction and one bad row rolls back the load.
create or replace function vat.load_ruleset(
    p_document          jsonb,
    p_source_checksum   text,
    p_allow_provisional boolean default false
)
returns vat_ruleset
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_ruleset vat_ruleset%rowtype;
    v_source  text := p_document->>'source';
    v_juris   text := coalesce(p_document->>'jurisdiction', 'NL');
    v_version text := p_document->>'ruleset_version';
begin
    if v_version is null or btrim(v_version) = '' then
        raise exception 'the document names no ruleset_version';
    end if;
    if v_source is null then
        raise exception 'the document declares no source; provenance is not optional';
    end if;
    if v_source = 'provisional-subset' and not p_allow_provisional then
        raise exception
            'VAT ruleset % is a provisional subset, not the official '
            'publication. Load it only with p_allow_provisional => true, and '
            'never as the basis for a filed return (FR-VAT-003).', v_version;
    end if;

    select * into v_ruleset
      from vat_ruleset where jurisdiction = v_juris and version = v_version;
    if found then
        if v_ruleset.source_checksum = p_source_checksum then
            return v_ruleset;
        end if;
        raise exception
            'VAT ruleset %/% is already loaded from a different document. Tax '
            'rules are append-only; publish a new ruleset version (CMP-014).',
            v_juris, v_version;
    end if;

    insert into vat_ruleset
        (jurisdiction, version, source, source_note, source_checksum)
    values
        (v_juris, v_version, v_source, p_document->>'source_note', p_source_checksum)
    returning * into v_ruleset;

    -- Treatments are upserted rather than inserted: the set is FR-AR-002's and
    -- does not change between rulesets, so a second ruleset must not collide
    -- with the first one's rows.
    insert into vat_treatment (code, role, description_nl, description_en)
    select t->>'code', t->>'role', t->>'description_nl', t->>'description_en'
      from jsonb_array_elements(coalesce(p_document->'treatments', '[]'::jsonb)) as t
    on conflict (code) do nothing;

    -- NFR-031: rates arrive as decimal STRINGS. A JSON number here would have
    -- been a Python float on the way in, and a rate is the first multiplier in
    -- the calculation path.
    if exists (
        select 1
          from jsonb_array_elements(coalesce(p_document->'rates', '[]'::jsonb)) as r
         where jsonb_typeof(r->'rate') <> 'string'
    ) then
        raise exception
            'rates must be decimal strings, not JSON numbers (NFR-031)';
    end if;

    insert into vat_rate (treatment_code, valid_from, rate, note, ruleset_id)
    select r->>'treatment', (r->>'valid_from')::date,
           (r->>'rate')::numeric(6, 3), r->>'note', v_ruleset.id
      from jsonb_array_elements(coalesce(p_document->'rates', '[]'::jsonb)) as r;

    insert into vat_rubriek
        (code, valid_from, kind, description_nl, description_en, ruleset_id)
    select b->>'code', (b->>'valid_from')::date, b->>'kind',
           b->>'description_nl', b->>'description_en', v_ruleset.id
      from jsonb_array_elements(coalesce(p_document->'rubrieken', '[]'::jsonb)) as b;

    insert into vat_treatment_rubriek
        (treatment_code, valid_from, turnover_rubriek, vat_rubriek, ruleset_id)
    select m->>'treatment', (m->>'valid_from')::date,
           m->>'turnover_rubriek', m->>'vat_rubriek', v_ruleset.id
      from jsonb_array_elements(
               coalesce(p_document->'treatment_rubriek', '[]'::jsonb)) as m;

    -- Every mapping must name a box the same document defines, at a date the
    -- box already exists. Checked after the fact rather than as a foreign key,
    -- because the box's key is (code, valid_from) and the mapping points at a
    -- code whose applicable row depends on the date.
    if exists (
        select 1
          from vat_treatment_rubriek m
         where m.ruleset_id = v_ruleset.id
           and not exists (
               select 1 from vat_rubriek b
                where b.code = m.turnover_rubriek and b.valid_from <= m.valid_from
           )
    ) then
        raise exception
            'a treatment maps to a rubriek that is not defined on the date the '
            'mapping takes effect';
    end if;

    return v_ruleset;
end;
$$;

-- ===========================================================================
-- Ownership and privileges
-- ===========================================================================
-- Owned by ledgr_ledger, like 0024's RGS reference data and for the same
-- reason: ledgr_app holds SELECT and nothing else, so application code cannot
-- invent a rate and then compute a return with it.
alter table vat_ruleset           owner to ledgr_ledger;
alter table vat_treatment         owner to ledgr_ledger;
alter table vat_rate              owner to ledgr_ledger;
alter table vat_rubriek           owner to ledgr_ledger;
alter table vat_treatment_rubriek owner to ledgr_ledger;
alter table vat_filing_watermark  owner to ledgr_ledger;

alter function vat_rule_not_behind_a_filing()  owner to ledgr_ledger;
alter function vat_rule_immutable()            owner to ledgr_ledger;
alter function vat_watermark_only_advances()   owner to ledgr_ledger;

alter function vat.rate_on(text, date)                       owner to ledgr_ledger;
alter function vat.rubriek_on(text, date)                    owner to ledgr_ledger;
alter function vat.rules_on(date)                            owner to ledgr_ledger;
alter function vat.rubrieken_on(date)                        owner to ledgr_ledger;
alter function vat.rules_fingerprint(date)                   owner to ledgr_ledger;
alter function vat.filed_period_drift()                      owner to ledgr_ledger;
alter function vat.watermark_drift()                         owner to ledgr_ledger;
alter function vat.load_ruleset(jsonb, text, boolean)        owner to ledgr_ledger;
alter function ledger.rgs_version_on(date)                   owner to ledgr_ledger;
alter function ledger.rgs_versions_without_effective_from()  owner to ledgr_ledger;

revoke all on vat_ruleset, vat_treatment, vat_rate, vat_rubriek,
              vat_treatment_rubriek, vat_filing_watermark from public;

-- Belt and braces over the triggers, as 0020 and 0024 do. DELETE and TRUNCATE
-- only: UPDATE stays, because vat_rate is referenced by nothing but a
-- foreign-key row lock would need it if it ever were, and because
-- vat_filing_watermark is legitimately updated by mark_period_filed. 0027 is
-- the lesson being applied rather than relearned.
revoke delete, truncate on vat_rate, vat_rubriek, vat_treatment_rubriek
    from ledgr_ledger;

grant select on vat_ruleset, vat_treatment, vat_rate, vat_rubriek,
                vat_treatment_rubriek, vat_filing_watermark
    to ledgr_app, ledgr_ops;

grant usage on schema vat to ledgr_app, ledgr_ops, ledgr_ledger;

-- ledgr_ledger writes the watermark from inside mark_period_filed.
grant select, insert, update on vat_filing_watermark to ledgr_ledger;

-- The two drift checks read `period`, and both are SECURITY INVOKER so that
-- they answer for what the caller can see - one tenant for ledgr_app, every
-- tenant for ledgr_ops. That only works if ledgr_ops can read the table at
-- all, and 0001 granted `period` to ledgr_app alone. Without this the
-- cross-tenant sweep fails with `permission denied for table period` rather
-- than reporting a clean ledger, which is the right direction to fail in and
-- still not one to ship.
--
-- SELECT only. Support tooling reads and never writes (CMP-009), and a period
-- moves status through ledger.mark_period_filed and nothing else.
grant select on period to ledgr_ops;

revoke all on all functions in schema vat from public;

-- Reading the rules is every tenant's business; loading them is an operator
-- action on CMP-013's schedule, so load_ruleset goes to ledgr_ops alone - the
-- same split 0024 makes for RGS.
grant execute on function vat.rate_on(text, date)      to ledgr_app, ledgr_ops;
grant execute on function vat.rubriek_on(text, date)   to ledgr_app, ledgr_ops;
grant execute on function vat.rules_on(date)           to ledgr_app, ledgr_ops;
grant execute on function vat.rubrieken_on(date)       to ledgr_app, ledgr_ops;
grant execute on function vat.rules_fingerprint(date)  to ledgr_app, ledgr_ops;
grant execute on function vat.filed_period_drift()     to ledgr_app, ledgr_ops;
grant execute on function vat.watermark_drift()        to ledgr_app, ledgr_ops;
grant execute on function vat.load_ruleset(jsonb, text, boolean) to ledgr_ops;

revoke all on all functions in schema ledger from public;
grant execute on function ledger.rgs_version_on(date) to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_versions_without_effective_from()
    to ledgr_app, ledgr_ops;

-- 0020, 0021, 0023 and 0024's grants are re-issued, following the convention
-- 0021 set: the revoke above strips PUBLIC, and this block keeps the file's
-- grant list the complete picture of who may call what in `ledger`.
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
grant execute on function ledger.set_account_rgs_code(uuid, text) to ledgr_app;
grant execute on function ledger.create_journal(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.set_journal_status(uuid, text) to ledgr_app;
grant execute on function ledger.create_party(uuid, text, text, text) to ledgr_app;
grant execute on function ledger.lock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.unlock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.mark_period_filed(uuid, uuid, text) to ledgr_app;
grant execute on function ledger.open_suppletie(uuid, text, uuid) to ledgr_app;
grant execute on function ledger.close_suppletie(uuid, text, text) to ledgr_app;
grant execute on function ledger.seed_chart_of_accounts(uuid, uuid, uuid, text)
    to ledgr_app;
grant execute on function ledger.apply_rgs_upgrade(uuid, uuid, uuid) to ledgr_app;

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
grant execute on function ledger.plan_rgs_upgrade(uuid, uuid)         to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_readiness(uuid)                  to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_mapping_deviations(uuid)         to ledgr_app, ledgr_ops;
grant execute on function ledger.chart_of_accounts(uuid, boolean)     to ledgr_app, ledgr_ops;
grant execute on function ledger.rgs_options(uuid, text)              to ledgr_app, ledgr_ops;
grant execute on function ledger.load_rgs_version(jsonb, text, boolean) to ledgr_ops;
grant execute on function ledger.publish_rgs_version(uuid)              to ledgr_ops;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
-- None, deliberately, on every table in this migration. Tax rules are public
-- law: every tenant reads the same rows, and a policy keyed on
-- app.current_org_id() would make the rate invisible to the tenant that needs
-- it. What protects them is that only ledgr_ops, through one function, writes.
--
-- vat_filing_watermark carries no tenant column for the same reason the
-- barrier needs it to: it is one fact about the whole system, and a per-tenant
-- view of it would let a rate be backdated over another tenant's filing.

commit;

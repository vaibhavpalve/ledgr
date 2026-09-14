-- 0021_period_locking.sql
-- FR-GL-007 (PRD §6.2). See docs/decisions/ADR-023-period-locking.md.
--
--   FR-GL-007  Period locking per fiscal period with a defined unlock
--              authority; VAT-filed periods are hard-locked and require a
--              suppletie flow to change.
--
-- Related, and deliberately NOT built here:
--   FR-VAT-005  Suppletie (correction return) flow for filed periods, with a
--               clear link to the original filing.
--
-- 0020 already refuses postings into a non-open period. What it did not have
-- is an *authority*: any code holding a database connection could flip
-- period.status and post freely. This migration closes that.
--
-- ===========================================================================
-- "A defined unlock authority", in two layers
-- ===========================================================================
--
-- 1. WHO may unlock is an authorization question, and Appendix A already
--    answers it: "Lock / unlock periods" is Full for Owner and Accountant and
--    absent for everyone else - PRD §8.4 says a Bookkeeper explicitly cannot
--    "unlock closed periods". That check lives in api.ledger.periods, through
--    the one authorization library (CLAUDE.md rule 3).
--
-- 2. WHAT enforces that the check happened is here: period.status can only
--    change from inside the ledger's SECURITY DEFINER functions, which run as
--    ledgr_ledger. A direct UPDATE by ledgr_app is refused by the trigger
--    below, so "the service checks the permission first" is not a convention
--    a caller can skip - there is no other way to move the column.
--
-- The rest of the period row stays writable as 0001 granted it. Only `status`
-- is behind the API, because only `status` is what FR-GL-007 governs.
--
-- ===========================================================================
-- The hard lock, and what "requires a suppletie flow" means concretely
-- ===========================================================================
--
-- Once a period is `vat_filed`, no transition out of it exists. Not for the
-- Owner, not for an Accountant, not through the API functions. Reopening
-- would let the ledger and a return already sent to the Belastingdienst
-- diverge with nothing recording that they had.
--
-- The route to changing a filed period is therefore not an unlock but a
-- SUPPLETIE: a correction return. In Dutch practice the original aangifte is
-- never amended; a separate filing states the difference. So:
--
--   * ledger.open_suppletie() opens a correction against a FILED period
--   * corrections are posted into an OPEN period, never into the filed one
--   * those entries carry suppletie_id, which is the "clear link to the
--     original filing" FR-VAT-005 asks for
--
-- What is built here is the ledger side: the record, the link, and the
-- guards. The return itself - rubriek values, the Digipoort submission, the
-- filing receipt - is FR-VAT-005 and is NOT built.

begin;

-- ---------------------------------------------------------------------------
-- Filing provenance on the period
-- ---------------------------------------------------------------------------
-- locked_at / locked_by_user_id already exist (0001). These are their filing
-- counterparts, so "who hard-locked this and when" is answerable from the row
-- rather than only from the audit log.
alter table period
    add column filed_at          timestamptz,
    add column filed_by_user_id  uuid references users(id),
    -- The Belastingdienst's acknowledgement, when FR-VAT-005 lands. Here now
    -- because a period cannot be un-filed, so a later backfill would have no
    -- way to reach the rows that need it.
    add column filing_reference  text;

-- ---------------------------------------------------------------------------
-- vat_suppletie (the ledger side of FR-VAT-005)
-- ---------------------------------------------------------------------------
create table vat_suppletie (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- The FILED period being corrected. Not the period the corrections are
    -- posted into - that is whichever period is open when the error is found,
    -- and is recorded per entry.
    period_id           uuid not null references period(id),

    status              text not null default 'open'
                            check (status in ('open', 'submitted', 'filed', 'withdrawn')),

    -- Why this correction exists. NOT NULL and non-blank: a suppletie with no
    -- stated reason is the one an inspector asks about, and "we found an
    -- error" recorded nowhere is not an answer.
    reason              text not null check (length(btrim(reason)) > 0),

    opened_by_user_id   uuid not null references users(id),
    opened_at           timestamptz not null default now(),
    submitted_at        timestamptz,
    filed_at            timestamptz,
    filing_reference    text
);

create index vat_suppletie_administration_idx on vat_suppletie(administration_id);
create index vat_suppletie_period_idx on vat_suppletie(period_id);
create index vat_suppletie_organization_idx on vat_suppletie(organization_id);

-- At most one suppletie open against a period at a time. Two concurrent open
-- corrections against one filed period would make "the net difference to
-- report" ambiguous, and the whole point of the link is that it is
-- unambiguous. Closed ones are unconstrained: a period may need correcting
-- more than once over its life.
create unique index vat_suppletie_one_open_per_period_idx
    on vat_suppletie(period_id)
    where status = 'open';

-- ---------------------------------------------------------------------------
-- journal_entry.suppletie_id - the link FR-VAT-005 asks for
-- ---------------------------------------------------------------------------
alter table journal_entry
    add column suppletie_id uuid references vat_suppletie(id);

create index journal_entry_suppletie_idx
    on journal_entry(suppletie_id)
    where suppletie_id is not null;

-- ===========================================================================
-- The transition guard, rewritten
-- ===========================================================================
-- Replaces 0020's version. Three things are new: the ledgr_ledger gate, the
-- requirement that a transition names an actor, and filing provenance.
create or replace function period_status_transition() returns trigger as $$
begin
    if new.status = old.status then
        return new;
    end if;

    -- Layer 2 of "a defined unlock authority" (see the header). Inside a
    -- SECURITY DEFINER function current_user is the function's OWNER, so this
    -- is true exactly when the change came through ledger.lock_period,
    -- ledger.unlock_period or ledger.mark_period_filed - each of which has
    -- already checked the caller's permission.
    --
    -- Deliberately no exemption for a superuser. One would buy nothing: a
    -- superuser can drop this trigger outright, so an exemption would only
    -- widen the path for roles that cannot.
    if current_user <> 'ledgr_ledger' then
        raise exception
            'period status changes through ledger.lock_period, '
            'ledger.unlock_period or ledger.mark_period_filed, which check the '
            'unlock authority first (FR-GL-007); a direct UPDATE bypasses that '
            'check and is refused';
    end if;

    -- The hard lock. Terminal, with no branch and no flag - the same shape
    -- 0019 and 0020 use for the guards that must not be talked into
    -- permitting anything.
    if old.status = 'vat_filed' then
        raise exception
            'period % is VAT-filed and hard-locked; correcting it requires a '
            'suppletie, not an unlock (FR-GL-007). See ledger.open_suppletie.',
            old.period_number;
    end if;

    if not (
        (old.status = 'open'   and new.status in ('locked', 'vat_filed')) or
        (old.status = 'locked' and new.status in ('open', 'vat_filed'))
    ) then
        raise exception 'unsupported period transition % -> % (FR-GL-007)',
            old.status, new.status;
    end if;

    if new.status = 'locked' then
        -- A lock that names nobody cannot answer "who locked this", which is
        -- the first question asked about a closed period.
        if new.locked_by_user_id is null then
            raise exception
                'locking a period must record who locked it (FR-GL-007)';
        end if;
        new.locked_at := now();

    elsif new.status = 'open' then
        -- Unlocking clears the lock. WHO unlocked it is not a column: it is an
        -- audit_log entry, written in the same transaction by
        -- api.ledger.periods. That log is append-only and hash-chained (0019),
        -- which is a stronger record than a column any later UPDATE could
        -- overwrite - and unlike a column it survives the next lock/unlock
        -- cycle instead of being replaced by it.
        new.locked_at := null;
        new.locked_by_user_id := null;

    elsif new.status = 'vat_filed' then
        if new.filed_by_user_id is null then
            raise exception
                'filing a period must record who filed it (FR-GL-007)';
        end if;
        new.filed_at := now();
        -- A filed period is also locked, and stays that way. Without this a
        -- filed period would show locked_at = null and read as "never
        -- locked", which is the opposite of the truth.
        new.locked_at := coalesce(old.locked_at, now());
        new.locked_by_user_id :=
            coalesce(old.locked_by_user_id, new.filed_by_user_id);
    end if;

    return new;
end;
$$ language plpgsql;

-- ===========================================================================
-- Postings under a suppletie
-- ===========================================================================
-- Replaces 0020's journal_entry_validate(), adding the suppletie checks. The
-- rest is unchanged and repeated in full rather than patched, because a
-- trigger function is replaced wholesale and a partial copy would silently
-- drop whichever checks were left out.
create or replace function journal_entry_validate() returns trigger as $$
declare
    v_period        period%rowtype;
    v_year          fiscal_year%rowtype;
    v_journal       ledger_journal%rowtype;
    v_suppletie     vat_suppletie%rowtype;
begin
    -- FR-GL-002: exactly one journal and one period, both belonging to this
    -- administration.
    select * into v_period from period where id = new.period_id;
    if not found then
        raise exception 'period % does not exist', new.period_id;
    end if;
    if v_period.administration_id <> new.administration_id then
        raise exception
            'period % belongs to administration %, not % (FR-GL-002)',
            new.period_id, v_period.administration_id, new.administration_id;
    end if;

    select * into v_year from fiscal_year where id = v_period.fiscal_year_id;
    if not found then
        raise exception 'fiscal year % does not exist', v_period.fiscal_year_id;
    end if;

    select * into v_journal from ledger_journal where id = new.journal_id;
    if not found then
        raise exception 'journal % does not exist', new.journal_id;
    end if;
    if v_journal.administration_id <> new.administration_id then
        raise exception
            'journal % belongs to administration %, not % (FR-GL-002)',
            new.journal_id, v_journal.administration_id, new.administration_id;
    end if;
    if v_journal.status <> 'active' then
        raise exception 'journal % (%) is blocked', v_journal.code, new.journal_id;
    end if;

    new.fiscal_year_id := v_period.fiscal_year_id;
    new.organization_id := v_period.organization_id;

    -- FR-GL-007.
    if v_period.status = 'vat_filed' then
        raise exception
            'period % is VAT-filed and hard-locked; a change requires a suppletie '
            '(FR-GL-007)', v_period.period_number;
    end if;
    if v_period.status <> 'open' then
        raise exception
            'period % is % and cannot be posted to (FR-GL-007)',
            v_period.period_number, v_period.status;
    end if;
    if v_year.status <> 'open' then
        raise exception
            'fiscal year % is closed (FR-GL-007/FR-GL-008)', v_year.start_date;
    end if;

    if new.entry_date < v_period.start_date or new.entry_date > v_period.end_date then
        raise exception
            'entry date % falls outside period % (% .. %)',
            new.entry_date, v_period.period_number,
            v_period.start_date, v_period.end_date;
    end if;

    -- FR-GL-007 / FR-VAT-005: the correction path for a filed period.
    if new.suppletie_id is not null then
        select * into v_suppletie from vat_suppletie where id = new.suppletie_id;
        if not found then
            raise exception 'suppletie % does not exist', new.suppletie_id;
        end if;
        if v_suppletie.administration_id <> new.administration_id then
            raise exception 'suppletie % belongs to another administration',
                new.suppletie_id;
        end if;
        if v_suppletie.status <> 'open' then
            raise exception
                'suppletie % is % and accepts no further corrections (FR-VAT-005)',
                new.suppletie_id, v_suppletie.status;
        end if;
        -- The correction goes into an OPEN period. Pointing it at the filed
        -- period it corrects is the mistake this exists to catch: that period
        -- is hard-locked, and a correction that could reach it would make the
        -- hard lock meaningless.
        if v_suppletie.period_id = new.period_id then
            raise exception
                'a suppletie correction is posted into an open period, not into '
                'the filed period it corrects (FR-GL-007)';
        end if;
    end if;

    -- FR-GL-013. Allocated last, so every rejection above leaves the series
    -- untouched - which is what "gapless" needs from a failure.
    insert into journal_sequence
        (organization_id, administration_id, journal_id, fiscal_year_id, next_number)
    values
        (new.organization_id, new.administration_id, new.journal_id,
         new.fiscal_year_id, 1)
    on conflict (journal_id, fiscal_year_id) do nothing;

    update journal_sequence
       set next_number = next_number + 1
     where journal_id = new.journal_id
       and fiscal_year_id = new.fiscal_year_id
    returning next_number - 1 into new.entry_number;

    new.posted_at := now();

    return new;
end;
$$ language plpgsql;

-- ===========================================================================
-- The API
-- ===========================================================================
-- Each of these is the ONLY way to reach the transition it performs, because
-- period_status_transition() refuses a status change from any other role. The
-- permission check that FR-GL-007 calls the "defined unlock authority" happens
-- in api.ledger.periods before these are called; these are what make skipping
-- it impossible rather than merely discouraged.

-- ---------------------------------------------------------------------------
-- ledger.post_entry gains p_suppletie_id
-- ---------------------------------------------------------------------------
-- DROP then CREATE, not CREATE OR REPLACE. Postgres identifies a function by
-- its argument types, so adding a parameter with CREATE OR REPLACE produces a
-- second OVERLOAD rather than a new version - and a caller passing the old
-- 11 arguments would silently keep reaching the old body, which knows nothing
-- about suppletie_id. Two overloads of the ledger's only write function is
-- exactly the ambiguity this schema cannot afford.
--
-- ledger.reverse_entry calls post_entry. PL/pgSQL bodies are not
-- dependency-tracked, so the drop succeeds and the call resolves at run time -
-- which is fine because both live inside this transaction. It is recreated
-- below anyway, so that its call site names the new signature explicitly.
drop function if exists ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text
);

create function ledger.post_entry(
    p_administration_id  uuid,
    p_journal_id         uuid,
    p_period_id          uuid,
    p_entry_date         date,
    p_description        text,
    p_document_reference text,
    p_posted_by_user_id  uuid,
    p_source_system      text,
    p_lines              jsonb,
    p_reverses_entry_id  uuid default null,
    p_idempotency_key    text default null,
    p_suppletie_id       uuid default null
)
returns journal_entry
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_entry   journal_entry%rowtype;
    v_line    jsonb;
    v_index   smallint := 0;
begin
    -- NFR-032. Checked before anything is allocated, so a retry does not
    -- consume an entry number.
    if p_idempotency_key is not null then
        select * into v_entry
          from journal_entry
         where administration_id = p_administration_id
           and idempotency_key = p_idempotency_key;
        if found then
            return v_entry;
        end if;
    end if;

    if jsonb_typeof(p_lines) <> 'array' then
        raise exception 'lines must be a JSON array, got %',
            coalesce(jsonb_typeof(p_lines), 'null');
    end if;
    if jsonb_array_length(p_lines) < 2 then
        raise exception
            'a journal entry needs at least two lines, got % (FR-GL-001)',
            jsonb_array_length(p_lines);
    end if;

    insert into journal_entry (
        organization_id, administration_id, fiscal_year_id, period_id,
        journal_id, entry_number, entry_date, description, document_reference,
        posted_by_user_id, source_system, reverses_entry_id, idempotency_key,
        suppletie_id
    )
    values (
        -- organization_id, fiscal_year_id and entry_number are overwritten by
        -- journal_entry_validate(); the placeholders exist only because the
        -- columns are NOT NULL.
        '00000000-0000-0000-0000-000000000000',
        p_administration_id,
        '00000000-0000-0000-0000-000000000000',
        p_period_id,
        p_journal_id,
        1,
        p_entry_date,
        p_description,
        p_document_reference,
        p_posted_by_user_id,
        p_source_system,
        p_reverses_entry_id,
        p_idempotency_key,
        p_suppletie_id
    )
    returning * into v_entry;

    for v_line in select * from jsonb_array_elements(p_lines)
    loop
        v_index := v_index + 1;

        if jsonb_typeof(v_line->'debit') <> 'string'
           or jsonb_typeof(v_line->'credit') <> 'string' then
            raise exception
                'line %: debit and credit must be decimal strings, not JSON '
                'numbers (NFR-031); got % and %',
                v_index,
                coalesce(jsonb_typeof(v_line->'debit'), 'missing'),
                coalesce(jsonb_typeof(v_line->'credit'), 'missing');
        end if;

        insert into journal_line (
            journal_entry_id, organization_id, administration_id, line_number,
            account_id, debit, credit, subledger_party_id, cost_centre_id,
            description
        )
        values (
            v_entry.id,
            '00000000-0000-0000-0000-000000000000',   -- derived by the trigger
            '00000000-0000-0000-0000-000000000000',   -- derived by the trigger
            v_index,
            (v_line->>'account_id')::uuid,
            (v_line->>'debit')::numeric(19, 2),
            (v_line->>'credit')::numeric(19, 2),
            (v_line->>'subledger_party_id')::uuid,
            (v_line->>'cost_centre_id')::uuid,
            v_line->>'description'
        );
    end loop;

    -- The balance is checked at COMMIT by journal_entry_balanced_trg, which
    -- covers this function and anything that ever writes beside it.
    return v_entry;
exception
    when unique_violation then
        if p_idempotency_key is null then
            raise;
        end if;
        select * into v_entry
          from journal_entry
         where administration_id = p_administration_id
           and idempotency_key = p_idempotency_key;
        if not found then
            raise;
        end if;
        return v_entry;
end;
$$;

-- Recreated so its call to post_entry names the new signature. Unchanged
-- otherwise: a reversal never carries a suppletie of its own - it mirrors an
-- entry, and if that entry was itself a correction the link is already on it.
create or replace function ledger.reverse_entry(
    p_entry_id          uuid,
    p_period_id         uuid,
    p_entry_date        date,
    p_description       text,
    p_posted_by_user_id uuid,
    p_source_system     text,
    p_idempotency_key   text default null
)
returns journal_entry
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_original journal_entry%rowtype;
    v_lines    jsonb;
begin
    select * into v_original from journal_entry where id = p_entry_id;
    if not found then
        raise exception 'entry % does not exist', p_entry_id;
    end if;

    if exists (select 1 from journal_entry where reverses_entry_id = p_entry_id) then
        raise exception
            'entry % has already been reversed (FR-GL-003)', p_entry_id;
    end if;

    -- A literal '.' here, not the locale-aware 'D': post_entry casts these
    -- strings back with (v_line->>'debit')::numeric (0021/0035), and a
    -- text-to-numeric cast always requires '.' regardless of server locale.
    -- 'D' instead emits whatever lc_numeric's decimal separator is - a
    -- comma on this database (English_Netherlands.1252, fitting for a Dutch
    -- bookkeeping product but not what a numeric cast accepts) - so every
    -- reversal failed with "invalid input syntax for type numeric: 0,00"
    -- the first time this ran against a real server rather than the C
    -- locale a fresh throwaway container defaults to.
    select jsonb_agg(
               jsonb_build_object(
                   'account_id', l.account_id,
                   'debit', to_char(l.credit, 'FM9999999999999999990.00'),
                   'credit', to_char(l.debit, 'FM9999999999999999990.00'),
                   'subledger_party_id', l.subledger_party_id,
                   'cost_centre_id', l.cost_centre_id,
                   'description', l.description
               )
               order by l.line_number
           )
      into v_lines
      from journal_line l
     where l.journal_entry_id = p_entry_id;

    return ledger.post_entry(
        p_administration_id  => v_original.administration_id,
        p_journal_id         => v_original.journal_id,
        p_period_id          => p_period_id,
        p_entry_date         => p_entry_date,
        p_description        => p_description,
        p_document_reference => v_original.document_reference,
        p_posted_by_user_id  => p_posted_by_user_id,
        p_source_system      => p_source_system,
        p_lines              => v_lines,
        p_reverses_entry_id  => p_entry_id,
        p_idempotency_key    => p_idempotency_key,
        p_suppletie_id       => v_original.suppletie_id
    );
end;
$$;

create or replace function ledger.lock_period(
    p_period_id uuid,
    p_user_id   uuid
)
returns period
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_period period%rowtype;
begin
    update period
       set status = 'locked',
           locked_by_user_id = p_user_id
     where id = p_period_id
    returning * into v_period;
    if not found then
        raise exception 'period % does not exist', p_period_id;
    end if;
    return v_period;
end;
$$;

create or replace function ledger.unlock_period(
    p_period_id uuid,
    p_user_id   uuid
)
returns period
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_period period%rowtype;
begin
    -- p_user_id is not stored: unlocking clears the lock columns. It is in the
    -- signature because the caller must have resolved an actor to get here,
    -- and a function that did not ask for one would read as though unlocking
    -- were unattributed. The attribution is the audit_log entry.
    perform p_user_id;

    update period
       set status = 'open'
     where id = p_period_id
    returning * into v_period;
    if not found then
        raise exception 'period % does not exist', p_period_id;
    end if;
    return v_period;
end;
$$;

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
    update period
       set status = 'vat_filed',
           filed_by_user_id = p_user_id,
           filing_reference = p_filing_reference
     where id = p_period_id
    returning * into v_period;
    if not found then
        raise exception 'period % does not exist', p_period_id;
    end if;
    return v_period;
end;
$$;

create or replace function ledger.open_suppletie(
    p_period_id uuid,
    p_reason    text,
    p_user_id   uuid
)
returns vat_suppletie
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_period    period%rowtype;
    v_suppletie vat_suppletie%rowtype;
begin
    select * into v_period from period where id = p_period_id;
    if not found then
        raise exception 'period % does not exist', p_period_id;
    end if;

    -- A suppletie corrects a FILED return. Opening one against a period that
    -- was never filed would produce a correction to nothing, and would let a
    -- caller route ordinary postings through the suppletie path to make them
    -- look like statutory corrections.
    if v_period.status <> 'vat_filed' then
        raise exception
            'period % is % and has no filed return to correct; a suppletie '
            'corrects a VAT-filed period (FR-GL-007)',
            v_period.period_number, v_period.status;
    end if;

    insert into vat_suppletie
        (organization_id, administration_id, period_id, reason, opened_by_user_id)
    values
        (v_period.organization_id, v_period.administration_id, p_period_id,
         p_reason, p_user_id)
    returning * into v_suppletie;

    return v_suppletie;
end;
$$;

create or replace function ledger.close_suppletie(
    p_suppletie_id     uuid,
    p_status           text,
    p_filing_reference text default null
)
returns vat_suppletie
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_suppletie vat_suppletie%rowtype;
begin
    if p_status not in ('submitted', 'filed', 'withdrawn') then
        raise exception 'a suppletie closes as submitted, filed or withdrawn, not %',
            p_status;
    end if;

    update vat_suppletie
       set status = p_status,
           submitted_at = case when p_status = 'submitted' then now()
                               else submitted_at end,
           filed_at = case when p_status = 'filed' then now() else filed_at end,
           filing_reference = coalesce(p_filing_reference, filing_reference)
     where id = p_suppletie_id
       and status = 'open'
    returning * into v_suppletie;

    if not found then
        raise exception
            'suppletie % does not exist or is already closed', p_suppletie_id;
    end if;
    return v_suppletie;
end;
$$;

-- The corrections attached to a suppletie, and their net effect. FR-VAT-005's
-- "clear link to the original filing", read from the other end.
create or replace function ledger.suppletie_corrections(p_suppletie_id uuid)
returns table (
    entry_id      uuid,
    entry_number  bigint,
    entry_date    date,
    period_id     uuid,
    description   text,
    total_debit   numeric(19, 2),
    total_credit  numeric(19, 2)
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select e.id, e.entry_number, e.entry_date, e.period_id, e.description,
           coalesce(sum(l.debit), 0)::numeric(19, 2),
           coalesce(sum(l.credit), 0)::numeric(19, 2)
      from journal_entry e
      left join journal_line l on l.journal_entry_id = e.id
     where e.suppletie_id = p_suppletie_id
     group by e.id, e.entry_number, e.entry_date, e.period_id, e.description
     order by e.entry_number;
$$;

-- ===========================================================================
-- Ownership and privileges
-- ===========================================================================
-- Same superuser dependency as 0020: these run after ownership moves, and
-- migrations are applied as a superuser (ledgr_migrator is NOCREATEROLE and
-- could not have created the roles either).
alter table vat_suppletie owner to ledgr_ledger;

-- period_status_transition() must be owned by ledgr_ledger too. It compares
-- current_user against 'ledgr_ledger', which is only ever true inside a
-- definer function owned by that role - but the trigger function itself is
-- not SECURITY DEFINER, so ownership here is about who may DROP it. `period`
-- is ledgr_migrator's table; the guard on its status column is not.
alter function period_status_transition() owner to ledgr_ledger;
alter function journal_entry_validate()   owner to ledgr_ledger;

-- Recreated above, so ownership must be re-established: a DROP + CREATE makes
-- the migration role the owner again, and SECURITY DEFINER would then run it
-- with privileges that cannot write a posting.
alter function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text, uuid
) owner to ledgr_ledger;
alter function ledger.reverse_entry(uuid, uuid, date, text, uuid, text, text)
    owner to ledgr_ledger;

alter function ledger.lock_period(uuid, uuid)                    owner to ledgr_ledger;
alter function ledger.unlock_period(uuid, uuid)                  owner to ledgr_ledger;
alter function ledger.mark_period_filed(uuid, uuid, text)        owner to ledgr_ledger;
alter function ledger.open_suppletie(uuid, text, uuid)           owner to ledgr_ledger;
alter function ledger.close_suppletie(uuid, text, text)          owner to ledgr_ledger;
alter function ledger.suppletie_corrections(uuid)                owner to ledgr_ledger;

-- ledgr_ledger updates `period`, which it does not own. 0001 granted UPDATE to
-- ledgr_app only, so without this the definer functions above cannot write.
grant select, update on period to ledgr_ledger;

revoke all on vat_suppletie from public;
grant select on vat_suppletie to ledgr_app, ledgr_ops;

revoke all on all functions in schema ledger from public;

grant execute on function ledger.lock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.unlock_period(uuid, uuid) to ledgr_app;
grant execute on function ledger.mark_period_filed(uuid, uuid, text) to ledgr_app;
grant execute on function ledger.open_suppletie(uuid, text, uuid) to ledgr_app;
grant execute on function ledger.close_suppletie(uuid, text, text) to ledgr_app;
grant execute on function ledger.suppletie_corrections(uuid)
    to ledgr_app, ledgr_ops;

-- 0020's grants are re-issued: `revoke all on all functions in schema ledger
-- from public` above strips PUBLIC, but an explicit grant to ledgr_app made in
-- 0020 survives it. Listed anyway so this file's grant block is the complete
-- picture rather than a delta a reader has to compose with 0020's.
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
grant execute on function ledger.numbering_gaps(uuid, uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.trial_balance(uuid, uuid) to ledgr_app, ledgr_ops;
grant execute on function ledger.subledger_balance(uuid, text) to ledgr_app, ledgr_ops;
grant execute on function ledger.control_account_reconciliation(uuid)
    to ledgr_app, ledgr_ops;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
alter table vat_suppletie enable row level security;
alter table vat_suppletie force row level security;

create policy vat_suppletie_select on vat_suppletie
    for select using (app.has_administration_access(administration_id));
create policy vat_suppletie_insert on vat_suppletie
    for insert with check (app.has_administration_access(administration_id));
create policy vat_suppletie_update on vat_suppletie
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- No DELETE policy and no DELETE grant: a suppletie is a statutory correction
-- record. It is withdrawn by status, never removed.

commit;

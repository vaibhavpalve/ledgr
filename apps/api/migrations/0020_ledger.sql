-- 0020_ledger.sql
-- FR-GL-001 through FR-GL-007 and FR-GL-013 (PRD §6.2).
-- See docs/decisions/ADR-022-ledger-bounded-context.md.
--
--   FR-GL-001  no transaction persists unless debits equal credits
--   FR-GL-002  journal types; each posting in exactly one journal and period
--   FR-GL-003  postings immutable once committed; corrections by reversal
--   FR-GL-004  every posting carries administration, fiscal year, period,
--              journal, date, description, document reference, actor,
--              source system, timestamp
--   FR-GL-005  chart of accounts: type, RGS code, VAT default, blocked state
--   FR-GL-006  AR/AP sub-ledgers reconciled continuously to control accounts;
--              a control account cannot be posted to directly
--   FR-GL-007  period locking with a defined unlock authority; VAT-filed
--              periods hard-locked
--   FR-GL-013  gapless numbering per journal per year, with gap detection
--
--   CMP-009    deletion of posted entries impossible through any interface
--   NFR-031    decimal types; floating point prohibited
--   NFR-032    idempotency; retries must never double-post
--
-- ===========================================================================
-- The bounded context, expressed as privileges
-- ===========================================================================
--
-- CLAUDE.md's first architectural non-negotiable: "the ledger is a separate
-- bounded context with a narrow API. Nothing writes to posting tables except
-- the ledger service."
--
-- That is a statement about privileges, not about code review. So:
--
--   * The posting tables are owned by ledgr_ledger - NOLOGIN, granted to
--     nobody, the same shape 0001 used for ledgr_bootstrap and 0019 for
--     ledgr_audit.
--   * ledgr_app holds SELECT on them and NOTHING ELSE. No INSERT, no UPDATE,
--     no DELETE, no TRUNCATE. A statement never granted cannot be issued, so
--     application code physically cannot write a posting - not by mistake,
--     not by a future developer who did not read this file.
--   * The only writers are the SECURITY DEFINER functions in the `ledger`
--     schema, which run as ledgr_ledger. Their signatures ARE the narrow API,
--     and the surface a reviewer has to audit is exactly their bodies.
--
-- Tenant isolation survives that boundary because RLS predicates read
-- app.current_org_id(), which is SESSION state, not role state. A definer
-- function called by tenant A sees tenant A's rows and no others - the
-- privilege escalation is over WHAT may be written, never over WHOSE.
--
-- ===========================================================================
-- What enforces each invariant
-- ===========================================================================
--
--   balance          deferred constraint trigger, evaluated at COMMIT
--   one-sided lines  CHECK constraint
--   immutability     no grants + unconditional RAISE triggers + NOLOGIN owner
--   sealed at commit created_xid comparison on line insert
--   reversal mirrors deferred constraint trigger
--   control accounts BEFORE INSERT trigger (needs a cross-table lookup)
--   gapless numbers  allocator row + UNIQUE index + gap report
--   period open      BEFORE INSERT trigger
--   decimal only     numeric columns; jsonb amounts must be strings
--
-- Every one of these binds any writer, including the definer functions
-- themselves. The functions are the API; the triggers are the invariants. A
-- bug in the former cannot violate the latter.

begin;

-- ---------------------------------------------------------------------------
-- ledgr_ledger
-- ---------------------------------------------------------------------------
-- Owns the posting tables and their triggers. NOLOGIN, so no session can be
-- opened as it; granted to no role, so nobody can SET ROLE to it. It exists
-- to be an owner that is neither ledgr_app nor ledgr_migrator.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ledgr_ledger') then
        create role ledgr_ledger nosuperuser nocreatedb nocreaterole nobypassrls nologin;
    end if;
end;
$$;

create schema if not exists ledger;

comment on schema ledger is
    'The ledger bounded context. Every function here is part of its public '
    'API; nothing outside it may write to a posting table.';

-- ===========================================================================
-- Chart of accounts (FR-GL-005)
-- ===========================================================================
create table ledger_account (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    code                text not null,
    name                text not null,

    account_type        text not null check (
        account_type in ('asset', 'liability', 'equity', 'revenue', 'expense')
    ),

    -- FR-GL-005: RGS (Referentie Grootboekschema) reference code. Nullable
    -- because a customer may carry accounts with no RGS equivalent; the
    -- mapping completeness check belongs to reporting, not to the schema.
    rgs_code            text,

    -- FR-GL-005: VAT default applied when this account is selected.
    default_vat_code    text,

    -- FR-GL-005: blocked/active. A blocked account keeps its history and
    -- refuses new postings - see ledger_line_validate().
    status              text not null default 'active'
                            check (status in ('active', 'blocked')),

    -- FR-GL-006. NULL for an ordinary account. Non-NULL makes this a control
    -- account, which can then ONLY be posted to with a sub-ledger party.
    control_kind        text check (
        control_kind in ('accounts_receivable', 'accounts_payable')
    ),

    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint ledger_account_code_unique unique (administration_id, code)
);

create index ledger_account_administration_idx on ledger_account(administration_id);
create index ledger_account_organization_idx on ledger_account(organization_id);

-- At most one control account per kind per administration. Two AR control
-- accounts would make "reconciled to control accounts" ambiguous about which.
create unique index ledger_account_one_control_per_kind_idx
    on ledger_account(administration_id, control_kind)
    where control_kind is not null;

-- ---------------------------------------------------------------------------
-- Identity fields are immutable
-- ---------------------------------------------------------------------------
-- Flipping control_kind on an account that already has postings would
-- retroactively invalidate FR-GL-006 for every one of them: entries that were
-- legitimately direct when posted become direct postings to a control
-- account. Changing account_type would silently restate the balance sheet.
-- Neither is a correction; both are a new account.
create or replace function ledger_account_identity_immutable() returns trigger as $$
begin
    if new.code is distinct from old.code then
        raise exception 'ledger_account.code is immutable (account %)', old.id;
    end if;
    if new.account_type is distinct from old.account_type then
        raise exception 'ledger_account.account_type is immutable (account %)', old.id;
    end if;
    if new.control_kind is distinct from old.control_kind then
        raise exception
            'ledger_account.control_kind is immutable (account %): changing it would '
            'retroactively invalidate FR-GL-006 for every posting already made',
            old.id;
    end if;
    if new.administration_id is distinct from old.administration_id
       or new.organization_id is distinct from old.organization_id then
        raise exception 'ledger_account tenancy is immutable (account %)', old.id;
    end if;
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger ledger_account_identity_immutable_trg
    before update on ledger_account
    for each row execute function ledger_account_identity_immutable();

-- ===========================================================================
-- Journals (FR-GL-002)
-- ===========================================================================
create table ledger_journal (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    code                text not null,
    name                text not null,

    -- FR-GL-002's list, closed. A type outside it is a schema change and a
    -- conversation, not a config value.
    journal_type        text not null check (
        journal_type in ('sales', 'purchase', 'bank', 'cash',
                         'memorial', 'opening', 'closing')
    ),

    status              text not null default 'active'
                            check (status in ('active', 'blocked')),

    created_at          timestamptz not null default now(),

    constraint ledger_journal_code_unique unique (administration_id, code)
);

create index ledger_journal_administration_idx on ledger_journal(administration_id);
create index ledger_journal_organization_idx on ledger_journal(organization_id);

-- ===========================================================================
-- Sub-ledger parties (FR-GL-006)
-- ===========================================================================
-- The counterparty a receivable is owed by or a payable is owed to. This is
-- the whole of the sub-ledger's master data: the BALANCES are not stored
-- here, they are derived from journal_line. See the note on
-- ledger.subledger_balance() for why that matters.
create table subledger_party (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    party_kind          text not null check (party_kind in ('customer', 'supplier')),
    name                text not null,
    external_reference  text,

    status              text not null default 'active'
                            check (status in ('active', 'blocked')),

    created_at          timestamptz not null default now()
);

create index subledger_party_administration_idx
    on subledger_party(administration_id, party_kind);
create index subledger_party_organization_idx on subledger_party(organization_id);

-- ===========================================================================
-- Journal entries (FR-GL-004)
-- ===========================================================================
create table journal_entry (
    id                  uuid primary key default gen_random_uuid(),

    -- FR-GL-004: administration. Plus organization_id, because CLAUDE.md
    -- rule 1 requires every record to carry it and RLS reads it.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- FR-GL-004: fiscal year, period, journal.
    fiscal_year_id      uuid not null references fiscal_year(id),
    period_id           uuid not null references period(id),
    journal_id          uuid not null references ledger_journal(id),

    -- FR-GL-013: gapless per journal per year. Assigned by
    -- journal_entry_allocate_number(); a caller-supplied value is discarded.
    entry_number        bigint not null,

    -- FR-GL-004: date, description, document reference.
    entry_date          date not null,
    description         text not null check (length(btrim(description)) > 0),
    document_reference  text,

    -- FR-GL-004: actor, source system, timestamp.
    posted_by_user_id   uuid references users(id),
    source_system       text not null check (length(btrim(source_system)) > 0),
    posted_at           timestamptz not null default now(),

    -- FR-GL-003: the correction mechanism. A reversal points at what it
    -- reverses; the original is never touched, so there is deliberately no
    -- reverse pointer on the original - a back-reference would require an
    -- UPDATE to a committed row, which is exactly what this table forbids.
    -- "Has this been reversed" is a lookup on this column's index.
    reverses_entry_id   uuid references journal_entry(id),

    -- NFR-032. A retry with the same key returns the original entry instead
    -- of posting a second one.
    idempotency_key     text,

    -- The seal. Set from the server's transaction id at insert; compared on
    -- every line insert. See journal_line_validate().
    created_xid         xid8 not null default pg_current_xact_id(),

    constraint journal_entry_number_unique
        unique (journal_id, fiscal_year_id, entry_number),
    constraint journal_entry_number_positive check (entry_number >= 1)
);

create index journal_entry_administration_idx
    on journal_entry(administration_id, entry_date desc);
create index journal_entry_organization_idx on journal_entry(organization_id);
create index journal_entry_period_idx on journal_entry(period_id);
create index journal_entry_journal_year_idx
    on journal_entry(journal_id, fiscal_year_id, entry_number);
create index journal_entry_document_idx
    on journal_entry(administration_id, document_reference)
    where document_reference is not null;

-- FR-GL-003: an entry may be reversed at most once. A second reversal would
-- double the correction and leave the ledger overstated in the opposite
-- direction.
create unique index journal_entry_reverses_once_idx
    on journal_entry(reverses_entry_id)
    where reverses_entry_id is not null;

-- NFR-032.
create unique index journal_entry_idempotency_idx
    on journal_entry(administration_id, idempotency_key)
    where idempotency_key is not null;

-- ===========================================================================
-- Journal lines (the postings themselves)
-- ===========================================================================
create table journal_line (
    id                  uuid primary key default gen_random_uuid(),
    journal_entry_id    uuid not null references journal_entry(id),

    -- Denormalised from the entry by the validating trigger, never supplied.
    -- RLS needs a tenant predicate on THIS table; joining to the parent to
    -- find one would make the policy depend on another table's policy.
    organization_id     uuid not null,
    administration_id   uuid not null,

    line_number         smallint not null check (line_number >= 1),
    account_id          uuid not null references ledger_account(id),

    -- NFR-031: decimal, scale 2, the functional currency's scale. There is no
    -- float anywhere in this table and no float in any function that touches
    -- it. FR-GL-010 (multi-currency, S/P3) will ADD transaction-currency
    -- columns beside these rather than widening them.
    debit               numeric(19, 2) not null default 0,
    credit              numeric(19, 2) not null default 0,

    -- FR-GL-006: required on a control account, forbidden anywhere else. The
    -- biconditional is what makes "cannot be posted to directly" true, and it
    -- is what makes the sub-ledger reconcile by construction.
    subledger_party_id  uuid references subledger_party(id),

    -- FR-GL-009 (cost centres, S) is not built; the column is here because
    -- adding it later to an append-only table with history is a backfill
    -- nobody can perform - there is no UPDATE.
    cost_centre_id      uuid,

    description         text,

    -- Exactly one side carries an amount, and it is positive. Both zero is a
    -- posting of nothing; both non-zero is a line whose direction is
    -- undefined. Neither is representable.
    constraint journal_line_one_sided check (
        debit >= 0 and credit >= 0 and (debit = 0) <> (credit = 0)
    ),
    constraint journal_line_number_unique unique (journal_entry_id, line_number)
);

create index journal_line_entry_idx on journal_line(journal_entry_id);
create index journal_line_account_idx on journal_line(account_id);
create index journal_line_administration_idx on journal_line(administration_id);
create index journal_line_party_idx
    on journal_line(subledger_party_id)
    where subledger_party_id is not null;

-- ===========================================================================
-- Numbering (FR-GL-013)
-- ===========================================================================
-- A table, not a SEQUENCE. Postgres sequences are deliberately
-- non-transactional: a rolled-back transaction keeps its consumed value and
-- leaves a hole. That behaviour is correct for surrogate keys and disqualifying
-- for a statutory numbering series, where CMP-009 requires gaplessness
-- "sufficient to satisfy inspection standards".
--
-- The cost is real and worth naming: the UPDATE below takes a row lock, so
-- concurrent postings to the same journal in the same year serialise. That is
-- the price of gaplessness. It is bounded per (journal, year), which is the
-- narrowest scope the requirement permits.
create table journal_sequence (
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),
    journal_id          uuid not null references ledger_journal(id),
    fiscal_year_id      uuid not null references fiscal_year(id),
    next_number         bigint not null default 1 check (next_number >= 1),
    primary key (journal_id, fiscal_year_id)
);

create index journal_sequence_administration_idx on journal_sequence(administration_id);

-- ===========================================================================
-- Trigger: allocate the number, derive the tenant, refuse a closed period
-- ===========================================================================
create or replace function journal_entry_validate() returns trigger as $$
declare
    v_period        period%rowtype;
    v_year          fiscal_year%rowtype;
    v_journal       ledger_journal%rowtype;
begin
    -- FR-GL-002: exactly one journal and one period, both belonging to this
    -- administration. A composite foreign key cannot express a constraint
    -- that spans three tables, so it is checked here.
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

    -- FR-GL-007. Derived from the period, never trusted: an entry whose
    -- fiscal_year_id disagreed with its period's would number itself into the
    -- wrong year's series.
    new.fiscal_year_id := v_period.fiscal_year_id;
    new.organization_id := v_period.organization_id;

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

    -- The entry date must fall inside the period it is posted to, or the
    -- period lock protects nothing: a January date could be posted into an
    -- open December.
    if new.entry_date < v_period.start_date or new.entry_date > v_period.end_date then
        raise exception
            'entry date % falls outside period % (% .. %)',
            new.entry_date, v_period.period_number,
            v_period.start_date, v_period.end_date;
    end if;

    -- FR-GL-013. Derive-don't-trust: a caller that could name its own number
    -- could overwrite a hole, duplicate a number in a different journal, or
    -- start a second series. The UNIQUE constraint would catch the last of
    -- those; this catches all three.
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

create trigger journal_entry_validate_trg
    before insert on journal_entry
    for each row execute function journal_entry_validate();

-- ===========================================================================
-- Trigger: validate each line, and seal the entry at commit
-- ===========================================================================
create or replace function journal_line_validate() returns trigger as $$
declare
    v_entry     journal_entry%rowtype;
    v_account   ledger_account%rowtype;
    v_party     subledger_party%rowtype;
begin
    select * into v_entry from journal_entry where id = new.journal_entry_id;
    if not found then
        raise exception 'entry % does not exist', new.journal_entry_id;
    end if;

    -- FR-GL-003, the half that immutability alone does not cover. Withholding
    -- UPDATE and DELETE stops a committed entry being CHANGED; it does nothing
    -- to stop a line being APPENDED to it tomorrow, which would unbalance it
    -- just as effectively. Comparing the parent's transaction id to the
    -- current one means lines can only be added by the transaction that
    -- created the header: an entry is sealed the instant it commits.
    if v_entry.created_xid <> pg_current_xact_id() then
        raise exception
            'entry % was committed in an earlier transaction and is sealed; '
            'corrections are made by reversing entry (FR-GL-003)',
            new.journal_entry_id;
    end if;

    new.organization_id := v_entry.organization_id;
    new.administration_id := v_entry.administration_id;

    select * into v_account from ledger_account where id = new.account_id;
    if not found then
        raise exception 'account % does not exist', new.account_id;
    end if;
    if v_account.administration_id <> v_entry.administration_id then
        raise exception
            'account % belongs to another administration', new.account_id;
    end if;

    -- FR-GL-005: blocked accounts keep their history and take no new postings.
    if v_account.status <> 'active' then
        raise exception
            'account % (%) is blocked and cannot be posted to (FR-GL-005)',
            v_account.code, v_account.id;
    end if;

    -- FR-GL-006, stated as a biconditional rather than a prohibition.
    --
    -- A control account may only be touched by a line that names the party
    -- the amount is owed by or to; that IS the sub-ledger entry, so the
    -- sub-ledger and the control account cannot disagree - they are the same
    -- rows. "Reconciled continuously" is then a property of the schema, not a
    -- job that runs and can fall behind.
    --
    -- The converse matters as much: a party on a non-control account would be
    -- a receivable recorded somewhere the control account does not see.
    if v_account.control_kind is not null and new.subledger_party_id is null then
        raise exception
            'account % is the % control account and cannot be posted to directly; '
            'the line must name a sub-ledger party (FR-GL-006)',
            v_account.code, v_account.control_kind;
    end if;
    if v_account.control_kind is null and new.subledger_party_id is not null then
        raise exception
            'account % is not a control account and cannot carry a sub-ledger '
            'party (FR-GL-006)', v_account.code;
    end if;

    if new.subledger_party_id is not null then
        select * into v_party from subledger_party where id = new.subledger_party_id;
        if not found then
            raise exception 'sub-ledger party % does not exist', new.subledger_party_id;
        end if;
        if v_party.administration_id <> v_entry.administration_id then
            raise exception
                'sub-ledger party % belongs to another administration',
                new.subledger_party_id;
        end if;
        if (v_account.control_kind = 'accounts_receivable'
                and v_party.party_kind <> 'customer')
           or (v_account.control_kind = 'accounts_payable'
                and v_party.party_kind <> 'supplier') then
            raise exception
                'a % party cannot be posted to the % control account (FR-GL-006)',
                v_party.party_kind, v_account.control_kind;
        end if;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger journal_line_validate_trg
    before insert on journal_line
    for each row execute function journal_line_validate();

-- ===========================================================================
-- FR-GL-001: debits equal credits, checked at COMMIT
-- ===========================================================================
-- A CONSTRAINT TRIGGER, DEFERRABLE INITIALLY DEFERRED. Deferral is not an
-- optimisation here, it is the only thing that makes the check possible: lines
-- arrive one INSERT at a time, so an immediate check would reject the first
-- line of every entry ever written for not balancing on its own.
--
-- Firing at commit means an unbalanced entry cannot survive the transaction
-- that created it. The requirement's wording - "no transaction may PERSIST
-- unless total debits equal total credits" - is satisfied literally: the
-- transaction aborts.
create or replace function journal_entry_assert_balanced() returns trigger as $$
declare
    v_lines  bigint;
    v_debit  numeric(19, 2);
    v_credit numeric(19, 2);
begin
    select count(*), coalesce(sum(debit), 0), coalesce(sum(credit), 0)
      into v_lines, v_debit, v_credit
      from journal_line
     where journal_entry_id = new.id;

    -- The line count is not decoration. SUM() over zero rows returns NULL, and
    -- NULL <> 0 evaluates to NULL, which is not TRUE - so an entry with no
    -- lines at all would pass the balance test below without this. A header
    -- with no postings is the one unbalanced state a naive check waves through.
    if v_lines = 0 then
        raise exception
            'entry % has no lines; a journal entry with nothing posted to it is '
            'not a balanced entry (FR-GL-001)', new.id;
    end if;

    -- Implied by the one-sided CHECK and a zero sum, but asserted so the error
    -- says what is wrong rather than surfacing as an imbalance.
    if v_lines < 2 then
        raise exception
            'entry % has a single line; double-entry requires at least two '
            '(FR-GL-001)', new.id;
    end if;

    -- coalesce is redundant given the count check above and kept anyway: this
    -- is the assertion the whole ledger rests on, and it should not depend on
    -- a check three statements earlier still being there.
    if v_debit <> v_credit then
        raise exception
            'entry % is unbalanced: debits % <> credits % (FR-GL-001)',
            new.id, v_debit, v_credit;
    end if;

    return null;
end;
$$ language plpgsql;

create constraint trigger journal_entry_balanced_trg
    after insert on journal_entry
    deferrable initially deferred
    for each row execute function journal_entry_assert_balanced();

-- ===========================================================================
-- FR-GL-003: a reversal must actually reverse
-- ===========================================================================
-- Also deferred, because the reversal's lines do not exist yet when its header
-- is inserted.
--
-- Checking this structurally rather than trusting the caller is the point: a
-- "reversal" that quietly differs from the original - a changed amount, a
-- substituted account, a dropped line - is precisely the manipulation
-- FR-GL-003 exists to prevent, and it is invisible in a UI that shows a
-- correction was made.
--
-- Comparison is by (account, party) group rather than line-for-line, so a
-- reversal may consolidate two lines to the same account into one. The totals
-- per account must mirror exactly; the line layout need not.
create or replace function journal_entry_assert_reverses() returns trigger as $$
declare
    v_original journal_entry%rowtype;
    v_mismatch text;
begin
    if new.reverses_entry_id is null then
        return null;
    end if;

    select * into v_original from journal_entry where id = new.reverses_entry_id;
    if not found then
        raise exception 'entry % does not exist and cannot be reversed',
            new.reverses_entry_id;
    end if;
    if v_original.administration_id <> new.administration_id then
        raise exception 'a reversal must belong to the same administration';
    end if;
    if v_original.id = new.id then
        raise exception 'an entry cannot reverse itself';
    end if;

    select string_agg(
               format('account %s: original D%s/C%s, reversal D%s/C%s',
                      coalesce(o.account_id, r.account_id),
                      coalesce(o.d, 0), coalesce(o.c, 0),
                      coalesce(r.d, 0), coalesce(r.c, 0)),
               '; ')
      into v_mismatch
      from (
            select account_id, subledger_party_id,
                   sum(debit) as d, sum(credit) as c
              from journal_line
             where journal_entry_id = new.reverses_entry_id
             group by account_id, subledger_party_id
           ) o
      full outer join (
            select account_id, subledger_party_id,
                   sum(debit) as d, sum(credit) as c
              from journal_line
             where journal_entry_id = new.id
             group by account_id, subledger_party_id
           ) r
        on r.account_id = o.account_id
       and r.subledger_party_id is not distinct from o.subledger_party_id
     where o.account_id is null            -- reversal touches an account the original did not
        or r.account_id is null            -- reversal misses an account the original had
        or r.d <> o.c                      -- debit does not mirror the credit
        or r.c <> o.d;

    if v_mismatch is not null then
        raise exception
            'entry % does not mirror the entry it reverses (FR-GL-003): %',
            new.id, v_mismatch;
    end if;

    return null;
end;
$$ language plpgsql;

create constraint trigger journal_entry_reverses_trg
    after insert on journal_entry
    deferrable initially deferred
    for each row execute function journal_entry_assert_reverses();

-- ===========================================================================
-- FR-GL-003 / CMP-009: immutability
-- ===========================================================================
-- Unconditional, with no branch and no exemption, for the same reason 0019
-- gives: a function whose only statement is RAISE cannot be talked into
-- permitting anything.
--
-- These fire for ledgr_app, for ledgr_migrator, for ledgr_ops with BYPASSRLS,
-- for the ledger's own definer functions, and for a superuser at a psql
-- prompt alike. Postgres runs BEFORE triggers for every writer.
--
-- CMP-009 says deletion must be impossible "through any interface, including
-- support tooling". Support tooling is ledgr_ops, and this is the layer that
-- binds it: BYPASSRLS skips row-level security and has never skipped a trigger.
create or replace function journal_posting_immutable() returns trigger as $$
begin
    raise exception
        'the ledger is append-only (FR-GL-003, CMP-009): % cannot be % once '
        'committed. Corrections are made by reversing entry.',
        tg_table_name, lower(tg_op);
end;
$$ language plpgsql;

create trigger journal_entry_no_update_trg
    before update on journal_entry
    for each row execute function journal_posting_immutable();

create trigger journal_entry_no_delete_trg
    before delete on journal_entry
    for each row execute function journal_posting_immutable();

create trigger journal_line_no_update_trg
    before update on journal_line
    for each row execute function journal_posting_immutable();

create trigger journal_line_no_delete_trg
    before delete on journal_line
    for each row execute function journal_posting_immutable();

-- TRUNCATE is neither UPDATE nor DELETE and fires neither trigger above. It is
-- a statement-level event and needs its own. TRUNCATE administration CASCADE
-- would otherwise reach these tables without naming them.
create trigger journal_entry_no_truncate_trg
    before truncate on journal_entry
    for each statement execute function journal_posting_immutable();

create trigger journal_line_no_truncate_trg
    before truncate on journal_line
    for each statement execute function journal_posting_immutable();

-- ===========================================================================
-- FR-GL-007: period lock transitions
-- ===========================================================================
-- The unlock AUTHORITY is the authorization library (a `period` permission
-- evaluated per request). This trigger is the other half: which transitions
-- exist at all, which no permission can widen.
--
-- vat_filed is terminal. Once a period's figures have been filed with the
-- Belastingdienst, reopening it would let the filed return and the ledger
-- diverge silently. The route back is a suppletie - a new filing, not an
-- edit - which is a separate flow and not this migration.
create or replace function period_status_transition() returns trigger as $$
begin
    if new.status = old.status then
        return new;
    end if;

    if old.status = 'vat_filed' then
        raise exception
            'period % is VAT-filed and hard-locked; correcting it requires a '
            'suppletie, not an unlock (FR-GL-007)', old.period_number;
    end if;

    if not (
        (old.status = 'open'   and new.status in ('locked', 'vat_filed')) or
        (old.status = 'locked' and new.status in ('open', 'vat_filed'))
    ) then
        raise exception 'unsupported period transition % -> % (FR-GL-007)',
            old.status, new.status;
    end if;

    if new.status = 'locked' then
        new.locked_at := now();
    elsif new.status = 'open' then
        new.locked_at := null;
        new.locked_by_user_id := null;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger period_status_transition_trg
    before update on period
    for each row execute function period_status_transition();

-- ===========================================================================
-- The narrow API (`ledger` schema)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- ledger.post_entry - the only way a posting is ever written
-- ---------------------------------------------------------------------------
-- Everything a trigger can check is checked by a trigger, so that it holds for
-- any writer rather than only for callers of this function. What is left here
-- is what triggers cannot do: parse the line payload, enforce the decimal
-- rule at the boundary, and short-circuit an idempotent retry.
--
-- p_lines is a jsonb array of objects:
--   {"account_id", "debit", "credit", "subledger_party_id",
--    "cost_centre_id", "description"}
--
-- NFR-031: debit and credit MUST be JSON strings, never JSON numbers.
-- Postgres stores a jsonb number as numeric, so the value would survive the
-- database intact - the risk is entirely on the way in. A Python float that
-- has already lost precision (4335.09 + 2964.61 = 7299.700000000001)
-- serialises to a JSON number and would be accepted silently. Requiring a
-- string means json.dumps() of a float produces a number and is REJECTED,
-- while a Decimal must be str()'d deliberately. The rule turns a silent
-- precision loss into a loud error at the only place it can still be caught.
create or replace function ledger.post_entry(
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
    p_idempotency_key    text default null
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
    -- NFR-032. A retried request returns what the first one wrote. Checked
    -- before anything is allocated, so a retry does not consume a number.
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
        posted_by_user_id, source_system, reverses_entry_id, idempotency_key
    )
    values (
        -- organization_id, fiscal_year_id and entry_number are all overwritten
        -- by journal_entry_validate(); the placeholders exist only because the
        -- columns are NOT NULL. Deriving them there rather than here means a
        -- second write path could not get them wrong either.
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
        p_idempotency_key
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

    -- The balance is NOT checked here. journal_entry_balanced_trg checks it at
    -- COMMIT, which covers this function and anything that ever writes beside
    -- it. Checking here as well would give a nicer error and a false sense of
    -- where the guarantee lives.
    return v_entry;
exception
    when unique_violation then
        -- Two concurrent retries of the same idempotency key: one inserted,
        -- one lost the race on journal_entry_idempotency_idx. Both must return
        -- the same entry, so re-read rather than fail. Any other unique
        -- violation is a real error and is re-raised.
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

-- ---------------------------------------------------------------------------
-- ledger.reverse_entry - FR-GL-003's correction mechanism
-- ---------------------------------------------------------------------------
-- Builds the mirror server-side from the original's own lines rather than
-- accepting one from the caller. journal_entry_reverses_trg would reject a
-- wrong mirror either way, but a caller that cannot supply the lines cannot
-- get them wrong in the first place - and the difference matters when the
-- caller is a support tool operated under time pressure.
--
-- The reversal goes into a period the caller names, which will usually not be
-- the original's: the original's period is very often locked by the time an
-- error is found, and posting the correction into the current open period is
-- the correct accounting treatment, not a workaround.
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

    -- Checked here for a clear error; journal_entry_reverses_once_idx is what
    -- actually guarantees it, including against a concurrent second reversal.
    if exists (select 1 from journal_entry where reverses_entry_id = p_entry_id) then
        raise exception
            'entry % has already been reversed (FR-GL-003)', p_entry_id;
    end if;

    -- debit and credit swap. Amounts are rendered with to_char to guarantee a
    -- plain decimal string: NFR-031 forbids a float anywhere in this path, and
    -- post_entry rejects a JSON number, so the mirror must arrive as text.
    -- FM strips to_char's leading alignment space; the 0D00 pattern keeps a
    -- leading zero and exactly two decimals.
    select jsonb_agg(
               jsonb_build_object(
                   'account_id', l.account_id,
                   'debit', to_char(l.credit, 'FM9999999999999999990D00'),
                   'credit', to_char(l.debit, 'FM9999999999999999990D00'),
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
        p_idempotency_key    => p_idempotency_key
    );
end;
$$;

-- ---------------------------------------------------------------------------
-- Master data, also behind the API
-- ---------------------------------------------------------------------------
-- The chart of accounts is not a posting table, but leaving it writable by
-- ledgr_app would hand back what withholding INSERT on journal_line took away:
-- application code could create an ordinary account, post to it freely, and
-- only then mark it as the AR control account. FR-GL-006 would hold for every
-- statement and be false for the administration.
--
-- ledger_account_identity_immutable_trg is the guard that closes that; routing
-- creation through here as well means the rule is visible at the boundary
-- rather than only in a trigger someone has to go looking for.
create or replace function ledger.create_account(
    p_administration_id uuid,
    p_code              text,
    p_name              text,
    p_account_type      text,
    p_rgs_code          text default null,
    p_default_vat_code  text default null,
    p_control_kind      text default null
)
returns ledger_account
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_org     uuid;
    v_account ledger_account%rowtype;
begin
    select organization_id into v_org
      from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    insert into ledger_account (
        organization_id, administration_id, code, name, account_type,
        rgs_code, default_vat_code, control_kind
    )
    values (
        v_org, p_administration_id, p_code, p_name, p_account_type,
        p_rgs_code, p_default_vat_code, p_control_kind
    )
    returning * into v_account;

    return v_account;
end;
$$;

create or replace function ledger.set_account_status(
    p_account_id uuid,
    p_status     text
)
returns ledger_account
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_account ledger_account%rowtype;
begin
    update ledger_account set status = p_status
     where id = p_account_id
    returning * into v_account;
    if not found then
        raise exception 'account % does not exist', p_account_id;
    end if;
    return v_account;
end;
$$;

create or replace function ledger.create_journal(
    p_administration_id uuid,
    p_code              text,
    p_name              text,
    p_journal_type      text
)
returns ledger_journal
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_org     uuid;
    v_journal ledger_journal%rowtype;
begin
    select organization_id into v_org
      from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    insert into ledger_journal
        (organization_id, administration_id, code, name, journal_type)
    values
        (v_org, p_administration_id, p_code, p_name, p_journal_type)
    returning * into v_journal;

    return v_journal;
end;
$$;

create or replace function ledger.set_journal_status(
    p_journal_id uuid,
    p_status     text
)
returns ledger_journal
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_journal ledger_journal%rowtype;
begin
    update ledger_journal set status = p_status
     where id = p_journal_id
    returning * into v_journal;
    if not found then
        raise exception 'journal % does not exist', p_journal_id;
    end if;
    return v_journal;
end;
$$;

create or replace function ledger.create_party(
    p_administration_id  uuid,
    p_party_kind         text,
    p_name               text,
    p_external_reference text default null
)
returns subledger_party
language plpgsql
security definer
set search_path = public, app, pg_temp
as $$
declare
    v_org   uuid;
    v_party subledger_party%rowtype;
begin
    select organization_id into v_org
      from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    insert into subledger_party
        (organization_id, administration_id, party_kind, name, external_reference)
    values
        (v_org, p_administration_id, p_party_kind, p_name, p_external_reference)
    returning * into v_party;

    return v_party;
end;
$$;

-- ---------------------------------------------------------------------------
-- FR-GL-013: gap detection reporting
-- ---------------------------------------------------------------------------
-- The allocator makes gaps impossible, so this report should never return a
-- row. It is built anyway because a report that can only ever say "no gaps" is
-- the check ON the allocator: if a future change - a partial restore, a
-- migration that copies entries, a second write path - ever puts a hole in a
-- series, this is what surfaces it. CMP-009 asks for gaplessness "sufficient
-- to satisfy inspection standards", and an inspector asks to see the report,
-- not the trigger.
create or replace function ledger.numbering_gaps(
    p_journal_id     uuid,
    p_fiscal_year_id uuid
)
returns table (missing_number bigint)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    -- coalesce(..., 0) matters: max() over an empty series returns NULL, and
    -- generate_series(1, NULL) yields no rows, which is the right answer but
    -- reached by accident. With the coalesce it is generate_series(1, 0),
    -- which is empty by definition rather than by NULL propagation.
    select g.n
      from generate_series(1, coalesce((
                select max(entry_number)
                  from journal_entry
                 where journal_id = p_journal_id
                   and fiscal_year_id = p_fiscal_year_id
            ), 0)) as g(n)
     where not exists (
        select 1 from journal_entry x
         where x.journal_id = p_journal_id
           and x.fiscal_year_id = p_fiscal_year_id
           and x.entry_number = g.n
     )
     order by g.n;
$$;

-- ---------------------------------------------------------------------------
-- FR-GL-001 as a report: the trial balance
-- ---------------------------------------------------------------------------
create or replace function ledger.trial_balance(
    p_administration_id uuid,
    p_fiscal_year_id    uuid
)
returns table (
    account_id    uuid,
    account_code  text,
    account_name  text,
    account_type  text,
    total_debit   numeric(19, 2),
    total_credit  numeric(19, 2),
    balance       numeric(19, 2)
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    -- The year filter lives INSIDE the left-joined subquery, not in the outer
    -- WHERE. Filtering outside drops any account whose only activity is in a
    -- different fiscal year from the report entirely, instead of showing it
    -- at zero - a trial balance with accounts silently missing is worse than
    -- one with empty rows, because nothing on the page says so.
    select a.id, a.code, a.name, a.account_type,
           coalesce(sum(l.debit), 0)::numeric(19, 2),
           coalesce(sum(l.credit), 0)::numeric(19, 2),
           (coalesce(sum(l.debit), 0) - coalesce(sum(l.credit), 0))::numeric(19, 2)
      from ledger_account a
      left join (
            select jl.account_id, jl.debit, jl.credit
              from journal_line jl
              join journal_entry je on je.id = jl.journal_entry_id
             where je.fiscal_year_id = p_fiscal_year_id
           ) l on l.account_id = a.id
     where a.administration_id = p_administration_id
     group by a.id, a.code, a.name, a.account_type
     order by a.code;
$$;

-- ---------------------------------------------------------------------------
-- FR-GL-006: the sub-ledger, derived rather than stored
-- ---------------------------------------------------------------------------
-- These ARE the control account's own lines, grouped by party. There is no
-- second set of numbers, so there is nothing to reconcile and nothing that can
-- drift. ledger.control_account_reconciliation() below exists to demonstrate
-- that continuously, which is what FR-GL-006 asks to be able to show.
create or replace function ledger.subledger_balance(
    p_administration_id uuid,
    p_control_kind      text
)
returns table (
    party_id      uuid,
    party_name    text,
    total_debit   numeric(19, 2),
    total_credit  numeric(19, 2),
    balance       numeric(19, 2)
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select p.id, p.name,
           coalesce(sum(l.debit), 0)::numeric(19, 2),
           coalesce(sum(l.credit), 0)::numeric(19, 2),
           (coalesce(sum(l.debit), 0) - coalesce(sum(l.credit), 0))::numeric(19, 2)
      from subledger_party p
      join journal_line l on l.subledger_party_id = p.id
      join ledger_account a on a.id = l.account_id
     where p.administration_id = p_administration_id
       and a.control_kind = p_control_kind
     group by p.id, p.name
     order by p.name;
$$;

create or replace function ledger.control_account_reconciliation(
    p_administration_id uuid
)
returns table (
    control_kind          text,
    control_account_code  text,
    control_balance       numeric(19, 2),
    subledger_balance     numeric(19, 2),
    difference            numeric(19, 2)
)
language sql
stable
security definer
set search_path = public, app, pg_temp
as $$
    select a.control_kind,
           a.code,
           coalesce(sum(l.debit) - sum(l.credit), 0)::numeric(19, 2),
           coalesce(sum(l.debit) filter (where l.subledger_party_id is not null)
                  - sum(l.credit) filter (where l.subledger_party_id is not null),
                    0)::numeric(19, 2),
           coalesce(sum(l.debit) filter (where l.subledger_party_id is null)
                  - sum(l.credit) filter (where l.subledger_party_id is null),
                    0)::numeric(19, 2)
      from ledger_account a
      left join journal_line l on l.account_id = a.id
     where a.administration_id = p_administration_id
       and a.control_kind is not null
     group by a.control_kind, a.code;
$$;

comment on function ledger.control_account_reconciliation(uuid) is
    'FR-GL-006. difference is always zero by construction: every line on a '
    'control account is required by journal_line_validate() to name a party, '
    'so the unattributed bucket cannot receive one. A non-zero row here means '
    'the trigger has been removed.';

-- ===========================================================================
-- Ownership and privileges - the bounded context, enforced
-- ===========================================================================
-- NOTE ON ORDERING: every GRANT and REVOKE below runs AFTER ownership has
-- moved to ledgr_ledger, and ledgr_migrator is not a member of that role. That
-- only works because migrations are applied as a superuser - which they must
-- be anyway, since ledgr_migrator is NOCREATEROLE and cannot execute the
-- CREATE ROLE at the top of this file. Run as ledgr_migrator instead, the
-- GRANTs would fail outright and the REVOKEs would degrade to a warning,
-- silently leaving PUBLIC with EXECUTE on the write functions.
--
-- 0019 relies on the same property for audit_log. Recorded here because it is
-- invisible in the file and would be a confusing failure to diagnose.
alter table ledger_account   owner to ledgr_ledger;
alter table ledger_journal   owner to ledgr_ledger;
alter table subledger_party  owner to ledgr_ledger;
alter table journal_entry    owner to ledgr_ledger;
alter table journal_line     owner to ledgr_ledger;
alter table journal_sequence owner to ledgr_ledger;

alter function journal_entry_validate()            owner to ledgr_ledger;
alter function journal_line_validate()             owner to ledgr_ledger;
alter function journal_entry_assert_balanced()     owner to ledgr_ledger;
alter function journal_entry_assert_reverses()     owner to ledgr_ledger;
alter function journal_posting_immutable()         owner to ledgr_ledger;
alter function ledger_account_identity_immutable() owner to ledgr_ledger;

-- The API functions MUST be owned by ledgr_ledger, not by whoever ran the
-- migration. SECURITY DEFINER runs a function as its OWNER, so left owned by
-- ledgr_migrator they would execute with ledgr_migrator's privileges - which
-- on these tables are none, since it neither owns them nor holds a grant.
-- Every write would fail with `permission denied for table journal_entry`.
--
-- It fails closed rather than open, which is the right direction, but it is
-- the line that makes the narrow API work at all.
alter function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text
) owner to ledgr_ledger;
alter function ledger.reverse_entry(uuid, uuid, date, text, uuid, text, text)
    owner to ledgr_ledger;
alter function ledger.create_account(uuid, text, text, text, text, text, text)
    owner to ledgr_ledger;
alter function ledger.set_account_status(uuid, text) owner to ledgr_ledger;
alter function ledger.create_journal(uuid, text, text, text) owner to ledgr_ledger;
alter function ledger.set_journal_status(uuid, text) owner to ledgr_ledger;
alter function ledger.create_party(uuid, text, text, text) owner to ledgr_ledger;
alter function ledger.numbering_gaps(uuid, uuid) owner to ledgr_ledger;
alter function ledger.trial_balance(uuid, uuid) owner to ledgr_ledger;
alter function ledger.subledger_balance(uuid, text) owner to ledgr_ledger;
alter function ledger.control_account_reconciliation(uuid) owner to ledgr_ledger;

-- period_status_transition() guards a table ledgr_migrator owns, so it stays
-- with ledgr_migrator: an owner that cannot drop the trigger on its own table
-- buys nothing. FR-GL-007 does not ask for protection from a migration - only
-- FR-GL-003 and CMP-009 do, and those tables are owned above.

revoke all on ledger_account, ledger_journal, subledger_party,
              journal_entry, journal_line, journal_sequence from public;

-- Belt and braces on top of journal_posting_immutable(). Postgres does not
-- privilege-check a table's owner, so this revoke is not what stops
-- ledgr_ledger - the triggers are. It stops the privilege being inherited or
-- granted onward by accident, and makes the intent unmissable in \dp.
--
-- INSERT is deliberately NOT revoked from ledgr_ledger: the definer functions
-- run as this role and it is the only role that may write a posting at all.
revoke update, delete, truncate on journal_entry, journal_line from ledgr_ledger;

-- =========================== THE LOAD-BEARING GRANT ========================
--
-- SELECT and nothing else. ledgr_app - the role every request runs as - holds
-- no INSERT, UPDATE, DELETE or TRUNCATE on any posting table. Application code
-- outside the ledger service cannot write a posting because it has no
-- privilege to attempt one, which is a stronger statement than any amount of
-- code review or module boundary.
--
-- tests/integration/test_ledger_bounded_context.py asserts each of the four
-- withheld statements fails for ledgr_app, so a future migration that grants
-- one back fails the build rather than quietly reopening the context.
grant select on ledger_account, ledger_journal, subledger_party,
                journal_entry, journal_line, journal_sequence to ledgr_app;

-- Support tooling reads and never writes (CMP-009).
grant select on ledger_account, ledger_journal, subledger_party,
                journal_entry, journal_line, journal_sequence to ledgr_ops;

grant usage on schema ledger to ledgr_app, ledgr_ops, ledgr_ledger;

-- Postgres grants EXECUTE on a new function to PUBLIC by default. Left as
-- shipped, that would hand ledgr_ops - and every future role - the write
-- functions, making CMP-009's "including support tooling" false for insertion
-- while it held for deletion. The default is revoked first and every caller
-- is then named explicitly, so the API's audience is the grant list below and
-- nothing else.
revoke all on all functions in schema ledger from public;

-- The API surface, in full. This list is the bounded context's public
-- interface; anything not on it cannot be reached from application code.
grant execute on function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text
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

-- ledgr_ops gets NO write function. Support tooling that could post would make
-- CMP-009's "including support tooling" false for insertion even while it
-- holds for deletion.

-- The definer functions read tenancy tables to validate what they are asked to
-- write. RLS still applies to ledgr_ledger (FORCE ROW LEVEL SECURITY below,
-- and 0001's on these tables), and the predicates read app.current_org_id(),
-- which is session state - so a definer function called by tenant A sees
-- exactly tenant A's rows. The elevation is over what may be written, never
-- over whose data is visible.
grant select on administration, firm_engagement, fiscal_year, period to ledgr_ledger;
grant usage on schema app to ledgr_ledger;

-- ===========================================================================
-- Row-level security
-- ===========================================================================
-- FORCE, so ledgr_ledger is bound by these policies on the tables it owns.
-- Without it Postgres exempts a table's owner, and the definer functions would
-- become a cross-tenant read path.
alter table ledger_account   enable row level security;
alter table ledger_account   force row level security;
alter table ledger_journal   enable row level security;
alter table ledger_journal   force row level security;
alter table subledger_party  enable row level security;
alter table subledger_party  force row level security;
alter table journal_entry    enable row level security;
alter table journal_entry    force row level security;
alter table journal_line     enable row level security;
alter table journal_line     force row level security;
alter table journal_sequence enable row level security;
alter table journal_sequence force row level security;

create policy ledger_account_select on ledger_account
    for select using (app.has_administration_access(administration_id));
create policy ledger_account_insert on ledger_account
    for insert with check (app.has_administration_access(administration_id));
create policy ledger_account_update on ledger_account
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy ledger_journal_select on ledger_journal
    for select using (app.has_administration_access(administration_id));
create policy ledger_journal_insert on ledger_journal
    for insert with check (app.has_administration_access(administration_id));
create policy ledger_journal_update on ledger_journal
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

create policy subledger_party_select on subledger_party
    for select using (app.has_administration_access(administration_id));
create policy subledger_party_insert on subledger_party
    for insert with check (app.has_administration_access(administration_id));

create policy journal_entry_select on journal_entry
    for select using (app.has_administration_access(administration_id));
create policy journal_entry_insert on journal_entry
    for insert with check (app.has_administration_access(administration_id));

create policy journal_line_select on journal_line
    for select using (app.has_administration_access(administration_id));
create policy journal_line_insert on journal_line
    for insert with check (app.has_administration_access(administration_id));

create policy journal_sequence_select on journal_sequence
    for select using (app.has_administration_access(administration_id));
create policy journal_sequence_all on journal_sequence
    for all using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- No UPDATE or DELETE policy on journal_entry or journal_line, and no grant
-- either. Three independent reasons a write fails, before the trigger is even
-- reached.

commit;

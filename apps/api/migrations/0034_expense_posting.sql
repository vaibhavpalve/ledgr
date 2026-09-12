-- 0034_expense_posting.sql
-- FR-EXP-001d, FR-EXP-001e (PRD §6.7), through the ledger's narrow API.
-- See docs/decisions/ADR-033-expense-posting.md.
--
--   FR-EXP-001d  The image is retained as the source document under §6.10,
--                LINKED BIDIRECTIONALLY TO THE RESULTING POSTING ...
--   FR-EXP-001e  Payment method is captured at entry ... BECAUSE IT DETERMINES
--                THE POSTING and cannot be reliably inferred later.
--
-- ===========================================================================
-- This migration writes no postings, and cannot
-- ===========================================================================
--
-- CLAUDE.md's first non-negotiable: "The ledger is a separate bounded context
-- with a narrow API. Nothing writes to posting tables except the ledger
-- service." Nothing here inserts into `journal_entry` or `journal_line`, and
-- `ledgr_app` still has no grant to - the entry is created by
-- `ledger.post_entry` through `api.ledger.service.LedgerService`, exactly as a
-- manual journal is.
--
-- What this adds is everything the ledger needs to be TOLD, and the link back:
--
--   expense_posting_account   which accounts an expense posts to
--   expense.journal_entry_id  the entry it produced, set once
--
-- ===========================================================================
-- Why the accounts are configuration rather than a guess
-- ===========================================================================
--
-- A posting needs account ids. An expense has a free-text `category`
-- (FR-EXP-001b), which is not one - and deriving an account from a category
-- name by matching strings against the chart of accounts is the kind of
-- cleverness that puts a lunch in "Loonheffingen" on a Friday afternoon.
--
-- So the mapping is DATA, per administration, and an unmapped category is a
-- refusal naming what is missing rather than a posting to whatever looked
-- closest. Configuration that has not been done is visible; a wrong guess is
-- not.
--
-- The alternative - having the client send account ids - is worse twice over:
-- it puts accounting knowledge in the client, which CLAUDE.md rules out, and
-- it lets a caller choose which account their lunch lands in.

begin;

-- ===========================================================================
-- expense_posting_account
-- ===========================================================================
create table expense_posting_account (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- What this account is FOR. The three payment methods are here because
    -- FR-EXP-001e says the method determines the posting: each credits
    -- something different, and that difference is the whole reason the field
    -- is captured at entry.
    purpose             text not null check (purpose in (
                            'expense_category',
                            'vat_input',
                            'business_account',
                            'business_card',
                            'reimbursement_liability'
                        )),

    -- The category this row maps, for purpose = 'expense_category'. NULL is
    -- the FALLBACK row: the account an unmapped category posts to. Having one
    -- is what keeps a new category from blocking a claim, while an
    -- administration that would rather refuse simply does not create it.
    category_key        text,

    account_id          uuid not null references ledger_account(id),

    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    -- Only 'expense_category' rows carry a key; the rest are one per purpose.
    constraint expense_posting_account_key_shape check (
        (purpose = 'expense_category') or (category_key is null)
    )
);

-- One account per purpose, and per category within 'expense_category'.
-- Two mappings for one purpose would make the posting depend on which row the
-- planner reached first.
create unique index expense_posting_account_purpose_idx
    on expense_posting_account(administration_id, purpose)
    where purpose <> 'expense_category';

create unique index expense_posting_account_category_idx
    on expense_posting_account(administration_id, lower(btrim(category_key)))
    where purpose = 'expense_category' and category_key is not null;

create unique index expense_posting_account_fallback_idx
    on expense_posting_account(administration_id)
    where purpose = 'expense_category' and category_key is null;

create index expense_posting_account_organization_idx
    on expense_posting_account(organization_id);

comment on table expense_posting_account is
    'FR-EXP-001e. Which ledger account an expense posts to, per purpose and '
    'per category. Configuration rather than a guess: an unmapped category is '
    'a refusal naming what is missing, not a posting to whatever looked close.';

-- An account from another administration's chart would post one client's
-- lunch into another client's books - the wrong-client failure FR-FRM-000a
-- calls the worst in this product, arriving through a configuration table.
create or replace function expense_posting_account_same_tenant() returns trigger as $$
declare
    v_account_admin uuid;
begin
    select administration_id into v_account_admin
      from ledger_account where id = new.account_id;

    if v_account_admin is distinct from new.administration_id then
        raise exception
            'a posting account belongs to the administration it maps: account is '
            'in %, mapping claims % (CLAUDE.md rule 1)',
            v_account_admin, new.administration_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger expense_posting_account_same_tenant_trg
    before insert or update on expense_posting_account
    for each row execute function expense_posting_account_same_tenant();

-- ===========================================================================
-- expense gains the entry it produced
-- ===========================================================================
alter table expense
    add column journal_entry_id uuid references journal_entry(id),
    add column posted_at        timestamptz;

-- One entry per expense and one expense per entry. Without this a retry could
-- produce a second posting for the same claim - the double-payment NFR-032
-- exists to prevent, arriving by a route idempotency keys do not cover because
-- the two requests are minutes apart.
create unique index expense_journal_entry_idx
    on expense(journal_entry_id) where journal_entry_id is not null;

comment on column expense.journal_entry_id is
    'FR-EXP-001d. The ledger entry this claim produced. Set once by '
    'api.expenses.posting through the ledger service - never written here.';

-- ---------------------------------------------------------------------------
-- `posted` joins the states
-- ---------------------------------------------------------------------------
alter table expense drop constraint expense_status_check;
alter table expense add constraint expense_status_check
    check (status in ('draft', 'ready', 'posted'));

-- A posted expense has an entry, and an expense with an entry is posted.
-- Neither half is redundant: the first stops a status that claims a posting
-- nobody made, the second stops an entry nobody can find from the claim.
alter table expense
    add constraint expense_posted_has_an_entry check (
        (status = 'posted') = (journal_entry_id is not null)
    );

alter table expense
    add constraint expense_posted_at_recorded check (
        (posted_at is null) = (journal_entry_id is null)
    );

-- `posted` is `ready` plus an entry, so it inherits the completeness rule.
-- Restated rather than widened: 0033's constraint names 'ready' specifically,
-- and a posted expense missing a payment method would be a posting whose own
-- shape nobody could explain.
alter table expense drop constraint expense_ready_is_complete;
alter table expense add constraint expense_ready_is_complete check (
    status not in ('ready', 'posted')
    or (
        expense_date is not null
        and supplier is not null and length(btrim(supplier)) > 0
        and gross_amount is not null
        and vat_treatment is not null
        and vat_rate is not null
        and vat_amount is not null
        and category is not null and length(btrim(category)) > 0
        and payment_method is not null
    )
);

-- ---------------------------------------------------------------------------
-- A posting, once made, is not unmade here
-- ---------------------------------------------------------------------------
-- FR-GL-003: corrections to the ledger are reversing entries, never mutation.
-- The same reasoning reaches the LINK: clearing `journal_entry_id` would leave
-- a posted entry with nothing pointing at it and an expense free to post
-- again, which is a double claim with no trace of the first.
create or replace function expense_posting_is_final() returns trigger as $$
begin
    if old.journal_entry_id is not null
       and new.journal_entry_id is distinct from old.journal_entry_id then
        raise exception
            'expense % is already posted as entry %; a posting is corrected by a '
            'reversing entry (FR-GL-003), never by repointing the claim',
            old.id, old.journal_entry_id;
    end if;

    if old.status = 'posted' and new.status <> 'posted' then
        raise exception
            'expense % is posted; it cannot return to %. The correction is a '
            'reversing entry (FR-GL-003).', old.id, new.status;
    end if;

    -- The figures behind a posted entry are what that entry was built from.
    -- Changing them afterwards would leave the ledger and the claim
    -- disagreeing about the same money.
    if old.status = 'posted' and (
        new.gross_amount   is distinct from old.gross_amount
        or new.vat_amount  is distinct from old.vat_amount
        or new.vat_rate    is distinct from old.vat_rate
        or new.vat_treatment is distinct from old.vat_treatment
        or new.expense_date  is distinct from old.expense_date
        or new.payment_method is distinct from old.payment_method
    ) then
        raise exception
            'expense % is posted; its amounts, date, VAT treatment and payment '
            'method are what entry % was built from and cannot change',
            old.id, old.journal_entry_id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger expense_posting_is_final_trg
    before update on expense
    for each row execute function expense_posting_is_final();

-- ===========================================================================
-- What a claim's posting looks like, for whoever has to check one
-- ===========================================================================
-- Reads the entry back through the link, so "does this claim's posting say
-- what the claim says" is one query rather than a join somebody assembles
-- differently each time.
create or replace function expenses.posting_of(p_expense_id uuid)
returns table (
    expense_id      uuid,
    journal_entry_id uuid,
    entry_number    bigint,
    entry_date      date,
    account_id      uuid,
    account_code    text,
    account_name    text,
    debit           numeric,
    credit          numeric,
    line_description text
)
language sql
stable
as $$
    select e.id, je.id, je.entry_number, je.entry_date,
           a.id, a.code, a.name, jl.debit, jl.credit, jl.description
      from expense e
      join journal_entry je on je.id = e.journal_entry_id
      join journal_line jl  on jl.journal_entry_id = je.id
      join ledger_account a on a.id = jl.account_id
     where e.id = p_expense_id
     order by jl.debit desc nulls last, a.code;
$$;

comment on function expenses.posting_of(uuid) is
    'FR-EXP-001d. The ledger lines a claim produced, as a person checking one '
    'would read them.';

-- ---------------------------------------------------------------------------
-- Claims that should have posted and did not
-- ---------------------------------------------------------------------------
-- The mirror of FR-DOC-003's completeness report, from the other side: a
-- `ready` claim with no entry is somebody waiting to be paid.
create or replace function expenses.unposted_claims(p_administration_id uuid)
returns table (
    expense_id     uuid,
    expense_date   date,
    supplier       text,
    gross_amount   numeric,
    payment_method text,
    waiting_since  timestamptz
)
language sql
stable
as $$
    select e.id, e.expense_date, e.supplier, e.gross_amount, e.payment_method,
           e.updated_at
      from expense e
     where e.administration_id = p_administration_id
       and e.status = 'ready'
     order by e.updated_at;
$$;

comment on function expenses.unposted_claims(uuid) is
    'FR-EXP-001e. Claims complete enough to post that have not been. A '
    'reimbursable one is somebody waiting for their money.';

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table expense_posting_account owner to ledgr_migrator;

-- No DELETE: a mapping that produced postings is the explanation for where
-- they went. Superseded by an UPDATE, following this schema's convention.
grant select, insert, update on expense_posting_account to ledgr_app;
grant select on expense_posting_account to ledgr_ops;

grant execute on function expenses.posting_of(uuid)      to ledgr_app, ledgr_ops;
grant execute on function expenses.unposted_claims(uuid) to ledgr_app, ledgr_ops;

-- `expenses.posting_of` reads journal_entry and journal_line. It is NOT
-- security definer, so it sees exactly what the calling request may see -
-- reading the ledger through RLS, never around it.

alter table expense_posting_account enable row level security;
alter table expense_posting_account force row level security;

create policy expense_posting_account_select on expense_posting_account
    for select using (app.has_administration_access(administration_id));
create policy expense_posting_account_insert on expense_posting_account
    for insert with check (app.has_administration_access(administration_id));
create policy expense_posting_account_update on expense_posting_account
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

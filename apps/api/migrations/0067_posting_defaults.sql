-- 0067_posting_defaults.sql
-- A new administration can post. See docs/decisions/ADR-086-posting-defaults-at-onboarding.md.
--
-- ===========================================================================
-- The defect this closes
-- ===========================================================================
--
-- `POST /v1/administrations` seeds the chart (FR-ONB-005) and opens the fiscal year
-- (FR-ONB-006), and stopped there. Every posting path then needs configuration nothing ever
-- wrote:
--
--   issuing a sales invoice     one active 'sales' journal, sales_posting_account (0040)
--   posting an expense          one active 'purchase' journal, expense_posting_account (0034)
--   reconciling a bank line     one active 'bank' journal
--   a manual posting            a 'memorial' journal
--
-- So a customer who signed up, onboarded and drafted an invoice was answered "This
-- administration has no single active sales journal" on the first issue - found by driving the
-- golden path over HTTP (scripts/golden_path.py), invisible to every test because each test
-- seeds its own journals and mappings.
--
-- ===========================================================================
-- What this adds
-- ===========================================================================
--
-- app.ensure_posting_defaults(administration_id): the journals and mappings a seeded chart
-- implies, derived from the chart's own accounts (by account code first, then by RGS code, so a
-- chart the owner has renamed still resolves). It only ever FILLS GAPS: an existing journal of a
-- type, or an existing mapping for a purpose/treatment/category, is left exactly as it is. Running
-- it twice is a no-op, which is what lets onboarding call it and this migration call it for every
-- administration that already exists.
--
-- SECURITY INVOKER on purpose. Called by the API as ledgr_app it runs under the caller's tenant
-- context and RLS, like any other insert the app makes - it cannot reach an administration the
-- caller cannot. Journals go through ledger.create_journal (the ledger's narrow API, non-negotiable
-- #1); the two mapping tables are ordinary configuration the app already inserts into.
--
-- The expense-category labels below are `api.expenses.categories.EXPENSE_CATEGORIES`' labels.
-- tests/integration/test_posting_defaults.py fails if a category is added there and not here.

begin;

create or replace function app.ensure_posting_defaults(p_administration_id uuid)
returns void
language plpgsql
security invoker
set search_path = public, app, pg_temp
as $$
declare
    v_org          uuid;
    v_legal_form   text;
    v_revenue      uuid;
    v_vat_output   uuid;
    v_vat_input    uuid;
    v_bank         uuid;
    v_reimburse    uuid;
    v_general      uuid;
    v_account      uuid;
    v_row          record;
begin
    select organization_id, legal_form into v_org, v_legal_form
      from administration where id = p_administration_id;
    if not found then
        raise exception 'administration % does not exist', p_administration_id;
    end if;

    -- Nothing to map onto before the chart is seeded; onboarding calls this after the seed.
    if not exists (select 1 from ledger_account where administration_id = p_administration_id) then
        return;
    end if;

    -- ------------------------------------------------------------------
    -- Journals: one of each type the posting paths look for, unless one exists.
    -- ------------------------------------------------------------------
    for v_row in
        select * from (values
            ('sales',    'VK',  'Verkoopboek'),
            ('purchase', 'IK',  'Inkoopboek'),
            ('bank',     'BNK', 'Bankboek'),
            ('cash',     'KAS', 'Kasboek'),
            ('memorial', 'MEM', 'Memoriaal')
        ) as j(journal_type, code, name)
    loop
        if not exists (
            select 1 from ledger_journal
             where administration_id = p_administration_id
               and journal_type = v_row.journal_type
               and status = 'active'
        ) and not exists (
            select 1 from ledger_journal
             where administration_id = p_administration_id and code = v_row.code
        ) then
            perform ledger.create_journal(
                p_administration_id, v_row.code, v_row.name, v_row.journal_type
            );
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- Accounts, by code then by RGS reference code.
    -- ------------------------------------------------------------------
    select id into v_revenue from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and account_type = 'revenue'
     order by (code = '8000') desc, (default_vat_code = 'btw_21') desc, code
     limit 1;

    select id into v_vat_output from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '1700' or rgs_code = 'BSchObe')
     order by (code = '1700') desc, code
     limit 1;

    select id into v_vat_input from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '1720' or rgs_code = 'BVorObe')
     order by (code = '1720') desc, code
     limit 1;

    select id into v_bank from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '1100' or rgs_code = 'BLimBan')
     order by (code = '1100') desc, code
     limit 1;

    -- Money the business owes the person who paid out of their own pocket: the director's
    -- current account for a BV, a private contribution for a sole trader, other payables otherwise.
    select id into v_reimburse from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '1500'
            or (code = '0560' and v_legal_form = 'eenmanszaak')
            or code = '1790')
     order by case code when '1500' then 0 when '0560' then 1 else 2 end
     limit 1;

    select id into v_general from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and account_type = 'expense'
     order by (code = '4400') desc, (rgs_code = 'WBedAlg') desc, code
     limit 1;

    -- ------------------------------------------------------------------
    -- Sales: revenue per treatment from each revenue account's own default VAT code, a revenue
    -- fallback, and one output-VAT fallback.
    -- ------------------------------------------------------------------
    for v_row in
        select distinct on (default_vat_code) default_vat_code, id
          from ledger_account
         where administration_id = p_administration_id and status = 'active'
           and account_type = 'revenue' and default_vat_code is not null
           and default_vat_code in (select code from vat_treatment)
         order by default_vat_code, code
    loop
        insert into sales_posting_account
            (organization_id, administration_id, purpose, vat_treatment_key, account_id)
        select v_org, p_administration_id, 'revenue', v_row.default_vat_code, v_row.id
         where not exists (
            select 1 from sales_posting_account
             where administration_id = p_administration_id and purpose = 'revenue'
               and vat_treatment_key = v_row.default_vat_code
         );
    end loop;

    if v_revenue is not null then
        insert into sales_posting_account
            (organization_id, administration_id, purpose, vat_treatment_key, account_id)
        select v_org, p_administration_id, 'revenue', null, v_revenue
         where not exists (
            select 1 from sales_posting_account
             where administration_id = p_administration_id and purpose = 'revenue'
               and vat_treatment_key is null
         );
    end if;

    if v_vat_output is not null then
        insert into sales_posting_account
            (organization_id, administration_id, purpose, vat_treatment_key, account_id)
        select v_org, p_administration_id, 'vat_output', null, v_vat_output
         where not exists (
            select 1 from sales_posting_account
             where administration_id = p_administration_id and purpose = 'vat_output'
               and vat_treatment_key is null
         );
    end if;

    -- ------------------------------------------------------------------
    -- Expenses: input VAT, the three funding purposes, each category and a fallback.
    -- ------------------------------------------------------------------
    for v_row in
        select * from (values
            ('vat_input',               v_vat_input),
            ('business_account',        v_bank),
            ('business_card',           v_bank),
            ('reimbursement_liability', v_reimburse)
        ) as p(purpose, account_id)
    loop
        if v_row.account_id is not null then
            insert into expense_posting_account
                (organization_id, administration_id, purpose, category_key, account_id)
            select v_org, p_administration_id, v_row.purpose, null, v_row.account_id
             where not exists (
                select 1 from expense_posting_account
                 where administration_id = p_administration_id and purpose = v_row.purpose
             );
        end if;
    end loop;

    for v_row in
        select * from (values
            ('Inventory & stock',        array['7000']),
            ('Car & transport',          array['4300']),
            ('Travel & lodging',         array['4200']),
            ('Lunch',                    array['4200']),
            ('Dining out',               array['4200']),
            ('Entertainment & gifts',    array['4200']),
            ('Office supplies',          array['4100']),
            ('Rent & premises',          array['4000']),
            ('Utilities',                array['4000']),
            ('Phone & internet',         array['4100']),
            ('Marketing & ads',          array['4200']),
            ('Software & subscriptions', array['4100']),
            ('Insurance',                array['4400']),
            ('Professional services',    array['4400']),
            ('Training & education',     array['4400']),
            ('Staff costs',              array['4600', '4400']),
            ('Bank & interest',          array['4900', '4400']),
            ('Capital asset',            array['0250', '0200']),
            ('Other',                    array['4400'])
        ) as c(label, codes)
    loop
        select a.id into v_account
          from unnest(v_row.codes) with ordinality as wanted(code, rank)
          join ledger_account a
            on a.administration_id = p_administration_id
           and a.code = wanted.code
           and a.status = 'active'
         order by wanted.rank
         limit 1;
        v_account := coalesce(v_account, v_general);
        if v_account is not null then
            insert into expense_posting_account
                (organization_id, administration_id, purpose, category_key, account_id)
            select v_org, p_administration_id, 'expense_category', v_row.label, v_account
             where not exists (
                select 1 from expense_posting_account
                 where administration_id = p_administration_id
                   and purpose = 'expense_category'
                   and lower(btrim(category_key)) = lower(btrim(v_row.label))
             );
        end if;
    end loop;

    if v_general is not null then
        insert into expense_posting_account
            (organization_id, administration_id, purpose, category_key, account_id)
        select v_org, p_administration_id, 'expense_category', null, v_general
         where not exists (
            select 1 from expense_posting_account
             where administration_id = p_administration_id
               and purpose = 'expense_category' and category_key is null
         );
    end if;
end;
$$;

comment on function app.ensure_posting_defaults(uuid) is
    'ADR-086. The journals and posting-account mappings a seeded chart implies. Fills gaps only; '
    'never changes an existing journal or mapping. Idempotent.';

revoke all on function app.ensure_posting_defaults(uuid) from public;
grant execute on function app.ensure_posting_defaults(uuid) to ledgr_app;

-- Every administration that already exists and has a chart gets the same defaults. The migration
-- runs as a superuser, so RLS does not narrow this to one tenant; the function itself scopes every
-- statement to the one administration it is given.
do $$
declare
    v_id uuid;
begin
    for v_id in
        select distinct administration_id from ledger_account
    loop
        perform app.ensure_posting_defaults(v_id);
    end loop;
end;
$$;

commit;

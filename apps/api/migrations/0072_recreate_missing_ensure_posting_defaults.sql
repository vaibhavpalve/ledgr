-- 0072_recreate_missing_ensure_posting_defaults.sql
-- app.ensure_posting_defaults did not exist in production, despite being defined in 0067 and
-- redefined in 0070. See docs/decisions/ADR-100-recreate-missing-ensure-posting-defaults.md for
-- how this was found and why a repair migration is the fix rather than a code change.
--
-- ===========================================================================
-- What this is, and is not
-- ===========================================================================
--
-- This is 0070's own `create or replace function app.ensure_posting_defaults(...)` body,
-- character-for-character, plus the same comment. Nothing here is new logic - it exists so that
-- whatever gap in this environment's migration history left the function missing is closed by
-- something the tracked runner (ADR-064) WILL apply, rather than by hand against production.
--
-- It also calls the function once for every administration that already has a seeded chart -
-- every administration onboarded while the function was missing never had this run for it at all,
-- not only the one that surfaced the gap. The function is idempotent (fills a gap only, never
-- changes an existing mapping), so calling it for an administration that happens to be fine
-- already is a no-op.
--
-- Tenancy: no schema change to any tenant-scoped table. Ledger: this function does not post
-- entries - it only creates journals (via ledger.create_journal, itself the ledger's own public
-- entry point) and posting-account mappings.
--
-- NFR-044: backward compatible (recreates a function, touches no table shape), no downtime.
-- Reversible: `drop function if exists app.ensure_posting_defaults(uuid);` - though that would
-- restore the exact gap this migration exists to close.

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
    v_transit      uuid;
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

    -- ADR-092: a receipt paid from the business account waits in Kruisposten until its bank line
    -- arrives and is matched, so the bank account itself only ever moves with the statement.
    select id into v_transit from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '2000' or rgs_code = 'BLimKrp')
     order by (code = '2000') desc, code
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
            ('business_account',        coalesce(v_transit, v_bank)),
            ('business_card',           coalesce(v_transit, v_bank)),
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
    'ADR-086, ADR-092, ADR-100. The journals and posting-account mappings a seeded chart implies; '
    'bank-paid receipts settle through Kruisposten. Fills gaps only; never changes an existing '
    'journal or mapping. Idempotent.';

-- Backfill: every administration that already has a seeded chart never had this function to run
-- at all. Idempotent per-call, so this is safe to run for one that already happens to be fine.
do $$
declare
    v_admin uuid;
begin
    for v_admin in select id from administration
    loop
        perform app.ensure_posting_defaults(v_admin);
    end loop;
end;
$$;

commit;

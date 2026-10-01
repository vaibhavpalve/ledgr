-- 0075_fix_posting_defaults_backfill_tenant_context.sql
-- 0072's own backfill loop could never succeed for any administration actually missing a
-- journal, in any environment - not only when applied by hand, but also had migrate.py's
-- automated preDeployCommand ever actually run it (see the ledgr.railway.ts / dashboard
-- Pre-Deploy Command gap found and fixed alongside this). This is why the earlier manual
-- catch-up run of 0072 against production rolled back entirely, taking the function
-- definition down with it even though app.schema_migrations was left recording it as applied.
--
-- ===========================================================================
-- Root cause
-- ===========================================================================
--
-- app.ensure_posting_defaults calls ledger.create_journal, a SECURITY DEFINER function owned
-- by ledgr_migrator. `administration` has FORCE ROW LEVEL SECURITY (0001), so even though
-- ledgr_migrator owns the table, RLS still applies to it inside that call - and
-- administration_select's policy is entirely keyed on app.current_org_id(), which reads the
-- session GUC `app.current_org_id`. A real request sets that GUC per IAM-001-005 (rule #1); an
-- ad hoc migration connection - migrate.py's included - never does, so
-- `select organization_id into v_org from administration where id = p_administration_id`
-- inside ledger.create_journal sees zero rows for every administration, not just one, and
-- raises 'administration % does not exist' regardless of who is actually connected, including a
-- superuser: SECURITY DEFINER re-evaluates RLS as the function's owner, not the caller.
--
-- This was invisible in this migration's own local validation because every locally seeded
-- test administration already had a full set of journals, so the backfill's
-- `if not exists (...) then perform create_journal(...)` branch was never actually taken - the
-- gap only shows up for an administration genuinely missing a journal, which is exactly the
-- population this backfill exists to fix.
--
-- ===========================================================================
-- The fix
-- ===========================================================================
--
-- Recreates app.ensure_posting_defaults (identical body - it was never really the function
-- itself at fault) and redoes the backfill, this time setting app.current_org_id to each
-- administration's own organization_id, transaction-locally (`set_config(..., true)`, reverting
-- automatically at this migration's own commit), before calling it - the same tenant context a
-- real request would have carried for that administration.
--
-- Tenancy: sets a session GUC to run each administration's OWN data through its OWN existing
-- RLS policies exactly as a real request for that tenant would - grants no cross-tenant access.
-- Ledger: unchanged from 0072 - still only ever creates journals/mappings via
-- ledger.create_journal, the ledger's own public entry point.
--
-- NFR-044: backward compatible, no downtime. Reversible: as 0072 - drop function if exists
-- app.ensure_posting_defaults(uuid);

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

    if not exists (select 1 from ledger_account where administration_id = p_administration_id) then
        return;
    end if;

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

    select id into v_transit from ledger_account
     where administration_id = p_administration_id and status = 'active'
       and (code = '2000' or rgs_code = 'BLimKrp')
     order by (code = '2000') desc, code
     limit 1;

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
    'ADR-086, ADR-092, ADR-100, ADR-102. The journals and posting-account mappings a seeded '
    'chart implies; bank-paid receipts settle through Kruisposten. Fills gaps only; never '
    'changes an existing journal or mapping. Idempotent.';

-- Backfill, corrected: sets this administration's own organization_id as the session's tenant
-- context (transaction-local - reverts at this migration's own commit) before calling into the
-- SECURITY DEFINER chain, so FORCE ROW LEVEL SECURITY sees the same tenant a real request for
-- this administration would have carried, instead of none at all.
do $$
declare
    v_admin record;
begin
    for v_admin in select id, organization_id from administration
    loop
        perform set_config('app.current_org_id', v_admin.organization_id::text, true);
        perform app.ensure_posting_defaults(v_admin.id);
    end loop;
end;
$$;

commit;

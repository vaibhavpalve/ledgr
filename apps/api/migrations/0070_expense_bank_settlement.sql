-- 0070_expense_bank_settlement.sql
-- A receipt paid from the bank is booked once, not twice. See
-- docs/decisions/ADR-092-bank-paid-expenses-settle-through-kruisposten.md.
--
-- ===========================================================================
-- The defect this closes
-- ===========================================================================
--
-- 0067 mapped the 'business_account' and 'business_card' payment methods straight onto the bank
-- account (1100). Posting such a receipt credited the bank on the receipt's date. When the bank
-- statement was imported, the same payment arrived again as an outgoing line, and the only way to
-- reconcile it was a generic posting - which, against the expense account a person would naturally
-- pick, booked the cost a second time and took the money out of the bank twice.
--
-- ===========================================================================
-- What this changes
-- ===========================================================================
--
-- 1. Bank-paid receipts settle through Kruisposten (2000, RGS BLimKrp - "transfers in transit").
--    Posting the receipt credits Kruisposten; matching its bank line posts Kruisposten -> Bank.
--    Kruisposten is inside RGS "Liquide middelen", so the dashboard's cash figure (which sums that
--    group) drops when the receipt is booked exactly as before, while 1100 now equals the bank
--    statement.
--    - app.ensure_posting_defaults: new administrations get Kruisposten (falling back to the bank
--      when a chart has no transit account).
--    - Existing mappings are repointed only where they still hold 0067's value (the bank account);
--      a mapping someone chose deliberately is left alone.
--    - Entries already posted are not touched (append-only ledger). A receipt that credited the
--      bank directly is matched to its bank line WITHOUT a second posting (api.bank.service).
-- 2. bank_transaction.matched_expense_id: which receipt a bank line settled. One bank line per
--    receipt (unique), and a line settles an invoice or a receipt, never both.
--
-- Tenancy: no new table; the new column sits under bank_transaction's existing RLS policies.
-- Ledger: nothing here writes to posting tables.
--
-- NFR-044: backward compatible (a nullable column, a changed default), no downtime. Reversible:
--   drop index bank_transaction_one_line_per_expense_idx;
--   alter table bank_transaction drop constraint bank_transaction_one_match_kind;
--   alter table bank_transaction drop column matched_expense_id;
--   re-run 0067's function body, and point the two mappings back at the bank account.
-- Reversing leaves any bank line matched to a receipt without that link, so do it only before the
-- first such match.

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
    'ADR-086, ADR-092. The journals and posting-account mappings a seeded chart implies; bank-paid '
    'receipts settle through Kruisposten. Fills gaps only; never changes an existing journal or '
    'mapping. Idempotent.';

-- Existing administrations: repoint the two bank-paid methods from the bank to Kruisposten, only
-- where the mapping is still the bank account 0067 put there.
update expense_posting_account m
   set account_id = (
           select t.id from ledger_account t
            where t.administration_id = m.administration_id and t.status = 'active'
              and (t.code = '2000' or t.rgs_code = 'BLimKrp')
            order by (t.code = '2000') desc, t.code
            limit 1
       ),
       updated_at = now()
 where m.purpose in ('business_account', 'business_card')
   and exists (
       select 1 from ledger_account b
        where b.id = m.account_id and (b.code = '1100' or b.rgs_code = 'BLimBan')
   )
   and exists (
       select 1 from ledger_account t
        where t.administration_id = m.administration_id and t.status = 'active'
          and (t.code = '2000' or t.rgs_code = 'BLimKrp')
   );

alter table bank_transaction
    add column matched_expense_id uuid references expense(id);

alter table bank_transaction
    add constraint bank_transaction_one_match_kind check (
        matched_sales_invoice_id is null or matched_expense_id is null
    );

create unique index bank_transaction_one_line_per_expense_idx
    on bank_transaction(matched_expense_id) where matched_expense_id is not null;

comment on column bank_transaction.matched_expense_id is
    'ADR-092. The receipt this outgoing line paid. Its journal_entry_id is the settlement posting '
    '(Kruisposten -> Bank), or the receipt''s own entry when that already credited the bank.';

commit;

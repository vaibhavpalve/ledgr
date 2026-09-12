-- 0040_sales_invoice_posting.sql
-- FR-AR-001 (PRD §6.3) reaching the books, FR-GL-006's AR sub-ledger, and
-- FR-TPL-017's stored rendering. See docs/decisions/ADR-039-invoice-posting.md.
--
--   FR-GL-006   Sub-ledgers for accounts receivable and payable, reconciled to
--               control accounts continuously.
--   FR-TPL-017  Sent invoices are immutable. Editing a template never alters
--               the appearance of an already-issued invoice; THE RENDERED PDF
--               IS STORED AS ISSUED.
--   FR-AR-012   Aged receivables reporting and per-customer statement of
--               account. (Not built; this is what it will read.)
--
-- ===========================================================================
-- This migration writes no postings, and cannot
-- ===========================================================================
--
-- CLAUDE.md's first non-negotiable, and the same opening 0034 makes for the
-- purchase side: nothing here inserts into `journal_entry` or `journal_line`,
-- and `ledgr_app` still has no grant that would let it. The entry is created by
-- `ledger.post_entry` through `api.ledger.service.LedgerService`, exactly as a
-- manual journal is.
--
-- What this adds is everything the ledger needs to be TOLD, and the links back:
--
--   sales_posting_account      which accounts a sale posts to
--   customer.subledger_party_id  which AR party a customer IS, in the ledger
--   sales_invoice.journal_entry_id   the entry it produced, set once
--   sales_invoice.document_id        the PDF as issued, set once
--
-- ===========================================================================
-- The shape of a sales entry
-- ===========================================================================
--
--   Dr  Debiteuren (AR control)        gross     <- carries the party
--   Cr  Omzet <treatment>              taxable   } one pair per VAT group
--   Cr  Te betalen omzetbelasting      vat       } (Art. 226's "per rate")
--
-- The credit side is per VAT GROUP rather than per line, for the reason 0037
-- gives about `sales_invoice_vat_total`: Art. 226 asks for the taxable amount
-- and the VAT per rate, and a per-line split would put one set of numbers on
-- the document and a different set in the books.
--
-- A CREDIT NOTE posts the same lines with the sides exchanged. It is not an
-- entry with negative amounts - `journal_line` refuses those (0020: "amounts
-- are unsigned; direction is carried by which of debit/credit the amount is
-- on"), and a negative debit would break every report that sums a column.
--
-- ===========================================================================
-- Why the AR control account is NOT configured here
-- ===========================================================================
--
-- `expense_posting_account` (0034) maps five purposes because an expense has a
-- free-text CATEGORY and the chart has no idea what "Kantoorbenodigdheden" is.
-- The receivable side has no such gap: FR-GL-006 already designates the AR
-- control account, and `ledger_account_one_control_per_kind_idx` already makes
-- it unique per administration. Adding a mapping row pointing at the account
-- the schema has already singled out would create a second answer to a question
-- that has one, and the two could disagree.
--
-- So `sales_posting_account` maps only what the chart genuinely does not
-- determine: which revenue account a TREATMENT sells into, and where the output
-- VAT lands.
--
-- ===========================================================================
-- Why revenue is not inferred from ledger_account.default_vat_code
-- ===========================================================================
--
-- Tempting, because the shipped RGS chart already carries exactly this:
-- "Omzet hoog tarief" is `btw_21`, "Omzet EU" is `btw_icp`, and so on. Reading
-- it backwards - treatment to account - would need no configuration at all.
--
-- It is the wrong direction. 0020 defines that column as "the VAT default
-- applied when this account is selected": account to treatment, a suggestion
-- offered to a bookkeeper who has already chosen the account. Inverting it
-- assumes the relation is a function, which nothing enforces - "Inkoopwaarde
-- van de omzet" is also `btw_21` - so the inversion is only unambiguous if you
-- ALSO filter by account_type and hope no administration ever creates a second
-- revenue account at the standard rate. Splitting revenue by product line is
-- ordinary bookkeeping, so that hope is misplaced.
--
-- The failure would be silent and expensive: revenue in the wrong account is
-- revenue in the wrong rubriek on the aangifte. 0034's header makes this
-- argument for expense categories and it holds here for the same reason -
-- configuration that has not been done is visible, and a wrong guess is not.
--
-- What the column IS good for is seeding this table, which is FR-ONB's job at
-- chart-creation time and not the posting path's.

begin;

-- The domain schema the other four already have (`ledger`, `vat`, `expenses`,
-- `documents`). 0037 put `invoice_number_gaps` in `public` instead; that is the
-- outlier rather than the convention, and it stays where it is because moving a
-- granted function is a change to something already working.
create schema if not exists invoicing;
comment on schema invoicing is
    'FR-AR. Functions over sales invoices and what they produced: the ledger '
    'lines behind an invoice, and the issued invoices the books do not know '
    'about.';

-- ===========================================================================
-- sales_posting_account
-- ===========================================================================
create table sales_posting_account (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- Two purposes, and no 'accounts_receivable' - see the header.
    purpose             text not null check (purpose in (
                            'revenue',
                            'vat_output'
                        )),

    -- The VAT treatment this row maps. NULL is the FALLBACK row: what an
    -- unmapped treatment posts to. Having one is what keeps a newly recognised
    -- treatment from blocking every invoice; an administration that would
    -- rather refuse simply does not create it.
    --
    -- Constrained to 0028's own table rather than to a copy of FR-AR-002's
    -- list: a treatment the ruleset recognises is one an invoice can carry, and
    -- two vocabularies here would be two ways to spell one rubriek.
    vat_treatment_key   text references vat_treatment(code),

    account_id          uuid not null references ledger_account(id),

    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- One account per purpose per treatment, and one fallback per purpose. Two
-- mappings for one pair would make the posting depend on which row the planner
-- reached first - 0034's argument, applied to a two-part key.
create unique index sales_posting_account_treatment_idx
    on sales_posting_account(administration_id, purpose, vat_treatment_key)
    where vat_treatment_key is not null;

create unique index sales_posting_account_fallback_idx
    on sales_posting_account(administration_id, purpose)
    where vat_treatment_key is null;

create index sales_posting_account_organization_idx
    on sales_posting_account(organization_id);

comment on table sales_posting_account is
    'FR-AR-001 / FR-VAT-001. Which ledger account a sale posts to, per purpose '
    'and per VAT treatment. Configuration rather than inference: revenue in the '
    'wrong account is revenue in the wrong rubriek on the aangifte.';

-- An account from another administration's chart would post one client's
-- turnover into another client's books. The same guard 0034 puts on its own
-- mapping table, and for the same reason.
create or replace function sales_posting_account_same_tenant() returns trigger as $$
declare
    v_account_admin uuid;
    v_account_type  text;
begin
    select administration_id, account_type into v_account_admin, v_account_type
      from ledger_account where id = new.account_id;

    if v_account_admin is distinct from new.administration_id then
        raise exception
            'a posting account belongs to the administration it maps: account is '
            'in %, mapping claims % (CLAUDE.md rule 1)',
            v_account_admin, new.administration_id;
    end if;

    -- A revenue mapping pointing at an expense account would understate
    -- turnover and overstate costs by the same amount, leaving the trial
    -- balance perfectly balanced and both figures wrong. Checked because it is
    -- cheap and because nothing downstream would notice.
    if new.purpose = 'revenue' and v_account_type is distinct from 'revenue' then
        raise exception
            'the revenue mapping points at a % account; turnover posts to a '
            'revenue account (FR-GL-005)', v_account_type;
    end if;
    if new.purpose = 'vat_output' and v_account_type is distinct from 'liability' then
        raise exception
            'the output-VAT mapping points at a % account; VAT you owe the '
            'Belastingdienst is a liability (FR-GL-005)', v_account_type;
    end if;

    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger sales_posting_account_same_tenant_trg
    before insert or update on sales_posting_account
    for each row execute function sales_posting_account_same_tenant();

-- ===========================================================================
-- A customer IS a sub-ledger party - FR-GL-006
-- ===========================================================================
-- 0039's `customer` is the AR master (FR-AR-006); 0020's `subledger_party` is
-- the ledger's own master data for whoever a receivable is owed by. They are
-- two tables about one counterparty, and this is the link.
--
-- The pointer lives HERE, on the customer, rather than a `customer_id` on
-- `subledger_party`. Direction matters: `api.customers` may know that the
-- ledger exists, and the ledger may not know that a customer master does. A
-- column on the ledger's table would make its schema depend on a bounded
-- context outside it, which is the coupling CLAUDE.md's first non-negotiable
-- exists to prevent.
alter table customer
    add column subledger_party_id uuid references subledger_party(id);

-- One customer, one party. Without this a race on first posting would create
-- two parties for one customer, and FR-AR-012's per-customer statement would
-- silently show half the balance.
create unique index customer_subledger_party_idx
    on customer(subledger_party_id) where subledger_party_id is not null;

comment on column customer.subledger_party_id is
    'FR-GL-006. The AR sub-ledger party this customer is, created on first '
    'posting by api.invoicing.posting through the ledger service. Set once.';

-- Set once, like every other link in this schema. Repointing a customer at a
-- different party would move receivables between debtors without a single
-- posting - a change to the books made by editing master data, which is the
-- shape of failure FR-GL-003 exists to rule out.
create or replace function customer_party_link_is_final() returns trigger as $$
begin
    if old.subledger_party_id is not null
       and new.subledger_party_id is distinct from old.subledger_party_id then
        raise exception
            'customer % is already the sub-ledger party %; repointing it would '
            'move receivables between debtors with no posting (FR-GL-006)',
            old.id, old.subledger_party_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger customer_party_link_is_final_trg
    before update on customer
    for each row execute function customer_party_link_is_final();

-- ===========================================================================
-- sales_invoice gains its entry and its rendering
-- ===========================================================================
alter table sales_invoice
    add column journal_entry_id uuid references journal_entry(id),
    add column posted_at        timestamptz,
    -- FR-TPL-017. The PDF AS ISSUED, in the archive (0031) - encrypted under
    -- the administration's own key, hash-verified on every read, write-once,
    -- and retained for CMP-001's seven years. All of that is FR-DOC's work
    -- already done; storing the rendering anywhere else would be a second
    -- archive with none of it.
    add column document_id      uuid references document(id);

-- One entry per invoice and one invoice per entry. Without this a retry could
-- produce a second posting for one invoice - a receivable recorded twice, which
-- is the double-count NFR-032 exists to prevent, arriving by a route
-- idempotency keys do not cover because the two requests are minutes apart.
create unique index sales_invoice_journal_entry_idx
    on sales_invoice(journal_entry_id) where journal_entry_id is not null;

create unique index sales_invoice_document_idx
    on sales_invoice(document_id) where document_id is not null;

comment on column sales_invoice.journal_entry_id is
    'FR-GL-006. The ledger entry this invoice produced. Set once by '
    'api.invoicing.posting through the ledger service - never written here.';
comment on column sales_invoice.document_id is
    'FR-TPL-017. The rendered PDF as issued. Set once; there is no re-render '
    'path, which is what makes "editing a template never alters an '
    'already-issued invoice" true rather than merely intended.';

-- A posted invoice has an entry, and an invoice with an entry is posted.
alter table sales_invoice
    add constraint sales_invoice_posted_at_recorded check (
        (posted_at is null) = (journal_entry_id is null)
    );

-- Only an ISSUED invoice has a posting or a rendering. A draft carries neither:
-- it has asserted nothing, owes nobody, and has no number to print.
alter table sales_invoice
    add constraint sales_invoice_draft_is_unposted check (
        status = 'issued' or (journal_entry_id is null and document_id is null)
    );

-- ---------------------------------------------------------------------------
-- Both links are final
-- ---------------------------------------------------------------------------
-- 0037's `sales_invoice_issued_is_frozen` deliberately permits columns that
-- describe what happened to the document AFTER it was issued, which is how
-- these two get written at all. That permission is exactly wide enough for one
-- write each, and this is what closes it.
--
-- Clearing `journal_entry_id` would leave a posted entry with nothing pointing
-- at it and an invoice free to post again - a receivable recorded twice with no
-- trace of the first. Clearing `document_id` would orphan the rendering and
-- allow a second one, which is FR-TPL-017 undone: the whole content of that
-- requirement is that the bytes a customer received are the bytes that stay.
create or replace function sales_invoice_posting_is_final() returns trigger as $$
begin
    if old.journal_entry_id is not null
       and new.journal_entry_id is distinct from old.journal_entry_id then
        raise exception
            'invoice % is already posted as entry %; a posting is corrected by a '
            'reversing entry (FR-GL-003), never by repointing the invoice',
            old.id, old.journal_entry_id;
    end if;

    if old.document_id is not null
       and new.document_id is distinct from old.document_id then
        raise exception
            'invoice % was rendered as document % when it was issued; the stored '
            'PDF is what the customer received and it is not re-rendered '
            '(FR-TPL-017)', old.id, old.document_id;
    end if;

    if old.posted_at is not null and new.posted_at is distinct from old.posted_at then
        raise exception 'invoice % was posted at %; that is a fact', old.id, old.posted_at;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_posting_is_final_trg
    before update on sales_invoice
    for each row execute function sales_invoice_posting_is_final();

-- ===========================================================================
-- What an invoice's posting looks like, for whoever has to check one
-- ===========================================================================
-- The mirror of `expenses.posting_of`. Reads the entry back through the link so
-- "does this invoice's posting say what the invoice says" is one query rather
-- than a join somebody assembles differently each time.
create or replace function invoicing.posting_of(p_invoice_id uuid)
returns table (
    invoice_id       uuid,
    invoice_reference text,
    journal_entry_id uuid,
    entry_number     bigint,
    entry_date       date,
    account_id       uuid,
    account_code     text,
    account_name     text,
    party_name       text,
    debit            numeric,
    credit           numeric,
    vat_treatment    text,
    line_description text
)
language sql
stable
as $$
    select si.id, si.invoice_reference, je.id, je.entry_number, je.entry_date,
           a.id, a.code, a.name, p.name, jl.debit, jl.credit, jl.vat_treatment,
           jl.description
      from sales_invoice si
      join journal_entry je on je.id = si.journal_entry_id
      join journal_line jl  on jl.journal_entry_id = je.id
      join ledger_account a on a.id = jl.account_id
      left join subledger_party p on p.id = jl.subledger_party_id
     where si.id = p_invoice_id
     order by jl.line_number;
$$;

comment on function invoicing.posting_of(uuid) is
    'FR-GL-006. The ledger lines an invoice produced, as a person checking one '
    'would read them - including the debtor the receivable is owed by.';

-- ---------------------------------------------------------------------------
-- Invoices that should have posted and did not
-- ---------------------------------------------------------------------------
-- Structurally empty while `api.invoicing.service.issue` stays atomic: an
-- invoice becomes issued and posted in one transaction, so a row here means
-- either an invoice issued before this migration, or that the atomicity has
-- been broken. Reported anyway, for the reason 0020 gives about its own gap
-- report - a check that should always be empty is the check on the thing that
-- makes it empty, and an issued invoice missing from the books is a receivable
-- nobody will chase.
create or replace function invoicing.unposted_invoices(p_administration_id uuid)
returns table (
    invoice_id        uuid,
    invoice_reference text,
    invoice_date      date,
    customer_name     text,
    issued_at         timestamptz,
    has_document      boolean
)
language sql
stable
as $$
    select si.id, si.invoice_reference, si.invoice_date, si.customer_name,
           si.issued_at, si.document_id is not null
      from sales_invoice si
     where si.administration_id = p_administration_id
       and si.status = 'issued'
       and si.journal_entry_id is null
     order by si.issued_at;
$$;

comment on function invoicing.unposted_invoices(uuid) is
    'An issued invoice with no ledger entry: a receivable the books do not '
    'know about. Should always be empty - see the function body.';

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table sales_posting_account owner to ledgr_migrator;

-- No DELETE: a mapping that produced postings is the explanation for where they
-- went. Superseded by an UPDATE, following this schema's convention.
grant select, insert, update on sales_posting_account to ledgr_app;
grant select on sales_posting_account to ledgr_ops;

grant usage on schema invoicing to ledgr_app, ledgr_ops;
revoke all on all functions in schema invoicing from public;
grant execute on function invoicing.posting_of(uuid)        to ledgr_app, ledgr_ops;
grant execute on function invoicing.unposted_invoices(uuid) to ledgr_app, ledgr_ops;

-- Neither function is SECURITY DEFINER, so both see exactly what the calling
-- request may see - reading the ledger through RLS, never around it.

alter table sales_posting_account enable row level security;
alter table sales_posting_account force row level security;

create policy sales_posting_account_select on sales_posting_account
    for select using (app.has_administration_access(administration_id));
create policy sales_posting_account_insert on sales_posting_account
    for insert with check (app.has_administration_access(administration_id));
create policy sales_posting_account_update on sales_posting_account
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;

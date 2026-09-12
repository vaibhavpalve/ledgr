-- 0039_customer_master.sql
-- FR-AR-006 (PRD 6.3), FR-ONB-003 (PRD 6.1). See
-- docs/decisions/ADR-038-customer-master.md.
--
--   FR-AR-006  Customer master with KvK number, VAT number, Peppol participant
--              ID discovery, payment terms, credit limit, preferred delivery
--              channel and language.
--   FR-ONB-003 ... validate EU VAT numbers via VIES.
--
-- ===========================================================================
-- A customer record is a DEFAULT, never a fact about a document
-- ===========================================================================
--
-- 0037 snapshots the customer's name, address, country and VAT number onto
-- every sales_invoice, and its comment promised that FR-AR-006 would POPULATE
-- those columns rather than replace them. This migration keeps that promise:
-- `sales_invoice.customer_id` is added as a nullable pointer BESIDE the
-- snapshot, not instead of it.
--
-- The distinction is the whole point of the table:
--
--   customer          what we believe about this counterparty TODAY. Editable,
--                     re-read on every new document, and wrong the moment they
--                     move offices.
--   sales_invoice.*   what was asserted to them ON A DATE. Frozen at issue,
--                     never re-derived.
--
-- A customer who moves next year must not silently rewrite the address on a
-- document already filed with the Belastingdienst. So the pointer is for
-- provenance and reporting ("every invoice to this customer"), and the
-- snapshot is for what the document says. `sales_invoice_issued_is_frozen` is
-- extended below so the pointer itself is frozen too - re-pointing an issued
-- invoice at a different customer would rewrite its history without changing a
-- single visible field.
--
-- ===========================================================================
-- A customer is never deleted
-- ===========================================================================
--
-- There is no DELETE grant and no delete policy. A customer row is referenced
-- by statutory documents that CMP-001 keeps for seven years, and "who was this
-- invoice made out to" has to stay answerable for all of them. `archived_at`
-- is what "we no longer trade with them" means; it removes the record from
-- pickers and changes nothing about the documents.
--
-- This is 0001's own posture, in its words: "No DELETE grant anywhere: these
-- rows are archived/revoked/closed via status columns, never removed. Even a
-- bug in a future policy could not make a DELETE succeed, because the privilege
-- to attempt one does not exist for ledgr_app." The same argument 0031 makes
-- for documents and 0037 for issued invoices, one table further out.
--
-- ===========================================================================
-- The VAT number carries a VERDICT and a DATE, not a boolean
-- ===========================================================================
--
-- FR-ONB-003 wants EU VAT numbers validated through VIES. Two facts about VIES
-- shape the columns:
--
--   * it is frequently unavailable, per member state and without warning.
--     NFR-026 says core bookkeeping continues when an integration is down, so
--     "VIES did not answer" must be a storable state rather than a failed
--     save. `vat_number_status` has `unavailable` for exactly that.
--   * for an intra-Community supply (btw_icp) the supplier's EVIDENCE of
--     having checked is the check itself, on a date. So the answer and the
--     moment it was given are stored, not recomputed - the same argument
--     CMP-014 makes about rates, and 0037 about VAT totals.
--
-- `unchecked` is the initial state and is deliberately distinct from `invalid`.
-- "Nobody has asked VIES" and "VIES said no" are different facts, and a screen
-- that showed them the same way would present an unverified customer as a
-- rejected one.
--
-- ===========================================================================
-- Peppol participant ID: the column exists, discovery does not
-- ===========================================================================
--
-- FR-AR-005 puts Peppol in P2. The identifier is stored now because it is part
-- of what FR-AR-006 names and because a business that already knows its
-- customer's participant ID should be able to record it. What is NOT here is
-- any claim to have LOOKED it up: `peppol_participant_id` is null until
-- somebody or something fills it, and `api.customers.peppol` refuses to invent
-- a value. See its module docstring for why a stub that returned
-- "not registered" would be worse than one that returns "not configured".

-- ===========================================================================
-- customer - FR-AR-006
-- ===========================================================================
create table customer (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1. Both, on every row, and RLS reads administration_id.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    -- The name that goes on the invoice. `trade_name` is what the person
    -- typing recognises; both are kept for the reason FR-FRM-000 keeps both on
    -- an administration - a business is looked up by the name on the door and
    -- invoiced under the name in the register.
    name                text not null check (length(btrim(name)) > 0),
    trade_name          text,

    -- -----------------------------------------------------------------------
    -- Address - structured, unlike the invoice's snapshot
    -- -----------------------------------------------------------------------
    -- The same split 0038 made for the supplier's own address, for the same
    -- two reasons: EN 16931 (FR-AR-005's Peppol, P2) wants cbc:StreetName,
    -- cbc:PostalZone and cbc:CityName as separate elements and does not accept
    -- a blob; and "is the address blank" is satisfied by a single space while
    -- "are street, postcode and city all present" is the question Wet OB art.
    -- 35a(1)(e) actually asks.
    --
    -- `api.customers.address.format_address` renders these into the single
    -- text column 0037 snapshots. One direction only - the rendered string is
    -- never parsed back.
    address_line1       text,
    address_line2       text,
    postal_code         text,
    city                text,
    -- ISO 3166-1 alpha-2, matching sales_invoice.customer_country and
    -- administration.country so all three sides of a document are written the
    -- same way. Defaulted rather than nullable: a country guessed at render
    -- time is a country guessed wrong, and it decides the VAT treatment.
    country             text not null default 'NL'
                            check (country ~ '^[A-Z]{2}$'),

    -- -----------------------------------------------------------------------
    -- FR-AR-006's identifiers
    -- -----------------------------------------------------------------------
    -- KvK number: eight digits. Stored as text, not a bigint - it is an
    -- identifier that may carry a leading zero, and arithmetic on it is never
    -- meaningful. Only Dutch counterparties have one, so it is nullable and
    -- its shape is checked only when present.
    kvk_number          text check (kvk_number ~ '^[0-9]{8}$'),

    -- The EU VAT identification number, stored in VIES's own normalised form:
    -- country prefix + national part, no spaces, no dots, upper case
    -- (NL123456789B01). Normalisation happens in
    -- `api.customers.vat_number.normalise` before it reaches this column, so
    -- two spellings of one number cannot become two customers.
    --
    -- The CHECK is deliberately loose - two letters and at least two more
    -- characters. The per-country formats live in
    -- api.customers.vat_number, where they can be corrected without a
    -- migration when a member state changes one, and the only authority on
    -- whether a number EXISTS is VIES.
    vat_number          text check (vat_number ~ '^[A-Z]{2}[0-9A-Z+*]{2,13}$'),

    -- FR-ONB-003. See the header: a verdict, not a boolean.
    vat_number_status   text not null default 'unchecked'
                            check (vat_number_status in (
                                'unchecked',      -- nobody has asked VIES
                                'syntax_invalid', -- fails the format check; no call made
                                'valid',          -- VIES confirmed it
                                'invalid',        -- VIES denied it
                                'unavailable'     -- VIES could not answer (NFR-026)
                            )),
    -- When the verdict above was obtained. The evidence for an intra-Community
    -- supply is the check on a DATE, so this is not an operational timestamp.
    vat_number_checked_at timestamptz,
    -- The name VIES returned, where the member state discloses one. Kept
    -- because a `valid` verdict against a name that is not the customer's is
    -- the interesting case, and it is invisible without this column.
    vat_number_checked_name text,
    -- VIES's own request identifier, returned when a consultation number is
    -- requested. It is the reference a tax authority asks for.
    vat_number_consultation_number text,

    -- FR-AR-006's "Peppol participant ID discovery". The identifier is a
    -- scheme and a value ("0106:12345678" is a Dutch KvK-scheme participant,
    -- "9944:NL123456789B01" a VAT-scheme one). Stored whole rather than split,
    -- because it is an opaque identifier to everything in this system and
    -- Peppol's own addressing uses the joined form.
    --
    -- P2. Nothing discovers this yet - see api.customers.peppol.
    peppol_participant_id text
                            check (peppol_participant_id ~ '^[0-9]{4}:[0-9A-Za-z._~%-]{1,50}$'),
    peppol_checked_at   timestamptz,

    -- -----------------------------------------------------------------------
    -- FR-AR-006's commercial terms
    -- -----------------------------------------------------------------------
    -- Days from invoice date to due date. 30 is the Dutch statutory default
    -- where nothing is agreed (BW art. 6:119a); a B2B term beyond 60 days is
    -- lawful only where expressly agreed and not "kennelijk onbillijk", which
    -- is a judgment about a contract this system has not seen. So the CHECK is
    -- a sanity bound, not the statute - encoding 60 here would refuse terms
    -- that are perfectly legal and push people to record something false.
    payment_terms_days  integer not null default 30
                            check (payment_terms_days between 0 and 365),

    -- NFR-031: numeric, never a float, everywhere in the calculation path.
    -- NULL means "no limit set", which is NOT the same as 0 - zero is a
    -- customer who may have no credit at all, and a screen that showed the two
    -- alike would put a trading halt on everybody who was never assessed.
    credit_limit        numeric(19,2) check (credit_limit >= 0),

    -- FR-AR-005's channels. `peppol` is selectable before P2 ships: it is a
    -- PREFERENCE, and a customer who has told us how they want to be invoiced
    -- should be recorded as having said so. Whether it can be honoured is a
    -- separate question the sender asks (participant id present), never a
    -- reason to lose the answer.
    delivery_channel    text not null default 'email'
                            check (delivery_channel in ('email', 'peppol', 'post')),
    -- Where an emailed invoice goes. Separate from any user account: a
    -- customer is a counterparty, not a person who signs in here.
    invoice_email       text,

    -- FR-AR-006's "language", and FR-TPL-013's recipient language. Mirrors the
    -- `users_language` CHECK in 0030 and api.i18n.language.Language, so a
    -- language the product ships is the only thing storable here.
    language            text not null default 'nl'
                            check (language in ('nl', 'en')),

    notes               text,

    -- See the header: no delete, ever. This is what "we no longer trade with
    -- them" means.
    archived_at         timestamptz,

    -- NFR-032.
    idempotency_key     text,

    created_by_user_id  uuid references users(id),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    -- An email channel with nowhere to send is a customer who looks
    -- invoiceable and is not. Checked here rather than at send time, because
    -- FR-AR-005 discovering it has no address is a failure hours after the
    -- person who could fix it has gone.
    constraint customer_email_channel_has_an_address check (
        delivery_channel <> 'email' or invoice_email is not null
    ),

    -- A verdict without a date is unusable as evidence, and a date without a
    -- verdict is meaningless. The two unchecked states carry neither.
    constraint customer_vat_verdict_is_dated check (
        (vat_number_status in ('unchecked', 'syntax_invalid')
            and vat_number_checked_at is null)
        or
        (vat_number_status in ('valid', 'invalid', 'unavailable')
            and vat_number_checked_at is not null)
    ),

    -- A verdict about a number that is not there describes nothing.
    constraint customer_vat_verdict_needs_a_number check (
        vat_number is not null or vat_number_status = 'unchecked'
    )
);

create index customer_administration_idx on customer(administration_id, name);
create index customer_organization_idx on customer(organization_id);

-- FR-FRM-000-style lookup: by name, trade name or KvK number. NFR-008's <2s
-- over the archive is what pg_trgm is here for (CLAUDE.md's search deviation),
-- and a customer picker is the most-typed search in the product. Restated
-- rather than assumed from 0018, the way 0031 restates it: a migration that
-- depends on an extension should say so where it uses it.
create extension if not exists pg_trgm;

create index customer_name_trgm_idx on customer using gin (name gin_trgm_ops);
create index customer_trade_name_trgm_idx on customer using gin (trade_name gin_trgm_ops)
    where trade_name is not null;

-- Looked up, never unique. Two branches of one customer share a KvK number -
-- each vestiging has its own vestigingsnummer but the register number is the
-- legal entity's - so a unique constraint here would refuse a legitimate
-- second delivery address. Duplicate detection on a customer master is a
-- judgment (FR-AR-006 does not ask for one), and a constraint is not the place
-- to make it.
create index customer_kvk_idx on customer(administration_id, kvk_number)
    where kvk_number is not null;
create index customer_vat_number_idx on customer(administration_id, vat_number)
    where vat_number is not null;

create unique index customer_idempotency_idx
    on customer(administration_id, idempotency_key)
    where idempotency_key is not null;

-- ===========================================================================
-- The link from the invoice - FR-AR-006 populates, it does not replace
-- ===========================================================================
alter table sales_invoice
    -- Nullable, permanently. A one-off customer must stay invoiceable without
    -- first being made a master record: 0037's snapshot columns are NOT NULL
    -- and this one is not, which is the correct way round. The document can
    -- always say who it was for; the master record is an optional convenience
    -- that filled the document in.
    add column customer_id uuid references customer(id),

    -- The recipient's language AS IT WAS when the document was raised
    -- (FR-TPL-013). Snapshotted alongside the name and address rather than
    -- joined from `customer.language`, and for exactly the same reason: a
    -- customer who switches to English next year must not silently restate the
    -- legal wording on an invoice already in their hands. Art. 226(11)'s
    -- statement of WHY no VAT was charged is legal content, not presentation.
    add column customer_language text not null default 'nl'
                                     check (customer_language in ('nl', 'en'));

create index sales_invoice_customer_idx on sales_invoice(customer_id)
    where customer_id is not null;

comment on column sales_invoice.customer_id is
    'FR-AR-006. Provenance only: which master record filled in the snapshot '
    'columns beside it. Never joined to render the document - see 0039''s header.';

-- Tenant coherence, the same guard 0037 puts on its lines: a customer from
-- another administration would put one tenant's counterparty on another
-- tenant's statutory document. RLS would hide the row on read; this is what
-- stops it being written in the first place.
--
-- Note which branch actually fires for a CROSS-TENANT id. This function is not
-- SECURITY DEFINER, so its SELECT runs under RLS as the calling session: a
-- customer belonging to another organization is not visible, and the `not
-- found` branch raises rather than the administration comparison. That is the
-- correct outcome and the correct message - the two cases are indistinguishable
-- to the caller, which is the property IAM-001 wants. The explicit
-- administration check below is what catches the remaining case: two
-- administrations of the SAME organization, where the row IS visible.
create or replace function sales_invoice_customer_same_tenant() returns trigger as $$
declare
    v_customer_admin uuid;
begin
    if new.customer_id is null then
        return new;
    end if;

    select administration_id into v_customer_admin
      from customer where id = new.customer_id;

    if not found then
        raise exception 'customer % does not exist', new.customer_id;
    end if;
    if v_customer_admin is distinct from new.administration_id then
        raise exception
            'customer % belongs to administration %, but the invoice belongs to % '
            '(IAM-001)', new.customer_id, v_customer_admin, new.administration_id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_customer_same_tenant_trg
    before insert or update on sales_invoice
    for each row execute function sales_invoice_customer_same_tenant();

-- ===========================================================================
-- 0037's freeze, extended to the two new columns
-- ===========================================================================
-- Replaced wholesale rather than added to, because there is one definition of
-- "what an issued invoice may still change" and it should be readable in one
-- place. The body is 0037's with `customer_id` and `customer_language` added.
--
-- Both belong in it. Re-pointing an issued invoice at a different customer
-- would rewrite its provenance while every visible field stayed identical -
-- the quietest possible corruption of a statutory record. And the language
-- decides which legal wording the document carries (art. 226(11)), so changing
-- it changes the document.
create or replace function sales_invoice_issued_is_frozen() returns trigger as $$
begin
    if old.status <> 'issued' then
        return new;
    end if;

    if new.invoice_number   is distinct from old.invoice_number
    or new.number_prefix    is distinct from old.number_prefix
    or new.invoice_date     is distinct from old.invoice_date
    or new.supply_date      is distinct from old.supply_date
    or new.due_date         is distinct from old.due_date
    or new.customer_name    is distinct from old.customer_name
    or new.customer_address is distinct from old.customer_address
    or new.customer_country is distinct from old.customer_country
    or new.customer_vat_number is distinct from old.customer_vat_number
    or new.customer_id      is distinct from old.customer_id
    or new.customer_language is distinct from old.customer_language
    or new.fiscal_year_id   is distinct from old.fiscal_year_id
    or new.administration_id is distinct from old.administration_id
    or new.organization_id  is distinct from old.organization_id
    then
        raise exception
            'invoice % has been issued; its content cannot be changed. Correct '
            'it with a credit note (FR-AR-001).', old.id;
    end if;

    return new;
end;
$$ language plpgsql;

-- ===========================================================================
-- updated_at, maintained by the database
-- ===========================================================================
-- In a trigger rather than in the repository's UPDATE statement, so a second
-- write path - a script, a support tool, a migration - cannot leave a row
-- whose contents moved and whose timestamp did not.
create or replace function customer_touch_updated_at() returns trigger as $$
begin
    new.updated_at := now();
    return new;
end;
$$ language plpgsql;

create trigger customer_touch_updated_at_trg
    before update on customer
    for each row execute function customer_touch_updated_at();

-- ===========================================================================
-- Row-level security - IAM-001, IAM-005
-- ===========================================================================
alter table customer enable row level security;
alter table customer force  row level security;

create policy customer_select on customer
    for select using (app.has_administration_access(administration_id));
create policy customer_insert on customer
    for insert with check (app.has_administration_access(administration_id));
create policy customer_update on customer
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));
-- Deliberately no delete policy, and no delete grant below. See the header.

grant select, insert, update on customer to ledgr_app;

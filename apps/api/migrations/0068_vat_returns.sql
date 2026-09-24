-- 0068_vat_returns.sql
-- The BTW return as filed: FR-VAT-001, FR-VAT-003 (receipt/evidence), FR-VAT-011.
-- See docs/decisions/ADR-087-vat-return-from-the-ledger.md.
--
-- ===========================================================================
-- Only FILED returns are stored
-- ===========================================================================
--
-- A return that has not been filed is not a record of anything: it is the ledger's current
-- answer to "what would this period's aangifte say", and `api.vat_returns` computes it on every
-- read. Storing drafts would give two answers to that question the moment a posting landed.
--
-- A filed return IS a record: the figures sent to the Belastingdienst, by whom, when, under which
-- rules (the fingerprint 0028's barrier keeps valid), with the reference the Belastingdienst gave
-- back. It is written once, in the same transaction that hard-locks the period
-- (ledger.mark_period_filed), and never changed.
--
-- ===========================================================================
-- Append-only
-- ===========================================================================
--
-- No UPDATE or DELETE grant, and a trigger refusing both for any role that has one anyway - the
-- same belt-and-braces 0020 gives journal_line. A filed return is corrected by a suppletie
-- (FR-VAT-005), never by editing what was filed.

begin;

create table vat_return (
    id                      uuid primary key default gen_random_uuid(),
    organization_id         uuid not null references organization(id),
    administration_id       uuid not null references administration(id),
    period_id               uuid not null references period(id),

    period_start            date not null,
    period_end              date not null,

    -- How it reached the Belastingdienst. 'manual': the figures were entered in Mijn
    -- Belastingdienst Zakelijk and the reference recorded here. 'digipoort': sent by LEDGR
    -- (FR-VAT-003; the adapter exists, the connection does not yet - see ADR-087).
    filing_channel          text not null check (filing_channel in ('manual', 'digipoort')),
    filing_reference        text,

    -- The boxes exactly as filed: [{code, turnover, turnover_rounded, vat, vat_rounded}], decimal
    -- STRINGS (NFR-031), never JSON numbers.
    boxes                   jsonb not null check (jsonb_typeof(boxes) = 'array'),
    output_vat              numeric(19, 2) not null,
    input_vat               numeric(19, 2) not null,
    total_due               numeric(19, 2) not null,

    rules_fingerprint       text not null,
    ruleset_provisional     boolean not null,
    -- The warnings the filer saw and accepted (FR-VAT-002), by code.
    warnings_acknowledged   jsonb not null default '[]'::jsonb
                                check (jsonb_typeof(warnings_acknowledged) = 'array'),

    filed_by_user_id        uuid not null references users(id),
    filed_at                timestamptz not null default now(),

    constraint vat_return_period_order check (period_end >= period_start),
    constraint vat_return_whole_euros check (
        output_vat = round(output_vat) and input_vat = round(input_vat)
    )
);

-- One filed return per period. A correction is a suppletie, not a second return.
create unique index vat_return_one_per_period_idx on vat_return(period_id);
create index vat_return_administration_idx on vat_return(administration_id, period_end);
create index vat_return_organization_idx on vat_return(organization_id);

comment on table vat_return is
    'FR-VAT-001/003/011. A BTW return as filed. Insert-only; drafts are computed from the ledger '
    'and never stored. See ADR-087.';

create or replace function vat_return_belongs_to_its_period() returns trigger as $$
declare
    v_period period%rowtype;
begin
    select * into v_period from period where id = new.period_id;
    if not found then
        raise exception 'period % does not exist', new.period_id;
    end if;
    if v_period.administration_id <> new.administration_id then
        raise exception 'period % belongs to another administration', new.period_id;
    end if;
    if v_period.start_date <> new.period_start or v_period.end_date <> new.period_end then
        raise exception 'a return''s dates must be its period''s (%..%)',
            v_period.start_date, v_period.end_date;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger vat_return_belongs_to_its_period_trg
    before insert on vat_return
    for each row execute function vat_return_belongs_to_its_period();

create or replace function vat_return_is_final() returns trigger as $$
begin
    raise exception 'a filed VAT return is final; correct it with a suppletie (FR-VAT-005)';
end;
$$ language plpgsql;

create trigger vat_return_is_final_trg
    before update or delete on vat_return
    for each row execute function vat_return_is_final();

alter table vat_return owner to ledgr_migrator;
grant select, insert on vat_return to ledgr_app;
grant select on vat_return to ledgr_ops;

alter table vat_return enable row level security;
alter table vat_return force row level security;

create policy vat_return_select on vat_return
    for select using (app.has_administration_access(administration_id));
create policy vat_return_insert on vat_return
    for insert with check (app.has_administration_access(administration_id));

commit;

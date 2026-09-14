-- 0035_journal_line_vat_treatment.sql
-- FR-VAT-001's raw material, added for FR-EXP-001e. See
-- docs/decisions/ADR-033-expense-posting.md.
--
-- ===========================================================================
-- Why a posting line has to carry its own VAT treatment
-- ===========================================================================
--
-- The PRD's data model (§12) gives PostingLine a `vat_code`; migration 0020
-- built everything else on that row and not this. It is needed now, because
-- FR-EXP-001e says the payment method "determines the posting" and the VAT
-- treatment determines the other half of it - and a return built by joining
-- back to whatever produced each entry would have to know about expenses,
-- sales invoices, purchase invoices and manual journals separately.
--
-- One column on the line means FR-VAT-001 reads the LEDGER, which is the only
-- place every source has already agreed on.
--
-- Nullable, and it stays nullable: a bank transfer between two own accounts
-- carries no VAT, and forcing a code onto it would invent one. Every existing
-- row keeps NULL - this is an append-only table with no UPDATE grant, so a
-- backfill is not merely undesirable but impossible (FR-GL-003).
--
-- ===========================================================================
-- The function is re-created, not edited
-- ===========================================================================
--
-- `ledger.post_entry` below is 0021's definition (the current one - it added
-- `p_suppletie_id`, not 0020's original) with two lines added: the column in
-- the INSERT and the value from the line's JSON. It is reproduced in full
-- because `create or replace` has no other form, and it was extracted
-- mechanically rather than retyped - a hand-copied 128-line function is a
-- transcription error waiting to be found by an unbalanced entry.
--
-- The SIGNATURE is unchanged from 0021's (12 parameters, `p_suppletie_id`
-- last) - a correction: an earlier version of this migration reproduced
-- 0020's 11-parameter signature instead of 0021's 12-parameter one, which
-- `create or replace` treats as a DIFFERENT overload rather than a
-- replacement (matched by argument types, not by name) - 0021's version was
-- explicit about exactly this risk ("two overloads of the ledger's only
-- write function is exactly the ambiguity this schema cannot afford") and
-- this migration briefly reintroduced it anyway, silently, discovered only
-- once this migration set was applied against a real Postgres and
-- `ledger.post_entry(...)` started raising `AmbiguousFunctionError`. Every
-- grant issued in 0020, 0021, 0024, 0028 and 0029 still names this function
-- and no privilege is disturbed - true now that the signature genuinely is
-- unchanged.

begin;

alter table journal_line
    add column vat_treatment text references vat_treatment(code);

comment on column journal_line.vat_treatment is
    'FR-VAT-001. Which VAT treatment applied to this line (migration 0028''s '
    'effective-dated set). Null where none applies - a transfer between own '
    'accounts carries no VAT.';

-- Every posting a VAT return has to find, without scanning the whole ledger.
create index journal_line_vat_treatment_idx
    on journal_line(administration_id, vat_treatment)
    where vat_treatment is not null;
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
        posted_by_user_id, source_system, reverses_entry_id, idempotency_key,
        suppletie_id
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
            description, vat_treatment
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
            v_line->>'description',
            -- FR-VAT-001 / FR-EXP-001e. Which VAT applied to THIS line, so a
            -- return can be built from the ledger rather than from whatever
            -- produced it. Null on lines that carry no VAT.
            v_line->>'vat_treatment'
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



-- The owner and every grant follow the signature, which has not changed. Both
-- are re-stated for the reason 0021 gives about its own re-issues: this file's
-- list is then the complete picture of who may call what it touches.
alter function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text, uuid
) owner to ledgr_ledger;

grant execute on function ledger.post_entry(
    uuid, uuid, uuid, date, text, text, uuid, text, jsonb, uuid, text, uuid
) to ledgr_app;

commit;

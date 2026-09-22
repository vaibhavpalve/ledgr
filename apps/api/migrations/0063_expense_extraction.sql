-- 0063_expense_extraction.sql
-- FR-EXP-001c / FR-AP-002: an invoice read automatically, and what was read.
--
-- ===========================================================================
-- Two nullable columns, and what each is for
-- ===========================================================================
--
--   invoice_number  the supplier's own number for the invoice. It is a field
--                   of the claim like supplier and date, so it lives on the
--                   row and is editable in the form, not buried in JSON.
--
--   extraction      HOW the fields above were filled, never WHAT they were:
--                   {"status", "provider", "model", "fields": {name: confidence}}.
--                   The values are already in the columns; a second copy here
--                   would be a second place for a correction to be missed. What
--                   this adds is the one thing the columns cannot say - which
--                   fields a machine wrote, and how sure it was - so the
--                   review screen can point at the ones worth checking.
--
-- Null on both means "nobody read this" (every existing row, and any capture
-- while extraction is switched off), which is a normal state, not a gap.
--
-- NFR-044: additive, backward compatible, no table rewrite (Postgres adds a
-- nullable column without one). Reversible by dropping both columns.

alter table expense
    add column invoice_number text,
    add column extraction     jsonb;

comment on column expense.invoice_number is
    'The supplier''s invoice number, as printed. Free text: formats vary '
    'by supplier and nothing downstream parses it.';

comment on column expense.extraction is
    'FR-AP-002. How the fields were filled by automatic reading: status '
    '(done | failed | skipped), provider, model and a confidence per field. '
    'Never the values themselves - those are the columns. Null: not read.';

-- 0051_administration_iban.sql
-- SI-02: the supplier's own bank account, for the EPC069-12 "pay by bank"
-- QR code on an invoice PDF.
--
-- ===========================================================================
-- Nullable, and why that is not a weakening (same reasoning as 0038)
-- ===========================================================================
--
-- Every existing administration predates this column, and there is no legal
-- requirement (unlike address/VAT/KvK - FR-AR-003) that forces one to exist
-- before an invoice can be issued. An administration with no IBAN on file
-- simply gets no QR code on its invoices - api.invoicing.epc_qr is never
-- called for one - rather than the statutory gate refusing to issue.
--
-- ===========================================================================
-- No CHECK constraint here, deliberately
-- ===========================================================================
--
-- Unlike migration 0038's country CHECK, an IBAN's shape varies by country
-- (15-34 characters) and its real validity property is a checksum
-- (ISO 7064 MOD 97-10), not a regular expression - api.invoicing.iban.parse
-- is where that lives, applied at the moment it is SET (the PATCH
-- .../administrations/{id} handler), not as a constraint every read has to
-- trust a migration got right forever. A column-level CHECK could only ever
-- re-implement a cheap subset of that and would drift from it.
--
-- NFR-044: additive, backward compatible, and reversible by dropping one
-- column. No table rewrite - Postgres adds a nullable column without one.

alter table administration
    add column iban text;

comment on column administration.iban is
    'SI-02: the supplier''s own bank account (validated, mod-97 checksum, at '
    'write time - see api.invoicing.iban.parse). Stored compact and '
    'upper-case, e.g. NL91ABNA0417164300. Null means no QR code is rendered '
    'on this administration''s invoices; this is never a statutory '
    'requirement, unlike address/VAT/KvK (FR-AR-003).';

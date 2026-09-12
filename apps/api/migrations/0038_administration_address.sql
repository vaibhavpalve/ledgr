-- 0038_administration_address.sql
-- FR-AR-003 (PRD 6.3). See docs/decisions/ADR-037-sales-invoices.md.
--
--   FR-AR-003  Invoices comply with Dutch statutory invoice content
--              requirements; the system blocks sending if a mandatory field is
--              absent.
--
-- ===========================================================================
-- The supplier's own address, which nothing could supply
-- ===========================================================================
--
-- Wet OB 1968 art. 35a(1)(e), implementing EU VAT Directive art. 226(5),
-- requires an invoice to carry the full name and address of the SUPPLIER as
-- well as the customer. `administration` carried a legal name, a KvK number
-- and a VAT number, and no address at all - so FR-AR-003's gate refused every
-- invoice, correctly and permanently, for want of a column.
--
-- ===========================================================================
-- Structured, not one text field
-- ===========================================================================
--
-- The customer's address on `sales_invoice` is a single snapshotted text
-- column, and this is deliberately not the same shape. The two are different
-- kinds of thing:
--
--   the customer's   copied onto a statutory document at issue and frozen
--                    there. Whatever it looked like on the day is what the
--                    invoice says, and it is never parsed again.
--   the supplier's   the business's own registered address, read afresh for
--                    every document, and the one that has to go into a
--                    structured e-invoice.
--
-- FR-AR-005 puts Peppol BIS Billing 3.0 / NLCIUS (EN 16931) in P2, and that
-- standard does not accept a blob: the seller's postal address is
-- cbc:StreetName, cbc:CityName, cbc:PostalZone and
-- cac:Country/cbc:IdentificationCode as separate elements. Storing one string
-- now would mean parsing Dutch addresses out of free text later, which is the
-- kind of migration that quietly gets 3% of a tenant base wrong.
--
-- It also lets FR-AR-003's check be worth something. "Is the address blank" is
-- satisfied by a single space; "are the street, postcode and city all present"
-- is the question the statute actually asks.
--
-- ===========================================================================
-- Nullable, and why that is not a weakening
-- ===========================================================================
--
-- Every existing administration predates this column, and a NOT NULL with a
-- backfilled placeholder would put a fabricated address on a statutory
-- document - far worse than having none. So the columns are nullable and
-- FR-AR-003's gate is what refuses to issue until they are filled. The
-- requirement is enforced at the moment it matters, against the real value,
-- rather than by a default nobody chose.
--
-- NFR-044: additive, backward compatible, and reversible by dropping four
-- columns. No table rewrite - Postgres adds a nullable column without one.

alter table administration
    add column address_line1 text,
    add column address_line2 text,
    add column postal_code   text,
    add column city          text,
    -- ISO 3166-1 alpha-2, matching sales_invoice.customer_country so the two
    -- sides of an invoice are written the same way. Defaulted rather than
    -- nullable: an administration in this system files a Dutch return, and a
    -- country that has to be guessed at render time is a country that will be
    -- guessed wrong.
    add column country       text not null default 'NL'
                                 check (country ~ '^[A-Z]{2}$');

comment on column administration.address_line1 is
    'Street and number. FR-AR-003 / Wet OB art. 35a(1)(e).';
comment on column administration.address_line2 is
    'Optional second line: suite, unit, PO box.';
comment on column administration.postal_code is
    'Postcode. Free text rather than a Dutch 1234 AB pattern, because an '
    'administration may be registered outside the Netherlands.';
comment on column administration.city is
    'City. FR-AR-003 / Wet OB art. 35a(1)(e).';

-- 0030_localisation.sql
-- FR-LOC-001b and FR-LOC-002 (PRD §20). See
-- docs/decisions/ADR-029-localisation.md.
--
--   FR-LOC-001b  Language is a per-user setting, not per-organization. A Dutch
--                bookkeeper and an English-speaking owner can work in the same
--                administration, each in their own language, seeing the same
--                data.
--   FR-LOC-002   Locale-correct number, date and currency formatting;
--                European decimal comma. Formatting follows the
--                administration's locale, not the user's UI language, so
--                amounts read identically to every user.
--
-- ===========================================================================
-- Two columns, on two different tables, and that IS the requirement
-- ===========================================================================
--
-- The obvious schema is one `locale` column, and it is the wrong one. §20
-- splits the setting in two because the two answer different questions for
-- different people:
--
--   users.language                  WHICH WORDS a person reads.
--   administration.formatting_locale HOW FIGURES ARE WRITTEN in these books.
--
-- Putting language on `users` is what makes "a Dutch bookkeeper and an
-- English-speaking owner in the same administration" expressible at all: a
-- column on `organization` or on `administration` would force one of them
-- into the other's language, and a firm serving both would have to choose.
--
-- Putting formatting on `administration` is what makes "amounts read
-- identically to every user" true. Derived from the reader instead, the same
-- trial balance would show 1.234,56 to one of them and 1,234.56 to the other,
-- and `1.234` means one thousand two hundred and thirty-four to one and
-- one-point-two-three-four to the other. Two people would read two different
-- numbers off one ledger and have no way to notice.
--
-- ===========================================================================
-- Why one is nullable and the other is not
-- ===========================================================================
--
-- `users.language` is NULLABLE, and NULL means "this person has never told us"
-- - which is a real state and a different one from "chose Dutch". IAM-010g
-- says the pre-login choice "persists on the device and is applied to the
-- account after first login", and that step needs to distinguish a user whose
-- account setting is unset (seed it from the device) from one who has since
-- chosen Dutch on purpose (leave it alone). A NOT NULL DEFAULT 'nl' would
-- erase the difference on the first row written, and every user invited by a
-- firm would be silently Dutch with no way to tell whether they meant it.
--
-- `administration.formatting_locale` is NOT NULL, because there is no
-- corresponding "unknown" state: an administration with no formatting locale
-- cannot render a trial balance, an invoice or a filing. Every administration
-- in this schema today is Dutch, so the default is the right answer rather
-- than a placeholder.
--
-- ===========================================================================
-- NFR-044: backward compatible, reversible, no downtime
-- ===========================================================================
--
-- Both statements are ADD COLUMN with a constant default (or none), which
-- Postgres 11+ records in the catalogue rather than rewriting the table - no
-- long ACCESS EXCLUSIVE lock on `users` or `administration`, both of which
-- every request touches.
--
-- Backward compatible: code that has never heard of either column keeps
-- working, because neither is referenced by an existing constraint, view or
-- trigger and both have an answer for a row that does not mention them.
--
-- Reversible: `alter table users drop column language;` and the same for
-- `administration.formatting_locale`. Dropping loses stored preferences and
-- nothing structural - no other table references either column.

begin;

-- ===========================================================================
-- users.language (FR-LOC-001b)
-- ===========================================================================
alter table users
    add column language text
        constraint users_language check (language in ('en', 'nl'));

comment on column users.language is
    'FR-LOC-001b. The UI language THIS PERSON reads, independent of their '
    'organization and of every other user in it. NULL means never chosen: '
    'IAM-010g seeds it from the pre-login device choice at first login, and '
    'must be able to tell that apart from a deliberate choice of Dutch.';

-- Mirrored by Language in apps/api/src/api/i18n/language.py and by
-- SUPPORTED_LANGUAGES in packages/i18n/src/language.ts. A third language is a
-- migration here, an enum member there, and a full set of translations -
-- which is the order those have to happen in, because FR-LOC-001 makes a
-- missing translation a release blocker rather than a fallback.

-- ===========================================================================
-- administration.formatting_locale (FR-LOC-002)
-- ===========================================================================
-- One permitted value today. That is not an oversight: LEDGR serves Dutch
-- SMBs, and a second entry would be a guess about a jurisdiction nobody has
-- specified a chart of accounts, a VAT ruleset or a filing channel for.
--
-- FR-LOC-005 ("adding a jurisdiction without forking the codebase") is served
-- by WHERE this lives rather than by how many values it allows: the locale is
-- a column on the administration, read by both formatting implementations
-- from a table keyed on it, so adding one is a value here plus a LocaleSpec on
-- each side - not a branch anywhere.
alter table administration
    add column formatting_locale text not null default 'nl-NL'
        constraint administration_formatting_locale
            check (formatting_locale in ('nl-NL'));

comment on column administration.formatting_locale is
    'FR-LOC-002. How numbers, dates and money are written for THESE BOOKS, for '
    'every reader regardless of their own UI language - so an amount reads '
    'identically to a Dutch bookkeeper and an English-speaking owner. Mirrored '
    'by LOCALES in api.i18n.formatting and packages/i18n/src/locale.ts.';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------
-- None needed. 0001 and 0003 granted SELECT/INSERT/UPDATE on `administration`
-- and `users` to ledgr_app at table level, which covers columns added later,
-- and `users` carries no RLS (users are global - 0003) while
-- `administration`'s existing policies are row predicates that do not
-- enumerate columns. Both new columns are therefore reachable exactly as far
-- as the rows holding them already were, which is the intended answer:
-- language is readable by the user it belongs to, and an administration's
-- locale by anyone who can already see the administration.
--
-- Stated rather than left silent, because "no grants needed" and "grants
-- forgotten" look identical in a diff.

commit;

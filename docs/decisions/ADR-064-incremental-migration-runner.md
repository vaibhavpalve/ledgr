# ADR-064: A tracked migration runner over the existing SQL files, not Alembic

- **Status**: Accepted
- **Date**: 2026-09-18
- **Serves**: `NFR-044` (migrations backward compatible, reversible, no downtime)
- **Amends**: [ADR-002](ADR-002-tenancy-schema.md) / `migrations/0001_tenancy_core.sql`, whose role
  table describes `ledgr_migrator` as the role that "runs migrations" — see *Which role actually
  applies a migration* below

## Context

`scripts/bootstrap_production_db.py` sets up an empty database by applying every file in
`migrations/` once, and refuses to run twice. Its own docstring named what was missing:

> There is no tracked, incremental migration runner yet [...] Applying a NEW migration to an
> already-live production database is separate, not-yet-built tooling.

So a database that had been bootstrapped had no supported way to receive migration 0051. Nothing
recorded which files a given database had, which means drift between environments was not merely
possible but undetectable — the failure mode that makes "just apply it by hand" untenable past
the first environment.

## Decision

`scripts/migrate.py` applies any numbered `.sql` file a database has not recorded, in filename
order, and records each one in an `app.schema_migrations` ledger (`filename` primary key,
`checksum`, `applied_at`). `bootstrap_production_db.py` now writes the same ledger for the files
it applies, so a freshly bootstrapped database is tracked from the start rather than needing to
be adopted immediately afterwards.

The existing hand-written SQL files are unchanged. They remain the source of truth; this adds a
ledger and a runner around them, nothing more.

### Which role actually applies a migration

`0001_tenancy_core.sql` describes `ledgr_migrator` as "owns every table. Runs migrations (CI/ops
only)". The first half is true and the second is not achievable as the migrations are written:

- 0001 itself does `create extension`, `create role` and `create schema`
- 0020, 0028, 0031, 0032 and 0040 each `create schema`
- `ledgr_migrator` is `nosuperuser nocreatedb nocreaterole`, with no `CREATE` grant on schema
  `app` or on the database

0001's own header already says the file "must be applied by a role with permission to CREATE
ROLE, CREATE SCHEMA and CREATE EXTENSION (typically a one-off admin/bootstrap connection, not
any role used day to day)" — so the header and the role table disagree, and the header is
correct. The runner therefore uses `DATABASE_ADMIN_URL`, the same connection
`bootstrap_production_db.py` uses. `ledgr_migrator` still ends up owning every table, because
the migration files transfer ownership themselves with `alter table ... owner to ledgr_migrator`.

This ADR records that as a correction to the role table's wording, not a change in behaviour:
nothing ever ran migrations as `ledgr_migrator`.

### Apply and record are deliberately not one transaction

45 of the 50 migration files open `begin;` and close `commit;` themselves. A transaction opened
by the runner would be committed by the file partway through, leaving the remainder of that file
running outside any transaction — worse than not opening one. The file's own transaction stays
the unit of atomicity.

The consequence is a window: if the process dies after a file commits but before its ledger row
is written, that migration is applied but unrecorded. That window fails **loudly** — the next run
re-attempts the same file and Postgres refuses it ("relation already exists") rather than
skipping or half-applying it — and `--baseline` is the documented way out.

### Checksums, and refusing rather than guessing

Each recorded migration carries a SHA-256 of its text (read as `utf-8-sig`, so a BOM written by
a Windows editor is not mistaken for an edit). Before applying anything, the runner compares
every already-applied file against its recorded checksum and stops on any mismatch: an applied
migration is history, editing one does not change the database that already ran it, and the
mismatch should say so rather than pass unnoticed.

The runner refuses, rather than guessing, in four situations — no `app` schema (never
bootstrapped), an `app` schema with no ledger (bootstrapped before this existed; needs
`--baseline`), a checksum mismatch, and a ledger naming files that no longer exist. A
`pg_advisory_lock` serialises concurrent runners so two deploys cannot apply the same file at
once.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Alembic | The standard choice, and it would bring `downgrade` support this runner does not have. Rejected because its autogenerate — the reason most teams adopt it — cannot see row-level security policies, `FORCE ROW LEVEL SECURITY`, grants, `SECURITY DEFINER` functions or the append-only triggers, which is most of what these migrations contain. Every migration would still be hand-written, so the cost would be converting or wrapping 50 existing files to gain a `downgrade` path that ADR-030's append-only tables cannot honestly offer anyway. |
| Keep applying migrations by hand through a tunnel, with a runbook | Zero code, and it is what happens today. Nothing records what was applied, so environment drift stays undetectable and a migration interrupted halfway leaves no trace of how far it got. That is the gap this ADR exists to close. |
| Extend `bootstrap_production_db.py` with an incremental mode | Rejected on the reasoning that script already gives for not being `bootstrap_test_db.py` with a flag: its refusal to touch a database that has an `app` schema is its main safety property, and a mode that inverts exactly that refusal belongs in a different file with a different name. |
| Record migrations without checksums | Simpler, and enough to know what ran. Would not catch a migration edited after it was applied, which is the specific way a tracked history quietly stops describing the database. |

## Consequences

- **The existing production database needs `--baseline` once.** It was bootstrapped before this
  ledger existed, so it has an `app` schema and no `app.schema_migrations`. The runner refuses it
  by design and prints the command. Until that is run, `migrate.py` will not apply anything.
- **No `downgrade`.** Rolling a migration back means writing a forward migration that reverses
  it — which is what the ledger's append-only tables (ADR-030, CMP-009) require in any case, and
  what NFR-044's "reversible" means here. An operator who needs a rollback writes SQL; the runner
  has no opinion.
- **Migration files are immutable once applied**, now enforced rather than assumed. Editing one
  that any environment has already run makes every subsequent run of that environment fail until
  the edit is reverted or the row is re-baselined.
- **Migrations still require an admin connection**, so applying them is not something the running
  API can do to itself. Deployment remains a two-step operation — apply migrations, then deploy
  the code — which is what NFR-044's "no downtime" requires anyway (a backward-compatible
  migration applied before the code that needs it).
- **Not automated in the deploy pipeline.** The runner is a script an operator or CI step calls;
  wiring it into Railway's deploy as a release phase is deliberately left as a separate decision,
  since running migrations automatically on every deploy is a meaningful change to the blast
  radius of a bad push.

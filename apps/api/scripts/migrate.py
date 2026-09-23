"""Applies migrations that a database has not had yet, and records what it
applied - the incremental runner `bootstrap_production_db.py` names as missing
in its own docstring (NFR-044).

`bootstrap_production_db.py` remains what it was: first-time setup of an empty
database, refusing to run twice. This is the other half - the thing you run on
an already-live database when a new numbered file lands in migrations/.

Usage:
    DATABASE_ADMIN_URL=postgresql://... uv run python scripts/migrate.py
    DATABASE_ADMIN_URL=postgresql://... uv run python scripts/migrate.py --dry-run
    DATABASE_ADMIN_URL=postgresql://... uv run python scripts/migrate.py --baseline

--- Why an admin connection and not ledgr_migrator ---

`ledgr_migrator` owns every table, but it does not have the privileges these
files need: 0001 creates roles and an extension, and 0020/0028/0031/0032/0040
each CREATE SCHEMA, none of which `nosuperuser nocreatedb` with no CREATE grant
on the database can do. 0001's own header says as much. So migrations run on
the same admin connection `bootstrap_production_db.py` uses, and the tables
they create still end up owned by `ledgr_migrator` because the files say
`alter table ... owner to ledgr_migrator` themselves.

--- Why apply and record are not one transaction ---

45 of the 50 migration files open `begin;` and close `commit;` themselves, so
a transaction opened out here would be committed by the file halfway through
and leave the rest running outside it. The file's own transaction is the unit
of atomicity, which means there is a window - process dies after a file
commits, before its row is written - where a migration is applied but
unrecorded.

That window fails LOUDLY rather than silently: the next run tries the same file
again and Postgres refuses it ("relation already exists"), rather than skipping
it or half-applying it. `--baseline` is the way out, and the reason it exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

#: Serialises concurrent runners - two deploys applying the same pending file
#: at once would have one of them fail somewhere mid-file. An advisory lock is
#: held for the session and released when the connection closes, including
#: when the process is killed, so a crashed run does not wedge the next one.
_ADVISORY_LOCK_KEY = 0x1EDA_2025_0918

#: Public because bootstrap_production_db.py creates the same table when it
#: sets a database up, so a freshly bootstrapped database is already tracked
#: and this runner works against it immediately. One definition, not two that
#: could drift.
TRACKING_TABLE_DDL = """
create table if not exists app.schema_migrations (
    -- The filename, not a parsed integer: it is what an operator greps for,
    -- and it catches a file renamed after it was applied.
    filename    text        primary key,
    -- Detects a migration edited after it ran. An applied migration is
    -- history; changing one does not change the database that already has it,
    -- and the mismatch says so instead of the edit passing unnoticed.
    checksum    text        not null,
    applied_at  timestamptz not null default now()
)
"""


def checksum(migration: Path) -> str:
    # utf-8-sig for the same reason bootstrap_test_db.py reads that way: a
    # Windows editor's BOM would otherwise change the hash of a file whose SQL
    # nobody touched.
    return hashlib.sha256(migration.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def accepted_checksums(migration: Path) -> set[str]:
    """Every checksum that means "this file is unchanged".

    Rows recorded on 2026-09-19..20 (0052-0061) hash the file's bytes with the
    CRLF it had on a Windows checkout, whereas `checksum` hashes the
    newline-normalised text. Same SQL, different digest - flagging that as an
    edit made production refuse every later migration. A line-ending difference
    is not an edit, so the raw-bytes digest is accepted as well.
    """
    return {checksum(migration), hashlib.sha256(migration.read_bytes()).hexdigest()}


def _migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def _adopt_existing(conn: asyncpg.Connection, migrations: list[Path]) -> None:
    """`--baseline`: record every migration as applied WITHOUT running any.

    For the database that was set up before this runner existed (by
    bootstrap_production_db.py, which applied files without recording them),
    and for recovering the window described in the module docstring.
    """
    for migration in migrations:
        await conn.execute(
            "insert into app.schema_migrations (filename, checksum) values ($1, $2) "
            "on conflict (filename) do nothing",
            migration.name,
            checksum(migration),
        )
    print(f"baselined {len(migrations)} migration(s) as already applied", file=sys.stderr)


async def main() -> None:
    admin_dsn = os.environ["DATABASE_ADMIN_URL"]
    baseline = "--baseline" in sys.argv
    dry_run = "--dry-run" in sys.argv

    migrations = _migrations()
    if not migrations:
        print(f"no migrations found in {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute("select pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)

        bootstrapped = await conn.fetchval(
            "select exists (select 1 from information_schema.schemata where schema_name = 'app')"
        )
        if not bootstrapped:
            print(
                "this database has no 'app' schema - it has never been bootstrapped. "
                "Run scripts/bootstrap_production_db.py first; this runner applies "
                "migrations to a database that already exists.",
                file=sys.stderr,
            )
            sys.exit(1)

        tracked_before = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_schema = 'app' and table_name = 'schema_migrations')"
        )
        await conn.execute(TRACKING_TABLE_DDL)

        if baseline:
            await _adopt_existing(conn, migrations)
            return

        if not tracked_before:
            # Bootstrapped before this runner existed: every file has almost
            # certainly been applied, but nothing recorded it. Applying them
            # again would fail partway through and leave a mess, so refuse and
            # say what to do instead of guessing.
            print(
                "this database predates migration tracking: it has an 'app' schema but no "
                "app.schema_migrations, so which files it already has is unknown. If it was "
                "set up by bootstrap_production_db.py, every migration is applied - adopt "
                "them with:\n\n    python scripts/migrate.py --baseline\n",
                file=sys.stderr,
            )
            sys.exit(1)

        applied = {
            row["filename"]: row["checksum"]
            for row in await conn.fetch("select filename, checksum from app.schema_migrations")
        }

        # Checked before anything is applied: a tampered history is a reason to
        # stop and look, not to carry on adding to it.
        drifted = [
            m.name
            for m in migrations
            if m.name in applied and applied[m.name] not in accepted_checksums(m)
        ]
        if drifted:
            print(
                "these migrations were edited after they were applied, so this database does "
                "not contain what the files now say:\n  " + "\n  ".join(drifted),
                file=sys.stderr,
            )
            sys.exit(1)

        missing = sorted(set(applied) - {m.name for m in migrations})
        if missing:
            print(
                "the database records migrations that no longer exist as files (renamed or "
                "deleted?):\n  " + "\n  ".join(missing),
                file=sys.stderr,
            )
            sys.exit(1)

        pending = [m for m in migrations if m.name not in applied]
        if not pending:
            print(f"up to date - {len(applied)} migration(s) applied", file=sys.stderr)
            return

        if dry_run:
            print("would apply:", file=sys.stderr)
            for migration in pending:
                print(f"  {migration.name}", file=sys.stderr)
            return

        for migration in pending:
            print(f"applying {migration.name}", file=sys.stderr)
            await conn.execute(migration.read_text(encoding="utf-8-sig"))
            await conn.execute(
                "insert into app.schema_migrations (filename, checksum) values ($1, $2)",
                migration.name,
                checksum(migration),
            )

        print(f"applied {len(pending)} migration(s)", file=sys.stderr)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

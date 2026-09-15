"""One-time setup of a brand-new production Postgres (e.g. Railway's managed
Postgres, ADR-062): applies every migration in order and sets a login
password for ledgr_app, ledgr_migrator and ledgr_ops.

Deliberately NOT bootstrap_test_db.py reused with a flag. That script drops
and recreates public/app/ledger unconditionally (safe only because it only
ever targets a disposable test database) and grants ledgr_ops to ledgr_app
via SET ROLE - a test-only bridge its own comment names as "itself a
tenant-isolation defect" if it ever reached production, since it would let
the ordinary application role escalate to the BYPASSRLS role on demand.
This script does neither: it refuses to run at all against a database that
already has an `app` schema, rather than assuming it is safe to reset, and
it never grants ledgr_ops to anything.

There is no tracked, incremental migration runner yet - this script is
first-time bootstrap only. Applying a NEW migration to an already-live
production database is separate, not-yet-built tooling; running this
script a second time against a bootstrapped database is refused rather
than attempted.

Usage:
    DATABASE_ADMIN_URL=postgresql://postgres:<railway-supplied-password>@<host>:<port>/railway \
    LEDGR_APP_PASSWORD=<generate one> \
    LEDGR_MIGRATOR_PASSWORD=<generate one> \
    LEDGR_OPS_PASSWORD=<generate one> \
    uv run python scripts/bootstrap_production_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def main() -> None:
    admin_dsn = os.environ["DATABASE_ADMIN_URL"]
    app_password = os.environ["LEDGR_APP_PASSWORD"]
    migrator_password = os.environ["LEDGR_MIGRATOR_PASSWORD"]
    ops_password = os.environ["LEDGR_OPS_PASSWORD"]

    conn = await asyncpg.connect(admin_dsn)
    try:
        already_bootstrapped = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'app')"
        )
        if already_bootstrapped:
            print(
                "refusing: schema 'app' already exists - this script is for a brand-new "
                "database only. Applying a new migration to an already-live database needs "
                "its own runner, not this script.",
                file=sys.stderr,
            )
            sys.exit(1)

        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            print(f"applying {migration.name}", file=sys.stderr)
            # utf-8-sig, not utf-8 - see bootstrap_test_db.py's identical
            # comment: a Windows editor's BOM turns into a Postgres syntax
            # error that points at the first statement, not the three bytes
            # actually at fault.
            sql = migration.read_text(encoding="utf-8-sig")
            await conn.execute(sql)

        for role, password in (
            ("ledgr_app", app_password),
            ("ledgr_migrator", migrator_password),
            ("ledgr_ops", ops_password),
        ):
            escaped = password.replace("'", "''")
            await conn.execute(f"ALTER ROLE {role} WITH LOGIN PASSWORD '{escaped}'")
            print(f"{role} password set", file=sys.stderr)

        print(
            "done. ledgr_app's password is what DATABASE_URL connects with; "
            "ledgr_ops's is what OPS_DATABASE_URL (the scheduled batch jobs) connects with. "
            "Neither role was granted the other's privileges.",
            file=sys.stderr,
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Applies every migration to a test database and sets a login password for
ledgr_app, so tests can connect as it — exactly the role the API uses in
production, which is what makes the tenant-isolation tests in
tests/integration/ a real exercise of RLS rather than a mock of it.

Test-only tooling: the migration itself never sets a password (SEC-025 —
secrets are never in source control), so this script exists specifically to
bridge that gap for an ephemeral test database. It is never run against
anything but a disposable test Postgres.

Usage:
    TEST_DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/ledgr_test \
    TEST_LEDGR_APP_PASSWORD=test-password \
    uv run python scripts/bootstrap_test_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def main() -> None:
    admin_dsn = os.environ["TEST_DATABASE_ADMIN_URL"]
    app_password = os.environ["TEST_LEDGR_APP_PASSWORD"]
    # Test-only, and optional: only test_audit_tamper_evidence.py's
    # migrator-role battery needs a real login for ledgr_migrator (see its
    # own comment on why admin+SET ROLE cannot stand in for one - a
    # superuser session can SET ROLE to any role regardless of membership,
    # which makes that proxy unable to test role-escalation attempts
    # specifically). Falls back to app_password so an environment that never
    # set this separately still gets a login rather than the KeyError
    # TEST_LEDGR_APP_PASSWORD gets; both are throwaway test credentials.
    migrator_password = os.environ.get("TEST_LEDGR_MIGRATOR_PASSWORD", app_password)

    conn = await asyncpg.connect(admin_dsn)
    try:
        # This script always starts from a clean slate — safe and intended
        # because it only ever runs against a disposable test database.
        # Without this, re-running it locally against an already-bootstrapped
        # DB would fail on the first `CREATE TABLE` (migrations here aren't
        # yet tracked/idempotent the way a real migration runner would make
        # them).
        print("resetting public/app/ledger schemas", file=sys.stderr)
        # Every schema the migrations create: `app` (0001) and `ledger` (0020),
        # plus `public`. Dropping only two of the three is not a clean slate -
        # it left the ledger's functions and its integrity_finding type behind,
        # and the second run of this script then failed on `type
        # "integrity_finding" already exists`. The migrations guard their
        # CREATE SCHEMA with IF NOT EXISTS, which hid the omission until a
        # migration created something inside `ledger` that has no such guard.
        await conn.execute("DROP SCHEMA IF EXISTS ledger CASCADE")
        await conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")

        # Put back the ACL initdb gives `public`, which DROP took with it.
        #
        # A schema created by CREATE SCHEMA is owned by the creating role and
        # grants nothing to anyone else. The `public` schema initdb makes is
        # different: it is owned by pg_database_owner and grants USAGE to
        # PUBLIC. Recreating it without restoring that leaves every
        # non-superuser role in this system - ledgr_app, ledgr_migrator,
        # ledgr_ops - unable to so much as look inside the schema every table
        # lives in.
        #
        # The migrations themselves run as a superuser and do not notice. What
        # notices is the first referential-integrity check on a table owned by
        # ledgr_migrator, because an RI trigger runs as the referencing
        # table's owner: `permission denied for schema public`, reported
        # against a SELECT nobody wrote. 0010's seed data is where that lands.
        #
        # CREATE is deliberately not granted back to PUBLIC. Pre-15 Postgres
        # did grant it, and nothing here needs it: the migrations create their
        # objects as the superuser and hand ownership over afterwards.
        await conn.execute("ALTER SCHEMA public OWNER TO pg_database_owner")
        await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")

        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            print(f"applying {migration.name}", file=sys.stderr)
            # utf-8-sig, not utf-8: an editor on Windows can save a .sql file
            # with a byte-order mark, and Postgres answers a leading U+FEFF
            # with `syntax error at or near ""` - which points at the first
            # statement and says nothing about the three bytes in front of it.
            # utf-8-sig strips a BOM when there is one and is a no-op when
            # there is not. tests/ledger/test_bounded_context.py reads source
            # files the same way, for the same reason.
            sql = migration.read_text(encoding="utf-8-sig")
            await conn.execute(sql)

        # app_password comes from a CI secret / local env var, not request
        # input — still escaped defensively since ALTER ROLE's PASSWORD
        # clause takes a literal, not a bind parameter.
        escaped_password = app_password.replace("'", "''")
        await conn.execute(f"ALTER ROLE ledgr_app WITH LOGIN PASSWORD '{escaped_password}'")
        print("ledgr_app password set", file=sys.stderr)

        escaped_migrator_password = migrator_password.replace("'", "''")
        await conn.execute(f"ALTER ROLE ledgr_migrator WITH PASSWORD '{escaped_migrator_password}'")
        print("ledgr_migrator password set", file=sys.stderr)

        # Test-only. Two suites open a REAL ledgr_ops connection through
        # OPS_DATABASE_URL (get_ops_engine): the document retention sweep and the VAT
        # ruleset loader. SET ROLE from ledgr_app cannot stand in for it, since
        # they call `get_ops_engine()` directly. Production never sets a password
        # here; ledgr_ops has no login there.
        ops_password = os.environ.get("TEST_LEDGR_OPS_PASSWORD", app_password)
        escaped_ops_password = ops_password.replace("'", "''")
        await conn.execute(f"ALTER ROLE ledgr_ops WITH LOGIN PASSWORD '{escaped_ops_password}'")
        print("ledgr_ops login enabled (test-only)", file=sys.stderr)

        # Test-only bridge, the same kind this script already is for
        # ledgr_app's password: several integration tests exercise
        # ledgr_ops-only code paths (BYPASSRLS reads, SECURITY DEFINER
        # batch functions like app.purge_expired_idempotency_keys) by
        # connecting once as ledgr_app (DATABASE_URL) and running
        # `SET ROLE ledgr_ops` mid-session, rather than opening a second
        # connection with its own credentials. `SET ROLE` to a non-superuser
        # target requires the CURRENT role to already be a member of it -
        # nothing in the migrations grants that (deliberately: ledgr_app
        # gaining ledgr_ops's cross-tenant BYPASSRLS privileges in
        # PRODUCTION would itself be a tenant-isolation defect), so it has
        # to be granted here, exactly where the equivalent problem for
        # ledgr_app's password is already solved.
        #
        # WITH INHERIT FALSE (PG16): membership alone is enough for SET
        # ROLE, which only checks membership - but plain membership also
        # auto-inherits the target's privileges with no SET ROLE at all,
        # which let ledgr_app call app.purge_expired_idempotency_keys()
        # directly and silently, defeating
        # test_the_application_role_cannot_purge_other_tenants_keys (found
        # by running that test against this grant for the first time - it
        # expects that call to be denied to ledgr_app specifically). INHERIT
        # FALSE keeps the SET ROLE bridge these tests need while closing
        # that hole: ledgr_ops's privileges apply only for the duration of
        # an explicit SET ROLE, never automatically.
        await conn.execute("GRANT ledgr_ops TO ledgr_app WITH INHERIT FALSE")
        print("ledgr_app may SET ROLE ledgr_ops (test-only, non-inheriting)", file=sys.stderr)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

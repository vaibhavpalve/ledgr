"""Runs the ledger integrity job (NFR-033) and reports what it found.

    NFR-033  A nightly integrity job verifies debit/credit balance, sub-ledger
             to control account agreement, and numbering continuity, alerting
             on any deviation.

The checks themselves are migration 0023's `ledger.integrity_*` functions and
the reporting is api.ledger.integrity; this is the way to run them. It is the
same code path nightly and on demand - a scheduled sweep is this script with
no arguments, which is what stops the scheduled path being the one nobody has
ever watched run.

--- Usage ---

    # every tenant, as ledgr_ops (the nightly sweep)
    uv run python scripts/verify_ledger_integrity.py

    # one administration, machine-readable
    uv run python scripts/verify_ledger_integrity.py \\
        --administration 0f8c... --json

    # through the application role, scoped to one tenant by RLS
    uv run python scripts/verify_ledger_integrity.py \\
        --app-connection --organization 0f8c...

    make verify-ledger-integrity
    make verify-ledger-integrity ARGS="--administration 0f8c... --json"

--- Exit codes ---

    0  every check passed, over a scope that contained something
    1  at least one deviation - the ledger contradicts FR-GL-001, FR-GL-006
       or FR-GL-013 somewhere
    2  the job could not be run (no connection string, bad arguments)
    3  nothing was examined: no administration was visible to this
       connection, so there were no findings AND no verification. Pass
       --allow-empty where an empty database is the expected state.

3 is separate from 0 on purpose, and it is the reason this script is usable as
a test oracle. Every check reports a problem by returning a row, so a run that
can see nothing returns nothing and looks exactly like a perfect ledger. A run
as `ledgr_app` with no tenant context set is precisely that case: RLS fails
closed, zero rows come back, and without this distinction the oracle would
certify a database it never read.

--- Which connection ---

Default is `ledgr_ops` via OPS_DATABASE_URL. That role holds BYPASSRLS (0002)
and SELECT-only on the posting tables (0020), which is the exact shape this
job wants: one pass across every tenant, and no privilege to change what it is
checking (CMP-009).

`--app-connection` uses DATABASE_URL as `ledgr_app` instead, where RLS scopes
the answer to whatever `--organization` names. That is the path an
administrator's own "check my books" would take, and the path a test uses when
it has already seeded a tenant through the application connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession  # noqa: E402

from api.ledger.integrity import (  # noqa: E402
    IntegrityCheck,
    IntegrityReport,
    LedgerIntegrityJob,
    LoggingIntegrityAlerter,
)

# The composition root for this job, and the only place outside the ledger
# context that names the SQL implementation. `LedgerIntegrityJob` takes the
# `IntegrityRepository` protocol precisely so that this line is the whole
# coupling: everything else in this file talks to the public module.
from api.ledger.integrity_repository import SqlIntegrityRepository  # noqa: E402

EXIT_INTACT = 0
EXIT_DEVIATIONS = 1
EXIT_CANNOT_RUN = 2
EXIT_EXAMINED_NOTHING = 3


async def run_report(
    engine: AsyncEngine,
    *,
    administration_id: uuid.UUID | None,
    organization_id: uuid.UUID | None,
) -> IntegrityReport:
    """One connection, one read-only transaction, no commit.

    The tenant context is set with `set_config(..., true)` - transaction-local,
    the same call api.db.get_db_session makes - so it cannot leak onto the next
    user of a pooled connection. For a `ledgr_ops` sweep it is simply absent:
    BYPASSRLS is what makes that run cross-tenant, and setting an organization
    would narrow it to one.
    """
    async with engine.connect() as conn:
        if organization_id is not None:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization_id)},
            )
        job = LedgerIntegrityJob(
            SqlIntegrityRepository(AsyncSession(bind=conn)),
            LoggingIntegrityAlerter(),
        )
        return await job.run(administration_id=administration_id)


def render(report: IntegrityReport) -> str:
    """Human output. Grouped by check, with every check named even when it
    found nothing - "numbering: none" is evidence the numbering check ran,
    where an omitted section is evidence of nothing.
    """
    scope = report.scope
    lines = [
        report.summary(),
        (
            f"scope: {scope.administrations} administration(s), "
            f"{scope.journals} journal(s), {scope.accounts} account(s), "
            f"{scope.entries} entries, {scope.lines} lines"
        ),
        f"checked at {report.checked_at.isoformat()}",
    ]

    grouped = report.by_check()
    for check in IntegrityCheck:
        found = grouped[check]
        lines.append("")
        if not found:
            lines.append(f"{check.value}: none")
            continue
        lines.append(f"{check.value}: {len(found)} deviation(s)")
        for deviation in found:
            lines.append(f"  [{deviation.deviation}] ({deviation.requirement}) {deviation.summary}")
            lines.append(
                f"      {deviation.subject_type} {deviation.subject_id} "
                f"in administration {deviation.administration_id}"
            )
            if deviation.detail:
                lines.append(f"      detail: {json.dumps(deviation.detail, sort_keys=True)}")
    return "\n".join(lines)


def _engine(use_app_connection: bool) -> AsyncEngine:
    # Imported here rather than at module scope: api.db builds the application
    # engine at import time from settings, and a script that only ever uses the
    # ops connection has no reason to construct it.
    from api.db import engine as app_engine
    from api.db import get_ops_engine

    return app_engine if use_app_connection else get_ops_engine()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify ledger integrity (NFR-033).",
        epilog=(
            "Exit codes: 0 intact, 1 deviations found, 2 could not run, "
            "3 nothing examined (see --allow-empty)."
        ),
    )
    parser.add_argument(
        "--administration",
        type=uuid.UUID,
        default=None,
        help="check one administration instead of everything visible",
    )
    parser.add_argument(
        "--organization",
        type=uuid.UUID,
        default=None,
        help=(
            "set tenant context for the run. Required with --app-connection: "
            "RLS fails closed without it and the run would examine nothing."
        ),
    )
    parser.add_argument(
        "--app-connection",
        action="store_true",
        help=(
            "connect through DATABASE_URL as ledgr_app (RLS-scoped) instead of "
            "OPS_DATABASE_URL as ledgr_ops (cross-tenant)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON instead of text",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "treat a run that examined no administration as a pass. Only "
            "correct where an empty database is genuinely expected."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # Refused rather than run, because the run would "succeed" - RLS would hide
    # every row, the job would find nothing, and the operator would read that
    # as a clean ledger. --allow-empty does not lift this: it says an empty
    # scope is expected, not that an unscoped app connection is meaningful.
    if args.app_connection and args.organization is None:
        print(
            "--app-connection needs --organization: as ledgr_app, row-level "
            "security returns nothing without tenant context, so the run would "
            "examine nothing and report no deviations.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    try:
        engine = _engine(args.app_connection)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN

    try:
        report = await run_report(
            engine,
            administration_id=args.administration,
            organization_id=args.organization,
        )
    finally:
        # The application engine is a module-level singleton shared with the
        # rest of the process; the ops engine is built per call and is this
        # script's to close.
        if not args.app_connection:
            await engine.dispose()

    print(json.dumps(report.as_dict(), indent=2) if args.json else render(report))

    if report.examined_nothing and not args.allow_empty:
        print(
            "nothing was examined - no administration was visible to this "
            "connection. This is not a pass; pass --allow-empty if an empty "
            "scope is expected.",
            file=sys.stderr,
        )
        return EXIT_EXAMINED_NOTHING
    return EXIT_DEVIATIONS if report.deviations else EXIT_INTACT


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

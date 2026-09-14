"""Runs the document retention sweep (PRIV-030) and reports what it found.

    PRIV-030  Retention is enforced by automated jobs, not manual process,
              with an exception report for records that failed to expire.

The rule and the exception report both live in api.documents.retention_job;
this is the way to run them - the same command nightly and on demand, which is
what stops the scheduled path being the one nobody has ever watched run (see
scripts/verify_ledger_integrity.py, which this mirrors).

--- Usage ---

    # every tenant, as ledgr_ops (the nightly sweep)
    uv run python scripts/enforce_document_retention.py

    # one administration, machine-readable
    uv run python scripts/enforce_document_retention.py \\
        --administration 0f8c... --json

    make enforce-document-retention
    make enforce-document-retention ARGS="--administration 0f8c... --json"

There is no ledgr_app / RLS-scoped mode, unlike verify_ledger_integrity.py's
--app-connection: that job only reads; this one deletes, and migration 0031
grants DELETE on `document` to ledgr_ops alone - ledgr_app cannot express the
statement at all (FR-DOC-002's "not deletable by users"), so a ledgr_app
connection would fail every deletion with permission denied regardless of
how expired the row is. --administration scopes an ops-connected run to one
tenant without needing a second, weaker connection mode to do it.

--- Exit codes ---

    0  clean - every document past retention was removed, or none were due
    1  at least one exception - a document past retention could not be
       removed (see the report for why)
    2  the job could not be run (no connection string, bad arguments)

There is deliberately no exit code for "nothing examined" the way
verify_ledger_integrity.py has one: an empty result here is documents.
expired() returning no rows, which is the ordinary, expected state of a
healthy archive on most nights, not a sign the connection could not see
anything.
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

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession  # noqa: E402

from api.documents.retention_job import (  # noqa: E402
    DocumentRetentionSweepJob,
    LoggingRetentionSweepAlerter,
    RetentionSweepReport,
)
from api.documents.retention_repository import SqlDocumentRetentionRepository  # noqa: E402

EXIT_CLEAN = 0
EXIT_EXCEPTIONS = 1
EXIT_CANNOT_RUN = 2


async def run_sweep(
    engine: AsyncEngine,
    *,
    administration_id: uuid.UUID | None,
) -> RetentionSweepReport:
    """One connection, one transaction, committed at the end.

    Unlike the read-only ledger integrity job, this one writes - so unlike
    that script's `run_report`, this commits. Every DELETE it issues has
    already passed `documents.expired()`'s `blocking_link_count` check, so
    nothing here is expected to fail; a genuine failure rolls the whole sweep
    back rather than leaving some tenants swept and others not.
    """
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn)
        job = DocumentRetentionSweepJob(
            SqlDocumentRetentionRepository(session), LoggingRetentionSweepAlerter()
        )
        report = await job.run(administration_id=administration_id)
        await session.commit()
        return report


def render(report: RetentionSweepReport) -> str:
    lines = [
        report.summary(),
        f"examined {report.examined} document(s) past retention",
        f"checked at {report.checked_at.isoformat()}",
    ]
    if report.exceptions:
        lines.append("")
        lines.append(f"exceptions ({len(report.exceptions)}):")
        for exc in report.exceptions:
            lines.append(f"  document {exc.document_id} in administration {exc.administration_id}")
            lines.append(f"      {exc.reason}")
    return "\n".join(lines)


async def main() -> int:
    from api.db import get_ops_engine

    parser = argparse.ArgumentParser(
        description="Enforce document retention (PRIV-030).",
        epilog="Exit codes: 0 clean, 1 exceptions found, 2 could not run.",
    )
    parser.add_argument(
        "--administration",
        type=uuid.UUID,
        default=None,
        help="sweep one administration instead of everything visible",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    try:
        engine = get_ops_engine()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN

    try:
        report = await run_sweep(engine, administration_id=args.administration)
    finally:
        await engine.dispose()

    print(json.dumps(report.as_dict(), indent=2) if args.json else render(report))
    return EXIT_EXCEPTIONS if report.exceptions else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

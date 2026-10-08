"""Sync every linked bank feed that is due (ADR-108). Run once a day (a Railway cron service):

    DATABASE_URL=postgresql+asyncpg://ledgr_app:...@.../ledgr \\
    OPS_DATABASE_URL=postgresql+asyncpg://ledgr_ops:...@.../ledgr \\
    BANK_FEED_PROVIDER=gocardless GOCARDLESS_SECRET_ID=... GOCARDLESS_SECRET_KEY=... \\
        uv run python scripts/sync_bank_feeds.py [--dry-run]

Both database URLs: ledgr_ops only reads which connections are due, and every sync writes as
ledgr_app scoped to its own organization - see api.bank.feed_job.

Safe to run any number of times: lines de-duplicate on the import's content hash.

Exit codes: 0 all synced (or nothing due), 1 some failed (retried next run), 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

EXIT_CLEAN = 0
EXIT_FAILURES = 1
EXIT_CANNOT_RUN = 2


async def main() -> int:
    from api.bank.feed_job import sync_due_feeds
    from api.bank.feed_routes import configured_provider, feed_settings
    from api.db import engine, get_ops_engine

    parser = argparse.ArgumentParser(description="Sync linked bank feeds (ADR-108).")
    parser.add_argument("--dry-run", action="store_true", help="list what is due, sync nothing")
    args = parser.parse_args()

    provider = configured_provider()
    if not provider.is_configured:
        print("cannot run: no bank feed provider configured (BANK_FEED_PROVIDER)", file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        ops = get_ops_engine()
    except RuntimeError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    report = await sync_due_feeds(
        ops_engine=ops,
        app_engine=engine,
        provider=provider,
        settings=feed_settings(),
        now=datetime.now(UTC),
        dry_run=args.dry_run,
    )
    await ops.dispose()
    await engine.dispose()

    for line in report.details:
        print(line)
    print(
        f"{report.due} due, {report.synced} synced, {report.new_lines} new lines, "
        f"{report.failed} failed{' (dry run)' if args.dry_run else ''}"
    )
    return EXIT_FAILURES if report.failed else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

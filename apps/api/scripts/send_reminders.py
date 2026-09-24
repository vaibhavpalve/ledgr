"""Send today's reminder e-mails: BTW returns coming due or overdue, receipts waiting (ADR-090).

Run once a day (a Railway cron service, 07:00 Europe/Amsterdam), as ledgr_ops:

    OPS_DATABASE_URL=postgresql+asyncpg://ledgr_ops:...@.../ledgr \\
        uv run python scripts/send_reminders.py [--dry-run] [--today YYYY-MM-DD]

Safe to run any number of times: every reminder is sent at most once per person (reminder_log's
unique key), so a second run the same day sends nothing. --dry-run reports what would be sent and
writes nothing. The mail goes through the configured sender (EMAIL_PROVIDER); with the collecting
provider nothing leaves the process.

Exit codes: 0 all sent (or nothing due), 1 some sends failed (retried next run), 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from api.mail.outbox import get_email_sender  # noqa: E402
from api.reminders.service import RunReport, run_reminders  # noqa: E402

EXIT_CLEAN = 0
EXIT_FAILURES = 1
EXIT_CANNOT_RUN = 2


async def main() -> int:
    from api.db import get_ops_engine

    parser = argparse.ArgumentParser(description="Send today's reminder e-mails (ADR-090).")
    parser.add_argument("--dry-run", action="store_true", help="report, send and write nothing")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="run as of a date")
    args = parser.parse_args()

    try:
        engine = get_ops_engine()
    except RuntimeError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    today = args.today or date.today()
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn)
        report: RunReport = await run_reminders(
            session, get_email_sender(), today=today, dry_run=args.dry_run
        )
        await session.commit()
    await engine.dispose()

    for line in report.details:
        print(line)
    print(
        f"{today.isoformat()}: sent {report.sent}, already sent {report.already_sent}, "
        f"failed {report.failed}{' (dry run)' if args.dry_run else ''}"
    )
    return EXIT_FAILURES if report.failed else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

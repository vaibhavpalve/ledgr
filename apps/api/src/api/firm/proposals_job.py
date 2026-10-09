"""Backfill booking proposals for lines that were imported before proposals existed (ADR-110).

    DATABASE_URL=postgresql+asyncpg://ledgr_app:...@.../ledgr \\
    OPS_DATABASE_URL=postgresql+asyncpg://ledgr_ops:...@.../ledgr \\
        python -m api.firm.proposals_job [--dry-run]      (from apps/api, PYTHONPATH=src)

The same two-role shape as the daily bank feed sync (api.bank.feed_job, ADR-108): ledgr_ops
(BYPASSRLS, SELECT-only on bank_transaction) only READS which administrations have unmatched
lines; each administration is then refreshed as ledgr_app in its own transaction with
app.current_org_id set to its own organization, through the same ProposalGenerator an import
runs. It writes under RLS like a request and cannot write into another tenant.

Safe to run any number of times: generation keeps what is still certain and writes nothing new for
it. Also useful on a schedule, to pick up invoices issued since the last import (issuing an invoice
does not itself trigger generation - only a statement import, a feed sync and a posted receipt do).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.bank.repository import SqlBankRepository
from api.firm.proposals import ProposalGenerator
from api.firm.proposals_repository import SqlProposalRepository


@dataclass
class BackfillReport:
    administrations: int = 0
    created: int = 0
    superseded: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


async def administrations_with_unmatched_lines(
    ops_engine: AsyncEngine,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(organization_id, administration_id) for every administration with an unmatched line."""
    async with ops_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT DISTINCT organization_id, administration_id FROM bank_transaction "
                "WHERE status = 'unmatched' ORDER BY organization_id, administration_id"
            )
        )
        return [(row.organization_id, row.administration_id) for row in result]


async def backfill(
    *, ops_engine: AsyncEngine, app_engine: AsyncEngine, dry_run: bool = False
) -> BackfillReport:
    report = BackfillReport()
    due = await administrations_with_unmatched_lines(ops_engine)
    report.administrations = len(due)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    for organization_id, administration_id in due:
        if dry_run:
            report.details.append(f"due: administration {administration_id}")
            continue
        try:
            async with sessions() as session, session.begin():
                # The request path's own tenant scoping (api.db.get_db_session).
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :org_id, true)"),
                    {"org_id": str(organization_id)},
                )
                generator = ProposalGenerator(
                    proposals=SqlProposalRepository(session),
                    matching=SqlBankRepository(session),
                )
                result = await generator.refresh(administration_id=administration_id)
        except Exception as exc:  # one tenant's failure must not stop the others
            report.failed += 1
            report.details.append(
                f"failed: administration {administration_id}: {type(exc).__name__}"
            )
            continue
        report.created += result.created
        report.superseded += result.superseded
        report.details.append(
            f"administration {administration_id}: {result.created} proposed, "
            f"{result.superseded} withdrawn, {result.kept} unchanged"
        )
    return report


async def main() -> int:
    from api.db import engine, get_ops_engine

    parser = argparse.ArgumentParser(description="Backfill booking proposals (ADR-110).")
    parser.add_argument("--dry-run", action="store_true", help="list what is due, write nothing")
    args = parser.parse_args()
    try:
        ops = get_ops_engine()
    except RuntimeError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    report = await backfill(ops_engine=ops, app_engine=engine, dry_run=args.dry_run)
    await ops.dispose()
    await engine.dispose()
    for line in report.details:
        print(line)
    print(
        f"{report.administrations} administrations, {report.created} proposed, "
        f"{report.superseded} withdrawn, {report.failed} failed"
        f"{' (dry run)' if args.dry_run else ''}"
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

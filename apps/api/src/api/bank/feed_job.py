"""The daily bank feed sync (scripts/sync_bank_feeds.py, ADR-108), as a function a test can drive.

ledgr_ops (BYPASSRLS, SELECT-only on bank_feed_connection) only READS which connections are due.
Each sync then runs as ledgr_app in its own transaction with app.current_org_id set to that
connection's organization - the same tenant scoping a request gets (api.db.get_db_session) - and
through the same BankFeedService a "Sync now" click uses. The job therefore writes
bank_transaction rows under row-level security like any request, and cannot write into another
tenant by construction.

One connection's failure is recorded on that connection and does not stop the rest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.bank.adapters import BankFeedProvider
from api.bank.compose import build_bank_feed_service, build_bank_service
from api.bank.feed import FeedError, FeedSettings

#: A connection synced less than this long ago is skipped: "daily" with room for the cron's jitter.
MIN_INTERVAL = timedelta(hours=20)


@dataclass
class JobReport:
    due: int = 0
    synced: int = 0
    failed: int = 0
    new_lines: int = 0
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Due:
    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    bank_account_id: uuid.UUID


async def due_connections(ops_engine: AsyncEngine, *, now: datetime) -> list[_Due]:
    async with ops_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, organization_id, administration_id, bank_account_id "
                "  FROM bank_feed_connection "
                " WHERE status = 'linked' "
                "   AND (last_synced_at IS NULL OR last_synced_at < :due_before) "
                " ORDER BY last_synced_at NULLS FIRST"
            ),
            {"due_before": now - MIN_INTERVAL},
        )
        return [
            _Due(
                id=row.id,
                organization_id=row.organization_id,
                administration_id=row.administration_id,
                bank_account_id=row.bank_account_id,
            )
            for row in result
        ]


async def sync_due_feeds(
    *,
    ops_engine: AsyncEngine,
    app_engine: AsyncEngine,
    provider: BankFeedProvider,
    settings: FeedSettings,
    now: datetime,
    dry_run: bool = False,
) -> JobReport:
    report = JobReport()
    due = await due_connections(ops_engine, now=now)
    report.due = len(due)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)

    for row in due:
        label = f"connection {row.id} (administration {row.administration_id})"
        if dry_run:
            report.details.append(f"due: {label}")
            continue
        try:
            async with sessions() as session, session.begin():
                await _scope(session, row.organization_id)
                service = build_bank_feed_service(
                    session,
                    bank=build_bank_service(session),
                    provider=provider,
                    settings=settings,
                )
                outcome = await service.sync(
                    organization_id=row.organization_id,
                    administration_id=row.administration_id,
                    bank_account_id=row.bank_account_id,
                    actor_user_id=None,
                )
        except FeedError as exc:
            report.failed += 1
            report.details.append(f"refused: {label}: {exc.reason}")
            continue
        except Exception as exc:  # one tenant's failure must not stop the others
            report.failed += 1
            report.details.append(f"failed: {label}: {type(exc).__name__}")
            continue
        if outcome.imported is None:
            report.failed += 1
            report.details.append(f"not read: {label}: {outcome.connection.last_error}")
        else:
            report.synced += 1
            report.new_lines += outcome.imported.transaction_count
            report.details.append(
                f"synced: {label}: {outcome.imported.transaction_count} new, "
                f"{outcome.imported.duplicate_count} already there"
            )
    return report


async def _scope(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """The request path's own tenant scoping (api.db.get_db_session), for one connection."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(organization_id)},
    )

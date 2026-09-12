"""SQLAlchemy-backed AuthAttemptRepository, reading/writing auth_attempt
through an ordinary AsyncSession - see migrations/0008_auth_rate_limiting.sql.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.rate_limiting import AttemptOutcome


class SqlAuthAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        endpoint: str,
        account_key: str,
        source_ip: str | None,
        outcome: AttemptOutcome,
        at: datetime,
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO auth_attempt (endpoint, account_key, source_ip, outcome, occurred_at) "
                "VALUES (:endpoint, :account_key, :source_ip, :outcome, :at)"
            ),
            {
                "endpoint": endpoint,
                "account_key": account_key,
                "source_ip": source_ip,
                "outcome": outcome,
                "at": at,
            },
        )

    async def count_recent(self, *, endpoint: str, account_key: str, since: datetime) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(*) FROM auth_attempt "
                "WHERE endpoint = :endpoint AND account_key = :account_key "
                "  AND occurred_at >= :since"
            ),
            {"endpoint": endpoint, "account_key": account_key, "since": since},
        )
        return int(result.scalar_one())

    async def consecutive_failures(
        self, *, endpoint: str, account_key: str
    ) -> tuple[int, datetime | None]:
        result = await self._session.execute(
            text(
                "WITH last_success AS ("
                "  SELECT MAX(occurred_at) AS at FROM auth_attempt "
                "  WHERE endpoint = :endpoint AND account_key = :account_key "
                "    AND outcome = 'success'"
                ") "
                "SELECT COUNT(*) AS failure_count, MAX(a.occurred_at) AS last_failure_at "
                "FROM auth_attempt a, last_success "
                "WHERE a.endpoint = :endpoint AND a.account_key = :account_key "
                "  AND a.outcome = 'failure' "
                "  AND (last_success.at IS NULL OR a.occurred_at > last_success.at)"
            ),
            {"endpoint": endpoint, "account_key": account_key},
        )
        row = result.one()
        return int(row.failure_count), row.last_failure_at

    async def distinct_account_keys_from_ip(
        self, *, endpoint: str, source_ip: str, since: datetime
    ) -> int:
        result = await self._session.execute(
            text(
                "SELECT COUNT(DISTINCT account_key) FROM auth_attempt "
                "WHERE endpoint = :endpoint AND source_ip = :source_ip AND occurred_at >= :since"
            ),
            {"endpoint": endpoint, "source_ip": source_ip, "since": since},
        )
        return int(result.scalar_one())

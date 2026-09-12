"""DB-backed proof that auth_attempt (migrations/0008_auth_rate_limiting.sql)
and SqlAuthAttemptRepository work against the real schema. Complements
tests/auth/test_rate_limiting.py, which proves the rate-limiting LOGIC in
isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.rate_limiting_repository import SqlAuthAttemptRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_count_recent_reflects_real_rows() -> None:
    account_key = f"attempt-{uuid.uuid4()}@example.com"
    now = datetime.now(UTC)

    async with _session_factory() as session, session.begin():
        repo = SqlAuthAttemptRepository(session)
        for _ in range(3):
            await repo.record(
                endpoint="login",
                account_key=account_key,
                source_ip="203.0.113.5",
                outcome="success",
                at=now,
            )

        count = await repo.count_recent(
            endpoint="login", account_key=account_key, since=now - timedelta(minutes=1)
        )
        assert count == 3


async def test_consecutive_failures_stops_counting_at_the_last_success() -> None:
    account_key = f"attempt-{uuid.uuid4()}@example.com"
    now = datetime.now(UTC)

    async with _session_factory() as session, session.begin():
        repo = SqlAuthAttemptRepository(session)
        await repo.record(
            endpoint="login",
            account_key=account_key,
            source_ip="203.0.113.5",
            outcome="failure",
            at=now,
        )
        await repo.record(
            endpoint="login",
            account_key=account_key,
            source_ip="203.0.113.5",
            outcome="success",
            at=now + timedelta(seconds=1),
        )
        await repo.record(
            endpoint="login",
            account_key=account_key,
            source_ip="203.0.113.5",
            outcome="failure",
            at=now + timedelta(seconds=2),
        )

        count, last_failure_at = await repo.consecutive_failures(
            endpoint="login", account_key=account_key
        )
        assert count == 1  # only the failure AFTER the success counts
        assert last_failure_at is not None


async def test_distinct_account_keys_from_ip_counts_unique_targets() -> None:
    source_ip = "198.51.100.7"
    now = datetime.now(UTC)
    endpoint = f"login-{uuid.uuid4()}"  # unique per test run to avoid cross-test interference

    async with _session_factory() as session, session.begin():
        repo = SqlAuthAttemptRepository(session)
        for i in range(5):
            await repo.record(
                endpoint=endpoint,
                account_key=f"victim{i}@example.com",
                source_ip=source_ip,
                outcome="failure",
                at=now,
            )
        # A repeat against one of the same accounts should not inflate the count.
        await repo.record(
            endpoint=endpoint,
            account_key="victim0@example.com",
            source_ip=source_ip,
            outcome="failure",
            at=now,
        )

        distinct = await repo.distinct_account_keys_from_ip(
            endpoint=endpoint, source_ip=source_ip, since=now - timedelta(minutes=1)
        )
        assert distinct == 5

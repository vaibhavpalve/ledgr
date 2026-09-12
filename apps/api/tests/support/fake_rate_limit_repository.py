"""In-memory AuthAttemptRepository double for testing
api.auth.rate_limiting without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from api.auth.rate_limiting import AttemptOutcome


@dataclass(frozen=True, slots=True)
class _Attempt:
    endpoint: str
    account_key: str
    source_ip: str | None
    outcome: AttemptOutcome
    occurred_at: datetime


class InMemoryAuthAttemptRepository:
    def __init__(self) -> None:
        self._attempts: list[_Attempt] = []

    async def record(
        self,
        *,
        endpoint: str,
        account_key: str,
        source_ip: str | None,
        outcome: AttemptOutcome,
        at: datetime,
    ) -> None:
        self._attempts.append(
            _Attempt(
                endpoint=endpoint,
                account_key=account_key,
                source_ip=source_ip,
                outcome=outcome,
                occurred_at=at,
            )
        )

    async def count_recent(self, *, endpoint: str, account_key: str, since: datetime) -> int:
        return sum(
            1
            for a in self._attempts
            if a.endpoint == endpoint and a.account_key == account_key and a.occurred_at >= since
        )

    async def consecutive_failures(
        self, *, endpoint: str, account_key: str
    ) -> tuple[int, datetime | None]:
        matching = [
            a for a in self._attempts if a.endpoint == endpoint and a.account_key == account_key
        ]
        successes = [a.occurred_at for a in matching if a.outcome == "success"]
        last_success_at = max(successes) if successes else None

        failures_since = [
            a.occurred_at
            for a in matching
            if a.outcome == "failure"
            and (last_success_at is None or a.occurred_at > last_success_at)
        ]
        if not failures_since:
            return 0, None
        return len(failures_since), max(failures_since)

    async def distinct_account_keys_from_ip(
        self, *, endpoint: str, source_ip: str, since: datetime
    ) -> int:
        return len(
            {
                a.account_key
                for a in self._attempts
                if a.endpoint == endpoint and a.source_ip == source_ip and a.occurred_at >= since
            }
        )

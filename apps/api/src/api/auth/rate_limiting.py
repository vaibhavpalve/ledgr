"""IAM-019: rate limiting and progressive lockout on authentication
endpoints; credential-stuffing detection with anomaly alerting.

Two dimensions, computed from the same append-only event log
(auth_attempt, migrations/0008_auth_rate_limiting.sql):

  - account_key (usually an email): "how many attempts has THIS account
    seen recently" (a sliding-window rate limit) and "how many
    CONSECUTIVE failures has it racked up since its last success" (which
    drives progressive lockout - the lockout gets longer the more times in
    a row the same account fails, and resets to nothing on a success).
  - source_ip: "how many DIFFERENT account_keys has THIS ip failed
    against recently" - the credential-stuffing shape, orthogonal to
    brute-forcing one account. One attacker trying a list of stolen
    email+password pairs against many different accounts looks nothing
    like one account under sustained attack, and needs its own signal.

AuthRateLimiter.check() answers "should this attempt even be allowed to
proceed" BEFORE the caller does any real authentication work (so a locked
account never even reaches a password hash comparison);
AuthRateLimiter.record_attempt() is called AFTER, with the real outcome,
to update the log those future checks read from.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

logger = logging.getLogger("api.auth.rate_limiting")

# Sliding-window rate limit: no more than this many attempts (of either
# outcome) per account_key within this window, independent of the
# progressive-lockout schedule below - this catches a fast burst even
# before enough consecutive failures accumulate to trigger a lockout.
_RATE_LIMIT_WINDOW = timedelta(minutes=5)
_RATE_LIMIT_MAX_ATTEMPTS = 10

# Progressive lockout: the LAST (highest) threshold not exceeding the
# consecutive-failure count applies. Below the first threshold, no
# lockout. The final entry caps the duration for any higher count -
# lockouts do not grow without bound.
_LOCKOUT_SCHEDULE: tuple[tuple[int, timedelta], ...] = (
    (5, timedelta(minutes=1)),
    (6, timedelta(minutes=5)),
    (7, timedelta(minutes=15)),
    (8, timedelta(hours=1)),
    (9, timedelta(hours=24)),
)

# Credential-stuffing detection: this many DISTINCT account_keys failed
# against by the SAME source_ip within this window is treated as an
# anomaly worth alerting on.
_CREDENTIAL_STUFFING_WINDOW = timedelta(minutes=5)
_CREDENTIAL_STUFFING_DISTINCT_ACCOUNT_THRESHOLD = 10

AttemptOutcome = Literal["success", "failure"]
RateLimitReason = Literal["ok", "rate_limited", "locked_out"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lockout_duration_for(consecutive_failures: int) -> timedelta | None:
    duration: timedelta | None = None
    for threshold, candidate in _LOCKOUT_SCHEDULE:
        if consecutive_failures >= threshold:
            duration = candidate
    return duration


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    reason: RateLimitReason
    retry_after_seconds: int | None


class AuthAttemptRepository(Protocol):
    async def record(
        self,
        *,
        endpoint: str,
        account_key: str,
        source_ip: str | None,
        outcome: AttemptOutcome,
        at: datetime,
    ) -> None: ...

    async def count_recent(self, *, endpoint: str, account_key: str, since: datetime) -> int: ...

    async def consecutive_failures(
        self, *, endpoint: str, account_key: str
    ) -> tuple[int, datetime | None]:
        """Count of failures since the account_key's last success (or
        since records began, if it has never succeeded), and the
        timestamp of the most recent one among them (None if the count is
        zero).
        """
        ...

    async def distinct_account_keys_from_ip(
        self, *, endpoint: str, source_ip: str, since: datetime
    ) -> int: ...


class AnomalyAlerter(Protocol):
    async def alert_credential_stuffing(
        self, *, source_ip: str, endpoint: str, distinct_accounts: int, window: timedelta
    ) -> None: ...


class LoggingAnomalyAlerter:
    """Real implementation: a structured warning-level log line. Wiring
    this to a real SIEM/paging system (SEC-037) is future work; this at
    minimum guarantees the signal is never silently dropped - every
    credential-stuffing detection produces a durable, greppable record.
    """

    async def alert_credential_stuffing(
        self, *, source_ip: str, endpoint: str, distinct_accounts: int, window: timedelta
    ) -> None:
        logger.warning(
            "credential_stuffing_suspected",
            extra={
                "source_ip": source_ip,
                "endpoint": endpoint,
                "distinct_accounts": distinct_accounts,
                "window_seconds": int(window.total_seconds()),
            },
        )


class AuthRateLimiter:
    def __init__(
        self,
        repository: AuthAttemptRepository,
        alerter: AnomalyAlerter,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._alerter = alerter
        self._clock = clock

    async def check(self, *, endpoint: str, account_key: str) -> RateLimitDecision:
        now = self._clock()

        failures, last_failure_at = await self._repository.consecutive_failures(
            endpoint=endpoint, account_key=account_key
        )
        lockout_duration = _lockout_duration_for(failures)
        if lockout_duration is not None and last_failure_at is not None:
            unlock_at = last_failure_at + lockout_duration
            if now < unlock_at:
                return RateLimitDecision(
                    allowed=False,
                    reason="locked_out",
                    retry_after_seconds=int((unlock_at - now).total_seconds()),
                )

        recent_count = await self._repository.count_recent(
            endpoint=endpoint, account_key=account_key, since=now - _RATE_LIMIT_WINDOW
        )
        if recent_count >= _RATE_LIMIT_MAX_ATTEMPTS:
            return RateLimitDecision(
                allowed=False,
                reason="rate_limited",
                retry_after_seconds=int(_RATE_LIMIT_WINDOW.total_seconds()),
            )

        return RateLimitDecision(allowed=True, reason="ok", retry_after_seconds=None)

    async def record_attempt(
        self,
        *,
        endpoint: str,
        account_key: str,
        source_ip: str | None,
        outcome: AttemptOutcome,
    ) -> None:
        now = self._clock()
        await self._repository.record(
            endpoint=endpoint, account_key=account_key, source_ip=source_ip, outcome=outcome, at=now
        )

        if source_ip is not None and outcome == "failure":
            distinct = await self._repository.distinct_account_keys_from_ip(
                endpoint=endpoint, source_ip=source_ip, since=now - _CREDENTIAL_STUFFING_WINDOW
            )
            if distinct >= _CREDENTIAL_STUFFING_DISTINCT_ACCOUNT_THRESHOLD:
                await self._alerter.alert_credential_stuffing(
                    source_ip=source_ip,
                    endpoint=endpoint,
                    distinct_accounts=distinct,
                    window=_CREDENTIAL_STUFFING_WINDOW,
                )

"""Pure-logic tests for IAM-019: rate limiting, progressive lockout, and
credential-stuffing detection. No database - InMemoryAuthAttemptRepository
and FakeAnomalyAlerter, with an injected fake clock so lockout timing is
deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.auth.rate_limiting import AuthRateLimiter
from tests.support.fake_anomaly_alerter import FakeAnomalyAlerter
from tests.support.fake_rate_limit_repository import InMemoryAuthAttemptRepository

_ENDPOINT = "login"


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _limiter(clock: _FakeClock) -> tuple[AuthRateLimiter, FakeAnomalyAlerter]:
    alerter = FakeAnomalyAlerter()
    limiter = AuthRateLimiter(InMemoryAuthAttemptRepository(), alerter, clock=clock)
    return limiter, alerter


# --- Baseline: allowed with no history --------------------------------------


async def test_a_fresh_account_key_is_allowed() -> None:
    limiter, _ = _limiter(_FakeClock())
    decision = await limiter.check(endpoint=_ENDPOINT, account_key="owner@example.com")
    assert decision.allowed is True
    assert decision.reason == "ok"


# --- Sliding-window rate limit -----------------------------------------------


async def test_exceeding_the_attempt_count_within_the_window_is_rate_limited() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(10):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="success"
        )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.allowed is False
    assert decision.reason == "rate_limited"
    assert decision.retry_after_seconds is not None and decision.retry_after_seconds > 0


async def test_rate_limit_clears_once_the_window_passes() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(10):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="success"
        )
    assert (await limiter.check(endpoint=_ENDPOINT, account_key=account)).allowed is False

    clock.advance(minutes=6)  # past the 5-minute window

    assert (await limiter.check(endpoint=_ENDPOINT, account_key=account)).allowed is True


# --- Progressive lockout -----------------------------------------------------


async def test_fewer_than_five_consecutive_failures_does_not_lock_out() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(4):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.allowed is True


async def test_five_consecutive_failures_locks_out_for_one_minute() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(5):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.allowed is False
    assert decision.reason == "locked_out"
    assert decision.retry_after_seconds == 60


async def test_lockout_duration_increases_with_more_consecutive_failures() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(8):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.allowed is False
    assert decision.retry_after_seconds == int(timedelta(hours=1).total_seconds())


async def test_lockout_duration_caps_at_the_schedules_final_entry() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(50):  # far beyond the schedule's highest threshold
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.retry_after_seconds == int(timedelta(hours=24).total_seconds())


async def test_the_lockout_lifts_once_its_duration_has_elapsed() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(5):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )
    assert (await limiter.check(endpoint=_ENDPOINT, account_key=account)).allowed is False

    clock.advance(minutes=1, seconds=1)  # past the 1-minute lockout

    assert (await limiter.check(endpoint=_ENDPOINT, account_key=account)).allowed is True


async def test_a_success_resets_the_consecutive_failure_count() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)
    account = "owner@example.com"

    for _ in range(4):
        await limiter.record_attempt(
            endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
        )
    await limiter.record_attempt(
        endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="success"
    )
    # One more failure after the success - only 1 consecutive failure now,
    # nowhere near the 5-failure lockout threshold.
    await limiter.record_attempt(
        endpoint=_ENDPOINT, account_key=account, source_ip="203.0.113.5", outcome="failure"
    )

    decision = await limiter.check(endpoint=_ENDPOINT, account_key=account)
    assert decision.allowed is True


async def test_lockout_and_rate_limiting_are_scoped_per_account_key() -> None:
    clock = _FakeClock()
    limiter, _ = _limiter(clock)

    for _ in range(5):
        await limiter.record_attempt(
            endpoint=_ENDPOINT,
            account_key="victim@example.com",
            source_ip="203.0.113.5",
            outcome="failure",
        )

    victim_decision = await limiter.check(endpoint=_ENDPOINT, account_key="victim@example.com")
    assert victim_decision.allowed is False

    other_decision = await limiter.check(endpoint=_ENDPOINT, account_key="unrelated@example.com")
    assert other_decision.allowed is True


# --- Credential-stuffing detection -------------------------------------------


async def test_one_ip_failing_against_many_distinct_accounts_triggers_an_alert() -> None:
    clock = _FakeClock()
    limiter, alerter = _limiter(clock)

    for i in range(10):
        await limiter.record_attempt(
            endpoint=_ENDPOINT,
            account_key=f"victim{i}@example.com",
            source_ip="198.51.100.7",
            outcome="failure",
        )

    assert len(alerter.alerts) == 1
    assert alerter.alerts[0].source_ip == "198.51.100.7"
    assert alerter.alerts[0].distinct_accounts == 10


async def test_fewer_than_the_threshold_of_distinct_accounts_does_not_alert() -> None:
    clock = _FakeClock()
    limiter, alerter = _limiter(clock)

    for i in range(9):
        await limiter.record_attempt(
            endpoint=_ENDPOINT,
            account_key=f"victim{i}@example.com",
            source_ip="198.51.100.7",
            outcome="failure",
        )

    assert alerter.alerts == []


async def test_repeated_failures_against_one_account_from_one_ip_does_not_alert() -> None:
    """Progressive lockout's job, not credential-stuffing detection's -
    the signature here is DISTINCT accounts, not attempt volume.
    """
    clock = _FakeClock()
    limiter, alerter = _limiter(clock)

    for _ in range(20):
        await limiter.record_attempt(
            endpoint=_ENDPOINT,
            account_key="one-victim@example.com",
            source_ip="198.51.100.7",
            outcome="failure",
        )

    assert alerter.alerts == []


async def test_credential_stuffing_detection_ignores_attempts_with_no_source_ip() -> None:
    clock = _FakeClock()
    limiter, alerter = _limiter(clock)

    for i in range(15):
        await limiter.record_attempt(
            endpoint=_ENDPOINT,
            account_key=f"victim{i}@example.com",
            source_ip=None,
            outcome="failure",
        )

    assert alerter.alerts == []

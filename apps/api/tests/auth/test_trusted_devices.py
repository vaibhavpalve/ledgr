"""Pure-logic tests for ADR-061's trusted-device remembering: issuing a
token, checking it back, expiry, revocation, and cross-user rejection. Uses
an injected fake clock, the same pattern as test_sessions.py and
test_totp.py - no sleeping, no flakiness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from api.auth.trusted_devices import TrustedDeviceService
from tests.support.fake_trusted_device_repository import InMemoryTrustedDeviceRepository


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _service(clock: _FakeClock, *, lifetime_days: int = 7) -> TrustedDeviceService:
    return TrustedDeviceService(
        InMemoryTrustedDeviceRepository(), lifetime_days=lifetime_days, clock=clock
    )


async def test_issue_then_check_round_trip() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()

    device, raw_token = await service.issue(user_id=user_id, name="Chrome on Windows")

    assert device.user_id == user_id
    assert device.name == "Chrome on Windows"
    assert await service.check(user_id=user_id, raw_token=raw_token) is True


async def test_the_raw_token_is_never_recoverable_from_the_issued_device() -> None:
    clock = _FakeClock()
    service = _service(clock)
    device, raw_token = await service.issue(user_id=uuid.uuid4(), name=None)

    assert raw_token not in (device.id.hex, str(device.id))


async def test_an_unknown_token_fails_closed() -> None:
    clock = _FakeClock()
    service = _service(clock)
    assert await service.check(user_id=uuid.uuid4(), raw_token="never-issued") is False


async def test_a_token_issued_for_one_user_does_not_verify_another() -> None:
    """The check that keeps a stolen/guessed token from being replayed
    against a different account - `user_id` must match the token's own
    owner, not merely be a valid, live token for SOMEONE.
    """
    clock = _FakeClock()
    service = _service(clock)
    _, raw_token = await service.issue(user_id=uuid.uuid4(), name=None)

    assert await service.check(user_id=uuid.uuid4(), raw_token=raw_token) is False


async def test_the_token_expires_after_its_lifetime() -> None:
    clock = _FakeClock()
    service = _service(clock, lifetime_days=7)
    user_id = uuid.uuid4()
    _, raw_token = await service.issue(user_id=user_id, name=None)

    clock.advance(days=6, hours=23)
    assert await service.check(user_id=user_id, raw_token=raw_token) is True

    clock.advance(hours=2)
    assert await service.check(user_id=user_id, raw_token=raw_token) is False


async def test_a_revoked_device_no_longer_verifies() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    device, raw_token = await service.issue(user_id=user_id, name=None)

    await service.revoke(device.id)

    assert await service.check(user_id=user_id, raw_token=raw_token) is False


async def test_list_for_user_only_returns_that_users_devices() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    await service.issue(user_id=user_a, name="A's phone")
    await service.issue(user_id=user_b, name="B's phone")

    listing = await service.list_for_user(user_a)

    assert [d.name for d in listing] == ["A's phone"]


async def test_checking_a_valid_token_touches_last_used() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    device, raw_token = await service.issue(user_id=user_id, name=None)

    clock.advance(days=1)
    await service.check(user_id=user_id, raw_token=raw_token)

    listing = await service.list_for_user(user_id)
    assert listing[0].last_used_at == clock.now

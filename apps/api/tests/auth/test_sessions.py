"""Pure-logic tests for IAM-016: 12-hour absolute maximum lifetime,
30-minute idle timeout for privileged roles, and the re-authentication
primitive. Uses an injected fake clock so expiry/idle-timeout behavior is
deterministic - no sleeping, no flakiness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.auth.geolocation import Location
from api.auth.sessions import (
    SessionExpiredError,
    SessionIdleTimeoutError,
    SessionNotFoundError,
    SessionRevokedError,
    SessionService,
)
from tests.support.fake_auth_repository import InMemorySessionRepository


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _service(clock: _FakeClock) -> SessionService:
    return SessionService(InMemorySessionRepository(), clock=clock)


async def test_issue_then_validate_round_trip() -> None:
    clock = _FakeClock()
    service = _service(clock)
    issued, raw_token = await service.issue_session(uuid.uuid4(), privileged=True)

    validated = await service.validate_session(raw_token)

    assert validated.id == issued.id


async def test_the_raw_token_is_never_recoverable_from_the_stored_session() -> None:
    clock = _FakeClock()
    service = _service(clock)
    issued, raw_token = await service.issue_session(uuid.uuid4(), privileged=True)

    assert raw_token not in (issued.token_hash, issued.id.hex)
    assert issued.token_hash != raw_token


async def test_an_unknown_token_is_rejected() -> None:
    clock = _FakeClock()
    service = _service(clock)

    with pytest.raises(SessionNotFoundError):
        await service.validate_session("this-token-was-never-issued")


async def test_absolute_lifetime_is_twelve_hours_by_default() -> None:
    clock = _FakeClock()
    service = _service(clock)
    _, raw_token = await service.issue_session(uuid.uuid4(), privileged=False)

    clock.advance(hours=11, minutes=59)
    await service.validate_session(raw_token)  # still within 12h - does not raise

    clock.advance(minutes=2)  # now 12h01m since issuance
    with pytest.raises(SessionExpiredError):
        await service.validate_session(raw_token)


async def test_idle_timeout_applies_to_privileged_sessions() -> None:
    clock = _FakeClock()
    service = _service(clock)
    _, raw_token = await service.issue_session(uuid.uuid4(), privileged=True)

    clock.advance(minutes=31)  # no activity for 31 minutes

    with pytest.raises(SessionIdleTimeoutError):
        await service.validate_session(raw_token)


async def test_idle_timeout_does_not_apply_to_non_privileged_sessions() -> None:
    """Only the absolute 12-hour lifetime applies to a non-privileged
    session - IAM-016 scopes the 30-minute idle timeout to privileged
    roles specifically.
    """
    clock = _FakeClock()
    service = _service(clock)
    _, raw_token = await service.issue_session(uuid.uuid4(), privileged=False)

    clock.advance(hours=2)  # far past 30 minutes of "idle", well within 12h

    await service.validate_session(raw_token)  # does not raise


async def test_activity_resets_the_idle_clock() -> None:
    clock = _FakeClock()
    service = _service(clock)
    _, raw_token = await service.issue_session(uuid.uuid4(), privileged=True)

    clock.advance(minutes=25)
    await service.validate_session(raw_token)  # activity - resets idle timer

    # 25 more minutes; 50 total since issuance, but only 25 since the activity above.
    clock.advance(minutes=25)
    await service.validate_session(raw_token)  # still fine - idle clock was reset above


async def test_revoked_session_is_rejected() -> None:
    clock = _FakeClock()
    service = _service(clock)
    issued, raw_token = await service.issue_session(uuid.uuid4(), privileged=True)

    await service.revoke_session(issued.id)

    with pytest.raises(SessionRevokedError):
        await service.validate_session(raw_token)


async def test_revoke_all_for_user_revokes_only_that_users_sessions() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    _, token_a = await service.issue_session(user_a, privileged=True)
    _, token_b = await service.issue_session(user_b, privileged=True)

    revoked_count = await service.revoke_all_for_user(user_a)

    assert revoked_count == 1
    with pytest.raises(SessionRevokedError):
        await service.validate_session(token_a)
    await service.validate_session(token_b)  # untouched


async def test_list_sessions_reflects_reality_for_iam_017() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    await service.issue_session(user_id, privileged=True, ip_address="203.0.113.5")
    clock.advance(minutes=5)
    await service.issue_session(user_id, privileged=False, ip_address="203.0.113.9")

    sessions = await service.list_sessions(user_id)

    assert len(sessions) == 2
    assert {s.ip_address for s in sessions} == {"203.0.113.5", "203.0.113.9"}


async def test_list_sessions_with_location_pairs_each_session_with_its_resolved_location() -> None:
    class _FixedLocationResolver:
        async def resolve(self, ip_address: str) -> Location | None:
            return Location(city="Amsterdam", country="Netherlands") if ip_address else None

    clock = _FakeClock()
    service = SessionService(
        InMemorySessionRepository(), clock=clock, geolocation=_FixedLocationResolver()
    )
    user_id = uuid.uuid4()
    await service.issue_session(user_id, privileged=True, ip_address="203.0.113.5")

    listings = await service.list_sessions_with_location(user_id)

    assert len(listings) == 1
    assert listings[0].location == Location(city="Amsterdam", country="Netherlands")
    assert listings[0].session.ip_address == "203.0.113.5"


async def test_list_sessions_with_location_leaves_unknown_ip_unresolved() -> None:
    clock = _FakeClock()
    service = _service(clock)  # default NullGeoLocationResolver
    user_id = uuid.uuid4()
    await service.issue_session(user_id, privileged=True)  # no ip_address

    listings = await service.list_sessions_with_location(user_id)

    assert listings[0].location is None


async def test_requires_recent_reauthentication_reflects_elapsed_time() -> None:
    clock = _FakeClock()
    service = _service(clock)
    issued, _ = await service.issue_session(uuid.uuid4(), privileged=True)

    assert not service.requires_recent_reauthentication(issued, within=timedelta(minutes=15))

    clock.advance(minutes=20)
    assert service.requires_recent_reauthentication(issued, within=timedelta(minutes=15))

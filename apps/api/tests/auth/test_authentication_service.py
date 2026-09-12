"""Pure-logic tests for registration and password authentication - no
database, using InMemoryUserRepository plus LocalDenylistBreachChecker.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from api.auth.breach_check import LocalDenylistBreachChecker
from api.auth.passwords import WeakPasswordError
from api.auth.service import (
    AccountNotActiveError,
    AuthenticationService,
    InvalidCredentialsError,
    UserAlreadyExistsError,
)
from tests.support.fake_auth_repository import InMemoryUserRepository

_PASSWORD = "a perfectly fine passphrase"


def _service() -> AuthenticationService:
    return AuthenticationService(InMemoryUserRepository(), LocalDenylistBreachChecker())


async def test_register_then_authenticate_round_trip() -> None:
    service = _service()
    registered = await service.register_user("owner@example.com", _PASSWORD)

    authenticated = await service.authenticate_with_password("owner@example.com", _PASSWORD)

    assert authenticated.id == registered.id


async def test_email_is_normalized_case_and_whitespace_insensitively() -> None:
    service = _service()
    await service.register_user("Owner@Example.com", _PASSWORD)

    authenticated = await service.authenticate_with_password("  owner@example.com  ", _PASSWORD)

    assert authenticated.email == "owner@example.com"


async def test_registering_the_same_email_twice_fails() -> None:
    service = _service()
    await service.register_user("owner@example.com", _PASSWORD)

    with pytest.raises(UserAlreadyExistsError):
        await service.register_user("owner@example.com", _PASSWORD)


async def test_registering_with_a_weak_password_fails() -> None:
    service = _service()
    with pytest.raises(WeakPasswordError):
        await service.register_user("owner@example.com", "short")


async def test_wrong_password_is_rejected() -> None:
    service = _service()
    await service.register_user("owner@example.com", _PASSWORD)

    with pytest.raises(InvalidCredentialsError):
        await service.authenticate_with_password("owner@example.com", "the wrong passphrase")


async def test_nonexistent_user_and_wrong_password_raise_the_same_exception() -> None:
    """SEC-008: error responses never disclose whether an account exists.
    Both failure modes must be indistinguishable to the caller.
    """
    service = _service()
    await service.register_user("owner@example.com", _PASSWORD)

    with pytest.raises(InvalidCredentialsError) as nonexistent_exc:
        await service.authenticate_with_password("nobody@example.com", _PASSWORD)
    with pytest.raises(InvalidCredentialsError) as wrong_password_exc:
        await service.authenticate_with_password("owner@example.com", "not the passphrase")

    assert type(nonexistent_exc.value) is type(wrong_password_exc.value)


async def test_nonexistent_user_and_wrong_password_take_comparable_time() -> None:
    """Loose timing check: verifying against the real Argon2 DUMMY_HASH for
    a nonexistent user should cost roughly the same as verifying a wrong
    password for a real user - not the near-zero cost of short-circuiting
    on "no such row." A wide tolerance keeps this from being flaky in CI
    while still catching a regression that removes the dummy-hash call
    entirely (which would make the nonexistent-user path orders of
    magnitude faster, not just somewhat faster).
    """
    service = _service()
    await service.register_user("owner@example.com", _PASSWORD)

    async def _time_it(email: str, password: str) -> float:
        start = time.perf_counter()
        with contextlib.suppress(InvalidCredentialsError):
            await service.authenticate_with_password(email, password)
        return time.perf_counter() - start

    nonexistent_seconds = await _time_it("nobody@example.com", _PASSWORD)
    wrong_password_seconds = await _time_it("owner@example.com", "not the passphrase")

    slower, faster = sorted([nonexistent_seconds, wrong_password_seconds], reverse=True)
    assert slower < faster * 5  # generous bound - this is a smoke test, not a precise one


async def test_inactive_account_is_rejected_only_after_correct_password() -> None:
    repository = InMemoryUserRepository()
    service = AuthenticationService(repository, LocalDenylistBreachChecker())
    user = await service.register_user("owner@example.com", _PASSWORD)
    repository.set_status(user.id, "suspended")

    # Wrong password against a suspended account still looks like any
    # other wrong password - no extra information leaked.
    with pytest.raises(InvalidCredentialsError):
        await service.authenticate_with_password("owner@example.com", "not the passphrase")

    # Only once the correct password is supplied does the real status surface.
    with pytest.raises(AccountNotActiveError) as exc_info:
        await service.authenticate_with_password("owner@example.com", _PASSWORD)
    assert exc_info.value.status == "suspended"

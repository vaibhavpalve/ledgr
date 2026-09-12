"""Pure-logic tests for IAM-013: hashing, and the password policy - 12
character minimum, breached-password screening, and explicitly NO
composition-rule or rotation checks.
"""

from __future__ import annotations

import pytest

from api.auth.breach_check import LocalDenylistBreachChecker
from api.auth.passwords import (
    MAX_LENGTH,
    MIN_LENGTH,
    WeakPasswordError,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)


def _checker() -> LocalDenylistBreachChecker:
    return LocalDenylistBreachChecker()


async def test_password_shorter_than_minimum_is_rejected() -> None:
    with pytest.raises(WeakPasswordError, match="at least 12"):
        await validate_password_policy("short1234567"[: MIN_LENGTH - 1], breach_checker=_checker())


async def test_password_at_exactly_the_minimum_length_is_accepted() -> None:
    password = "correcthorse"  # exactly 12 characters, no composition requirements
    assert len(password) == MIN_LENGTH
    await validate_password_policy(password, breach_checker=_checker())  # does not raise


async def test_password_longer_than_maximum_is_rejected() -> None:
    with pytest.raises(WeakPasswordError, match=f"at most {MAX_LENGTH}"):
        await validate_password_policy("a" * (MAX_LENGTH + 1), breach_checker=_checker())


async def test_password_with_no_uppercase_digit_or_symbol_is_accepted() -> None:
    """The explicit, load-bearing negative case: IAM-013 requires NO
    composition rules. A password of all lowercase letters, meeting only
    the length requirement, must pass.
    """
    await validate_password_policy("onlylowercaseletters", breach_checker=_checker())


async def test_breached_password_is_rejected_even_if_long_enough() -> None:
    with pytest.raises(WeakPasswordError, match="known data breach"):
        await validate_password_policy("password12345", breach_checker=_checker())


async def test_hash_and_verify_round_trip() -> None:
    password = "a reasonably long passphrase"
    hashed = hash_password(password)

    assert verify_password(password, hashed)
    assert not verify_password("a different passphrase entirely", hashed)


def test_a_hash_from_current_parameters_does_not_need_rehashing() -> None:
    assert needs_rehash(hash_password("whatever passphrase")) is False

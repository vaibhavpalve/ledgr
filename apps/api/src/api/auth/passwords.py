"""Password hashing and policy (IAM-013): NIST SP 800-63B — 12 character
minimum, breached-password screening, no forced rotation, no composition
rules.

The "no composition rules" and "no forced rotation" halves of IAM-013 are
implemented by *absence*: there is no uppercase/digit/symbol check anywhere
in validate_password_policy below, and no expires_at or last_changed-based
rejection anywhere in this module or in api.auth.service. If either shows
up here later, it is a regression against this requirement, not a missing
feature — do not add them back.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from api.auth.breach_check import PasswordBreachChecker

MIN_LENGTH = 12
# NIST 800-63B asks implementations to support at least 64 characters and
# not silently truncate. This cap exists only to bound Argon2's hashing
# cost against a maliciously huge input (a denial-of-service vector, not a
# security-strength concern) - well above the 64-char support floor.
MAX_LENGTH = 128

# OWASP's current (2023+) recommended Argon2id parameters for an
# interactive login path. Set explicitly rather than relying on library
# defaults, which can change between argon2-cffi versions - see SEC-026
# (cryptographic agility as configuration, not accident).
_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)

# Used for verify_password against a nonexistent user (see
# api.auth.service.AuthenticationService) so that failing because "no such
# user" and failing because "wrong password" take approximately the same
# amount of time - a real Argon2 hash of a fixed, unguessable value, never
# actually assigned to any account.
DUMMY_HASH = _HASHER.hash("not-a-real-password-used-only-for-timing-safety")


class WeakPasswordError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


async def validate_password_policy(password: str, *, breach_checker: PasswordBreachChecker) -> None:
    reasons: list[str] = []

    if len(password) < MIN_LENGTH:
        reasons.append(f"must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        reasons.append(f"must be at most {MAX_LENGTH} characters")

    # No composition-rule checks belong here. See module docstring.

    if not reasons and await breach_checker.is_breached(password):
        reasons.append("appears in a known data breach - choose a different password")

    if reasons:
        raise WeakPasswordError(reasons)


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _HASHER.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if this hash was produced with different parameters than
    _HASHER currently uses - callers (api.auth.service) should re-hash and
    persist the new value on the next successful login when this is true.
    This is the crypto-agility mechanism for password hashes: parameters
    can be strengthened later without forcing a mass password reset.
    """
    return _HASHER.check_needs_rehash(password_hash)

"""Breached-password screening (IAM-013). Same adapter shape as
api.crypto.kms: a Protocol the password-policy code depends on, and two
implementations selected by config — CLAUDE.md's fourth architectural
non-negotiable applies here too, since this is an integration with a
third-party service.

- HibpBreachChecker: production. Checks the "Have I Been Pwned" Pwned
  Passwords API using k-anonymity — only the first 5 hex characters of the
  password's SHA-1 hash are ever sent over the network; the full password,
  and even the full hash, never leave the process.
- LocalDenylistBreachChecker: local development and tests. No network call;
  checks against a small embedded list of extremely common passwords. Not a
  substitute for HIBP's actual breach corpus — selecting this in production
  would silently disable real breach screening while the password-length
  and no-composition-rules checks kept passing, which is exactly the kind
  of gap that's easy to miss, hence it defaults off (see build_breach_checker).
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import httpx

_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"

# Not a serious denylist — a handful of the most common passwords in every
# public breach corpus, for local/test use where a real HIBP call is
# neither desired nor reliable to depend on.
_LOCAL_DENYLIST = frozenset(
    {
        "password12345",
        "123456789012",
        "qwertyuiop123",
        "letmein123456",
        "iloveyou12345",
    }
)


class PasswordBreachChecker(Protocol):
    async def is_breached(self, password: str) -> bool: ...


class LocalDenylistBreachChecker:
    async def is_breached(self, password: str) -> bool:
        return password.lower() in _LOCAL_DENYLIST


class HibpBreachChecker:
    def __init__(
        self, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 3.0
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def is_breached(self, password: str) -> bool:
        # SHA-1 here is HIBP's published k-anonymity protocol, not a choice
        # of ours - it is never used to store or verify the password itself.
        sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]

        response = await self._client.get(
            _HIBP_RANGE_URL.format(prefix=prefix),
            headers={"Add-Padding": "true"},  # HIBP anti-traffic-analysis padding
        )
        response.raise_for_status()

        for line in response.text.splitlines():
            candidate_suffix, _, _count = line.partition(":")
            if candidate_suffix.strip().upper() == suffix:
                return True
        return False


def build_breach_checker(provider: str) -> PasswordBreachChecker:
    if provider == "local":
        return LocalDenylistBreachChecker()
    if provider == "hibp":
        return HibpBreachChecker()
    raise ValueError(f"unknown breach checker provider: {provider!r}")

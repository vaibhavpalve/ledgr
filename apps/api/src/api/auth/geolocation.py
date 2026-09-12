"""IP-to-location resolution for IAM-017's "device sessions are listed to
the user with location and last-use." Same adapter shape as
api.crypto.kms and api.auth.breach_check: a Protocol the session-listing
code depends on, with a safe local default and a real network-backed
implementation selected by config.

- IpApiGeoLocationResolver: production. A single unauthenticated HTTP call
  per lookup against ip-api.com's free JSON endpoint. No API key, no
  request signing - deliberately the simplest correct implementation
  rather than a full MaxMind GeoLite2 database integration, since "list
  approximately where a session came from" does not need
  ISP-in-a-nutshell precision.
- NullGeoLocationResolver: local development and tests. Always returns
  None - no network call, ever. Selecting this in production would mean
  every session lists no location, which is a visible, obviously-wrong
  degradation rather than a silent one - the same "safe local default"
  reasoning already applied to kms_provider and breach_checker_provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

_IP_API_URL = "http://ip-api.com/json/{ip_address}"


@dataclass(frozen=True, slots=True)
class Location:
    city: str | None
    country: str | None

    def __str__(self) -> str:
        parts = [part for part in (self.city, self.country) if part]
        return ", ".join(parts) if parts else "Unknown location"


class GeoLocationResolver(Protocol):
    async def resolve(self, ip_address: str) -> Location | None: ...


class NullGeoLocationResolver:
    async def resolve(self, ip_address: str) -> Location | None:
        return None


class IpApiGeoLocationResolver:
    def __init__(
        self, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 3.0
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def resolve(self, ip_address: str) -> Location | None:
        # Private/loopback addresses (local dev, internal health checks)
        # have no meaningful public location - fail soft rather than make
        # a pointless network call that would just error.
        if ip_address in ("127.0.0.1", "::1") or ip_address.startswith(("10.", "192.168.")):
            return None

        try:
            response = await self._client.get(_IP_API_URL.format(ip_address=ip_address))
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError:
            # Location is enrichment, never a correctness-critical value -
            # a provider outage must not break session listing, only omit
            # the location for that entry.
            return None

        if body.get("status") != "success":
            return None

        return Location(city=body.get("city") or None, country=body.get("country") or None)


def build_geolocation_resolver(provider: str) -> GeoLocationResolver:
    if provider == "local":
        return NullGeoLocationResolver()
    if provider == "ip-api":
        return IpApiGeoLocationResolver()
    raise ValueError(f"unknown geolocation provider: {provider!r}")

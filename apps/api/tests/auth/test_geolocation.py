"""Pure-logic tests for IAM-017's location resolution and session-listing
enrichment - NullGeoLocationResolver never calls the network;
IpApiGeoLocationResolver is exercised against httpx.MockTransport, so no
real network call happens in this suite either.
"""

from __future__ import annotations

import httpx
import pytest

from api.auth.geolocation import (
    IpApiGeoLocationResolver,
    Location,
    NullGeoLocationResolver,
    build_geolocation_resolver,
)


async def test_null_resolver_always_returns_none() -> None:
    resolver = NullGeoLocationResolver()
    assert await resolver.resolve("203.0.113.5") is None


def test_build_geolocation_resolver_selects_by_provider_name() -> None:
    assert isinstance(build_geolocation_resolver("local"), NullGeoLocationResolver)
    assert isinstance(build_geolocation_resolver("ip-api"), IpApiGeoLocationResolver)
    with pytest.raises(ValueError, match="unknown geolocation provider"):
        build_geolocation_resolver("maxmind")


def test_location_str_combines_city_and_country() -> None:
    assert str(Location(city="Amsterdam", country="Netherlands")) == "Amsterdam, Netherlands"
    assert str(Location(city=None, country="Netherlands")) == "Netherlands"
    assert str(Location(city=None, country=None)) == "Unknown location"


def _resolver(handler: httpx.MockTransport) -> IpApiGeoLocationResolver:
    return IpApiGeoLocationResolver(client=httpx.AsyncClient(transport=handler))


async def test_ip_api_resolver_parses_a_successful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "city": "Amsterdam", "country": "Netherlands"}
        )

    resolver = _resolver(httpx.MockTransport(handler))
    location = await resolver.resolve("203.0.113.5")

    assert location == Location(city="Amsterdam", country="Netherlands")


async def test_ip_api_resolver_returns_none_on_a_failed_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "fail", "message": "private range"})

    resolver = _resolver(httpx.MockTransport(handler))
    assert await resolver.resolve("10.0.0.5") is None


async def test_ip_api_resolver_returns_none_when_the_provider_errors() -> None:
    """Location is enrichment, never correctness-critical - a provider
    outage must not raise out of session listing, only omit the location.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    resolver = _resolver(httpx.MockTransport(handler))
    assert await resolver.resolve("203.0.113.5") is None


async def test_ip_api_resolver_skips_the_network_for_loopback_and_private_addresses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called for a private/loopback address")

    resolver = _resolver(httpx.MockTransport(handler))
    assert await resolver.resolve("127.0.0.1") is None
    assert await resolver.resolve("192.168.1.10") is None

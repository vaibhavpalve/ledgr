"""api.customers.kvk - SI-03, NFR-026.

Same shape as tests/customers/test_vies.py, and the same central property:
**nothing turns into FOUND except an explicit result from the register**, and
UNAVAILABLE is never confused with NOT_FOUND - a timeout must never tell
someone their real KvK number does not exist, and a genuinely absent number
must never be reported as "try again later".

The HTTP layer is exercised through `httpx.MockTransport`, exactly as
`test_vies.py` exercises `RestViesValidator` - `RestKvkLookup` is a client of
a wire protocol, and stubbing `lookup` would prove nothing about how the
protocol's failure modes are read.
"""

from __future__ import annotations

import httpx
import pytest

from api.customers.kvk import (
    KvkLookupStatus,
    RestKvkLookup,
    SyntaxOnlyKvkLookup,
    build_kvk_lookup,
)

KVK = "68750110"


def _lookup(handler: object) -> RestKvkLookup:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return RestKvkLookup(api_key="test-key", client=httpx.AsyncClient(transport=transport))


def _responds(payload: object, status_code: int = 200) -> RestKvkLookup:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)

    return _lookup(handler)


# --- syntax, before any network call -----------------------------------------


async def test_an_empty_number_is_unchecked() -> None:
    result = await SyntaxOnlyKvkLookup().lookup(None)
    assert result.status is KvkLookupStatus.UNCHECKED


@pytest.mark.parametrize("bad", ["1234567", "123456789", "NL12345678", "1234-5678"])
async def test_anything_that_is_not_eight_digits_is_syntax_invalid(bad: str) -> None:
    result = await SyntaxOnlyKvkLookup().lookup(bad)
    assert result.status is KvkLookupStatus.SYNTAX_INVALID


async def test_a_syntax_invalid_number_never_reaches_the_network() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"resultaten": []})

    result = await _lookup(handler).lookup("not-a-number")

    assert result.status is KvkLookupStatus.SYNTAX_INVALID
    assert calls == []


async def test_whitespace_inside_the_number_is_tolerated() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["kvkNummer"])
        return httpx.Response(200, json={"resultaten": []})

    await _lookup(handler).lookup("6875 0110")

    assert seen == ["68750110"]


# --- the dev/test stub never claims FOUND ------------------------------------


async def test_the_syntax_only_stub_never_reports_found() -> None:
    """It makes no network call at all, so FOUND would be a fabrication."""
    result = await SyntaxOnlyKvkLookup().lookup(KVK)
    assert result.status is KvkLookupStatus.UNAVAILABLE


# --- a real result ------------------------------------------------------------


async def test_a_found_company_carries_name_and_address() -> None:
    lookup = _responds(
        {
            "resultaten": [
                {
                    "kvkNummer": KVK,
                    "naam": "Test B.V.",
                    "adres": {
                        "straatnaam": "Vijzelstraat",
                        "huisnummer": 68,
                        "postcode": "1017HL",
                        "plaats": "Amsterdam",
                    },
                }
            ]
        }
    )
    result = await lookup.lookup(KVK)

    assert result.status is KvkLookupStatus.FOUND
    assert result.kvk_number == KVK
    assert result.legal_name == "Test B.V."
    assert result.trade_name == "Test B.V."
    assert result.address_line1 == "Vijzelstraat 68"
    assert result.postal_code == "1017HL"
    assert result.city == "Amsterdam"


async def test_a_missing_house_number_still_yields_the_street() -> None:
    lookup = _responds(
        {"resultaten": [{"naam": "X", "adres": {"straatnaam": "Damrak", "plaats": "Amsterdam"}}]}
    )
    result = await lookup.lookup(KVK)

    assert result.address_line1 == "Damrak"


async def test_an_empty_result_list_is_not_found() -> None:
    result = await _responds({"resultaten": []}).lookup(KVK)
    assert result.status is KvkLookupStatus.NOT_FOUND


async def test_a_404_is_not_found() -> None:
    result = await _responds({}, status_code=404).lookup(KVK)
    assert result.status is KvkLookupStatus.NOT_FOUND


# --- every way "no answer" arrives, none of them FOUND or NOT_FOUND ----------


@pytest.mark.parametrize(
    ("payload", "status_code"),
    [
        ({}, 500),
        ({}, 401),
        ("not json at all", 200),
        ("null", 200),
        ({"resultaten": "not-a-list"}, 200),
        ({"resultaten": ["not-a-dict"]}, 200),
    ],
)
async def test_every_malformed_or_failing_response_is_unavailable(
    payload: object, status_code: int
) -> None:
    result = await _responds(payload, status_code=status_code).lookup(KVK)
    assert result.status is KvkLookupStatus.UNAVAILABLE


async def test_a_transport_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    result = await _lookup(handler).lookup(KVK)
    assert result.status is KvkLookupStatus.UNAVAILABLE


async def test_unavailable_never_carries_a_name_or_address() -> None:
    """The refusal is silence, not a half-populated guess."""
    result = await _responds({}, status_code=500).lookup(KVK)

    assert result.legal_name is None
    assert result.trade_name is None
    assert result.address_line1 is None


# --- the apikey header, not a query parameter --------------------------------


async def test_the_api_key_is_sent_as_a_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"resultaten": []})

    lookup = RestKvkLookup(
        api_key="super-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )
    await lookup.lookup(KVK)

    assert seen.get("apikey") == "super-secret"


# --- the factory ---------------------------------------------------------------


def test_the_factory_refuses_kvk_api_with_no_key() -> None:
    with pytest.raises(ValueError, match="KVK_API_KEY"):
        build_kvk_lookup("kvk-api", api_key=None)


def test_the_factory_refuses_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown KvK lookup provider"):
        build_kvk_lookup("carrier-pigeon")


def test_the_factory_builds_the_real_client_given_a_key() -> None:
    lookup = build_kvk_lookup("kvk-api", api_key="k")
    assert isinstance(lookup, RestKvkLookup)


def test_the_factory_builds_the_stub_by_default() -> None:
    assert isinstance(build_kvk_lookup("syntax-only"), SyntaxOnlyKvkLookup)

"""ADR-095: what a provider's refusal is called, and when it is tried again."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from api.expenses.extraction.model import ExtractionError
from api.expenses.extraction.transport import post_json


def client(*answers: httpx.Response) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def recording_sleep() -> tuple[Callable[[float], object], list[float]]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return sleep, waits


async def send(http: httpx.AsyncClient, sleep: Callable[[float], object]) -> httpx.Response:
    return await post_json(
        http=http,
        url="https://provider.example/v1/read",
        payload={"x": 1},
        headers={"Authorization": "Bearer k"},
        timeout=5.0,
        sleep=sleep,  # type: ignore[arg-type]
    )


async def test_a_rate_limit_that_clears_is_invisible_to_the_caller() -> None:
    http, seen = client(httpx.Response(429), httpx.Response(200, json={"ok": True}))
    sleep, waits = recording_sleep()

    response = await send(http, sleep)

    assert response.status_code == 200
    assert len(seen) == 2
    assert waits == [0.5]


async def test_retry_after_is_honoured_and_capped() -> None:
    http, _ = client(
        httpx.Response(503, headers={"Retry-After": "2"}),
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(200, json={}),
    )
    sleep, waits = recording_sleep()

    await send(http, sleep)

    # An hour is not a wait a capture can afford; five seconds is the ceiling.
    assert waits == [2.0, 5.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_refusal_that_cannot_change_is_not_retried(status: int) -> None:
    http, seen = client(httpx.Response(status))
    sleep, waits = recording_sleep()

    with pytest.raises(ExtractionError):
        await send(http, sleep)

    assert len(seen) == 1 and waits == []


async def test_googles_nested_status_is_the_provider_code() -> None:
    http, _ = client(
        httpx.Response(
            403,
            json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "echo"}},
        )
    )
    sleep, _ = recording_sleep()

    with pytest.raises(ExtractionError) as excinfo:
        await send(http, sleep)

    assert excinfo.value.reason == "provider_auth_failed"
    assert excinfo.value.provider_code == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "Supplier: Meelfabriek Zeeland, total 1240.00"},
        {"code": "x" * 41},
        {"type": True},
        ["not", "an", "object"],
    ],
    ids=["sentence", "too-long", "bool", "list"],
)
async def test_only_a_short_token_is_ever_kept_from_an_error_body(body: object) -> None:
    """A code that could hold a document's text is not a code."""
    http, _ = client(httpx.Response(400, json=body))
    sleep, _ = recording_sleep()

    with pytest.raises(ExtractionError) as excinfo:
        await send(http, sleep)

    assert excinfo.value.provider_code is None


async def test_a_body_that_is_not_json_still_gives_a_status() -> None:
    http, _ = client(httpx.Response(502, text="<html>Bad gateway</html>"))
    sleep, _ = recording_sleep()

    with pytest.raises(ExtractionError) as excinfo:
        await send(http, sleep)

    assert (excinfo.value.reason, excinfo.value.http_status) == ("provider_error", 502)
    assert excinfo.value.provider_code is None

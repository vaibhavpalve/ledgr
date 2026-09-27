"""The one HTTP round trip every invoice reader makes, and what a refusal is called.

--- Telling refusals apart ---

A rate limit, a rejected API key and a wrong model id used to end the same way,
`provider_refused`, which made a failed reading undiagnosable without a database
and a guess (ADR-095). They now carry their own reason code, the HTTP status, and
the provider's own short error code - `rate_limited` from Mistral,
`PERMISSION_DENIED` from Google.

What is still never kept is the error BODY's text: an error page can echo the
request, and the request is the invoice. The provider code is accepted only as a
short token (`_TOKEN`), which is a machine code and not a place a document's text
can travel.

--- Retrying ---

A 429 or a 5xx is retried, at most twice, waiting what `Retry-After` asks for
(capped) or a short backoff. Nothing else is: a 400 sent again is the same 400.
The whole reading is still bounded by the service's own timeout, so a retry can
never hold a capture longer than a single slow answer could.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from api.expenses.extraction.model import ExtractionError

_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_WAIT_SECONDS = 5.0
_BASE_BACKOFF_SECONDS = 0.5
_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")

Sleep = Callable[[float], Awaitable[None]]


def reason_for_status(status: int) -> str:
    if status in (401, 403):
        return "provider_auth_failed"
    if status == 429:
        return "provider_rate_limited"
    if status in (400, 404, 422):
        # Almost always configuration: a model id the provider does not offer,
        # or a request shape it does not accept. Retrying cannot help.
        return "provider_rejected_request"
    if status >= 500:
        return "provider_error"
    return "provider_refused"


async def post_json(
    *,
    http: httpx.AsyncClient | None,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
    sleep: Sleep = asyncio.sleep,
) -> httpx.Response:
    """POSTs `payload`, retrying what is worth retrying. Returns a 200 or raises
    `ExtractionError`."""
    client = http or httpx.AsyncClient(timeout=timeout)
    try:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await client.post(
                    url, json=dict(payload), headers=dict(headers), timeout=timeout
                )
            except httpx.TimeoutException as exc:
                raise ExtractionError("timeout") from exc
            except httpx.HTTPError as exc:
                raise ExtractionError("provider_unreachable", type(exc).__name__) from exc

            if response.status_code == 200:
                return response
            if response.status_code not in _RETRYABLE or attempt >= _MAX_ATTEMPTS:
                raise refusal(response)
            await sleep(_wait_before_retry(response, attempt))
    finally:
        if http is None:
            await client.aclose()


def refusal(response: httpx.Response) -> ExtractionError:
    status = response.status_code
    return ExtractionError(
        reason_for_status(status),
        f"HTTP {status}",
        http_status=status,
        provider_code=_provider_code(response),
    )


def _wait_before_retry(response: httpx.Response, attempt: int) -> float:
    asked = response.headers.get("retry-after", "").strip()
    try:
        seconds = float(asked)
    except ValueError:
        seconds = _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    return max(0.0, min(seconds, _MAX_WAIT_SECONDS))


def _provider_code(response: httpx.Response) -> str | None:
    """The provider's machine-readable code, and nothing else from the body.

    Mistral answers `{"type": "rate_limited", "code": "1300", ...}`; Google
    answers `{"error": {"status": "PERMISSION_DENIED", ...}}`. `message` is never
    read.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    candidates: list[object] = [body.get("type"), body.get("code")]
    nested = body.get("error")
    if isinstance(nested, dict):
        candidates += [nested.get("status"), nested.get("type"), nested.get("code")]
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, str) and _TOKEN.match(value):
            return value
    return None

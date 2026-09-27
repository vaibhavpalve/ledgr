"""The Mistral adapter (ADR-094) - same checked-reading contract as the Vertex
adapter (see test_extraction.py's "Vertex adapter" section), different
transport: La Plateforme's chat completions endpoint, forced through one tool
via `tool_choice: "required"` with a single tool offered.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from api.expenses.extraction.mistral import MistralExtractor
from api.expenses.extraction.model import ExtractionError

TODAY = date(2026, 9, 22)


def tool_answer(**fields: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "record_invoice",
                                    "arguments": json.dumps(fields),
                                }
                            }
                        ]
                    }
                }
            ]
        },
    )


async def no_sleep(_: float) -> None:
    return None


def extractor(handler: Any, **kwargs: Any) -> tuple[MistralExtractor, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return (
        MistralExtractor(
            api_key="test-key",
            model="mistral-small-latest",
            http=client,
            today=TODAY,
            sleep=no_sleep,
            **kwargs,
        ),
        seen,
    )


async def test_the_request_carries_a_bearer_token_to_the_chat_completions_endpoint() -> None:
    reader, seen = extractor(lambda _: tool_answer(supplier="Acme"))

    await reader.extract(data=b"%PDF-1.7 x", content_type="application/pdf")

    (request,) = seen
    assert str(request.url) == "https://api.mistral.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"


async def test_a_pdf_is_sent_as_a_document_url_and_the_answer_is_forced_through_one_tool() -> None:
    reader, seen = extractor(lambda _: tool_answer(supplier="Acme"))

    await reader.extract(data=b"%PDF-1.7 x", content_type="application/pdf")

    body = json.loads(seen[0].content)
    assert body["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in body["tools"]] == ["record_invoice"]
    block = body["messages"][1]["content"][0]
    assert block["type"] == "document_url"
    assert block["document_url"].startswith("data:application/pdf;base64,")
    # The model is told the document is untrusted.
    assert "untrusted" in body["messages"][0]["content"]


async def test_a_photograph_is_sent_as_an_image_url() -> None:
    reader, seen = extractor(lambda _: tool_answer(supplier="Acme"))

    await reader.extract(data=b"\x89PNG\r\n\x1a\n", content_type="image/png")

    block = json.loads(seen[0].content)["messages"][1]["content"][0]
    assert block["type"] == "image_url"
    assert block["image_url"].startswith("data:image/png;base64,")


async def test_the_answer_is_read_from_the_tool_call_and_checked() -> None:
    reader, _ = extractor(
        lambda _: tool_answer(
            supplier="Meelfabriek Zeeland",
            invoice_date="2026-09-18",
            gross_amount="1240.00",
            vat_rate="21",
            confidence={"supplier": 0.99},
        )
    )

    result = await reader.extract(data=b"%PDF-1.7", content_type="application/pdf")

    assert result.supplier == "Meelfabriek Zeeland"
    assert result.gross_amount.__class__.__name__ == "Decimal"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "provider_rejected_request"),
        (401, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (500, "provider_error"),
        (503, "provider_error"),
    ],
)
async def test_a_refusal_is_an_extraction_error_that_does_not_carry_the_body(
    status: int, reason: str
) -> None:
    reader, _ = extractor(lambda _: httpx.Response(status, text="echo of the INVOICE TEXT"))

    with pytest.raises(ExtractionError) as excinfo:
        await reader.extract(data=b"%PDF-1.7", content_type="application/pdf")

    assert excinfo.value.reason == reason
    assert "INVOICE TEXT" not in str(excinfo.value)


async def test_mistrals_own_rate_limit_answer_is_recognised_and_retried_first() -> None:
    """The exact body La Plateforme answered in production (HTTP 429, code
    1300). Retried, then reported with Mistral's own code beside the status."""
    reader, seen = extractor(
        lambda _: httpx.Response(
            429,
            json={
                "object": "error",
                "message": "Rate limit exceeded",
                "type": "rate_limited",
                "param": None,
                "code": "1300",
                "raw_status_code": 429,
            },
        )
    )

    with pytest.raises(ExtractionError) as excinfo:
        await reader.extract(data=b"%PDF-1.7", content_type="application/pdf")

    assert len(seen) == 3, "the first try and two retries"
    assert excinfo.value.reason == "provider_rate_limited"
    assert excinfo.value.provider_code == "rate_limited"
    assert "Rate limit exceeded" not in str(excinfo.value), "the message is never carried"


async def test_an_answer_with_no_tool_call_is_unreadable() -> None:
    reader, _ = extractor(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"tool_calls": []}}]})
    )

    with pytest.raises(ExtractionError) as excinfo:
        await reader.extract(data=b"%PDF-1.7", content_type="application/pdf")

    assert excinfo.value.reason == "response_unreadable"


async def test_a_timeout_and_a_connection_failure_are_told_apart() -> None:
    def times_out(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def cannot_connect(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    slow, _ = extractor(times_out)
    down, _ = extractor(cannot_connect)

    with pytest.raises(ExtractionError) as timeout:
        await slow.extract(data=b"%PDF-1.7", content_type="application/pdf")
    with pytest.raises(ExtractionError) as unreachable:
        await down.extract(data=b"%PDF-1.7", content_type="application/pdf")

    assert timeout.value.reason == "timeout"
    assert unreachable.value.reason == "provider_unreachable"


async def test_heic_is_not_sent_and_neither_is_an_oversized_file() -> None:
    reader, seen = extractor(lambda _: tool_answer(supplier="Acme"))

    with pytest.raises(ExtractionError) as heic:
        await reader.extract(data=b"x", content_type="image/heic")
    with pytest.raises(ExtractionError) as big:
        await reader.extract(data=b"x" * (5 * 1024 * 1024 + 1), content_type="image/jpeg")

    assert (heic.value.reason, big.value.reason) == ("unsupported_type", "too_large")
    assert seen == [], "nothing was sent"


# ===========================================================================
# Choosing the provider from settings (api.expenses.extraction.build)
# ===========================================================================


def test_mistral_selected_without_a_key_fails_at_build_not_on_first_capture() -> None:
    from api.config import Settings
    from api.expenses.extraction.build import ExtractionNotConfigured, build_extractor

    with pytest.raises(ExtractionNotConfigured):
        build_extractor(Settings(extraction_provider="mistral", extraction_mistral_api_key=None))


@pytest.mark.parametrize(
    ("provider", "extra", "model"),
    [
        ("mistral", {"extraction_mistral_api_key": "k"}, "mistral-small-latest"),
        ("vertex-claude", {"extraction_gcp_project": "p"}, "claude-haiku-4-5@20251001"),
    ],
)
def test_an_unset_model_means_the_selected_providers_own_default(
    provider: str, extra: dict[str, str], model: str
) -> None:
    """ADR-095. One shared default once sent a Claude model id to Mistral, and
    every reading failed with a 400 that looked like any other refusal."""
    from api.config import Settings
    from api.expenses.extraction.build import build_extractor

    built = build_extractor(
        Settings(extraction_provider=provider, extraction_model=None, **extra)  # type: ignore[arg-type]
    )

    assert built is not None
    assert built.model == model


def test_mistral_selected_with_a_key_builds_the_mistral_adapter() -> None:
    from api.config import Settings
    from api.expenses.extraction.build import build_extractor

    built = build_extractor(
        Settings(
            extraction_provider="mistral",
            extraction_mistral_api_key="test-key",
            extraction_model="mistral-small-latest",
        )
    )

    assert isinstance(built, MistralExtractor)
    assert built.provider == "mistral"
    assert built.model == "mistral-small-latest"

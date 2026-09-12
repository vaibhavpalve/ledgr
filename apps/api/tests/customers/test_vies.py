"""api.customers.vies - FR-ONB-003, NFR-026.

The one property worth more than the rest, and the reason most of this file
exists: **nothing turns into VALID except an explicit yes from the register.**

Every other test here is a way that could go wrong - a timeout, a 500, an error
page served with a 200, a member state reporting itself down inside an
otherwise successful body, a response with no validity flag at all. Each one
must land on UNAVAILABLE, which says nothing about the number, and never on
VALID (which would zero-rate an intra-Community supply against an unchecked
number) or INVALID (which would reject a real customer because a server was
being patched).

The HTTP layer is exercised through `httpx.MockTransport` rather than a patched
method: `RestViesValidator` is a client of a wire protocol, and a test that
stubbed `validate` would prove the caller calls it and nothing about how the
protocol's failure modes are read.
"""

from __future__ import annotations

import httpx
import pytest

from api.customers.vies import (
    RestViesValidator,
    SyntaxOnlyViesValidator,
    ViesResult,
    ViesStatus,
    build_vies_validator,
)

pytestmark = pytest.mark.anyio

NL = "NL123456789B01"


def _validator(handler: object) -> RestViesValidator:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return RestViesValidator(client=httpx.AsyncClient(transport=transport))


def _responds(payload: object, status_code: int = 200) -> RestViesValidator:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)

    return _validator(handler)


# --- the verdicts -----------------------------------------------------------


async def test_a_confirmed_number_is_valid() -> None:
    validator = _responds(
        {
            "isValid": True,
            "name": "De Vries Holding B.V.",
            "address": "Damrak 70, 1012 LM Amsterdam",
            "requestIdentifier": "WAPIAAAAWjJ2rWvJ",
        }
    )
    result = await validator.validate(NL)

    assert result.status is ViesStatus.VALID
    assert result.vat_number == NL
    assert result.name == "De Vries Holding B.V."
    assert result.consultation_number == "WAPIAAAAWjJ2rWvJ"
    # The evidence for an intra-Community supply is the check ON A DATE.
    assert result.checked_at is not None


async def test_a_denied_number_is_invalid() -> None:
    validator = _responds({"isValid": False})
    result = await validator.validate(NL)

    assert result.status is ViesStatus.INVALID
    assert result.checked_at is not None


async def test_the_request_goes_to_the_country_and_national_parts_separately() -> None:
    """The REST path is /ms/{country}/vat/{number}. Sending the whole number as
    the path segment produces a confident "not found" for a real number.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"isValid": True})

    await _validator(handler).validate("nl 1234.56.789.b01")

    assert seen == ["/taxation_customs/vies/rest-api/ms/NL/vat/123456789B01"]


# --- every way "no answer" arrives ------------------------------------------


@pytest.mark.parametrize(
    ("payload", "status_code", "why"),
    [
        ({"isValid": True}, 500, "a server error, whatever the body claims"),
        ({"isValid": True}, 503, "the service is down"),
        ({"isValid": True}, 404, "the endpoint moved"),
        ("<html>maintenance</html>", 200, "an error page served with a 200"),
        ({}, 200, "a JSON object with no validity flag"),
        ([1, 2, 3], 200, "valid JSON that is not an object"),
        (
            {"actionSucceed": False, "isValid": False},
            200,
            "VIES says the consultation did not succeed - the `isValid: false` "
            "beside it is a default, not a verdict",
        ),
        (
            {"userError": "MS_UNAVAILABLE", "isValid": False},
            200,
            "the member state's own register is down",
        ),
        (
            {"userError": "MS_MAX_CONCURRENT_REQ", "isValid": False},
            200,
            "the member state is throttling us",
        ),
        (
            {"userError": "SERVICE_UNAVAILABLE", "isValid": False},
            200,
            "VIES itself is down",
        ),
        (
            {"userError": "TIMEOUT", "isValid": False},
            200,
            "the national register did not answer in time",
        ),
    ],
)
async def test_no_answer_is_unavailable_and_never_a_verdict(
    payload: object, status_code: int, why: str
) -> None:
    result = await _responds(payload, status_code).validate(NL)

    assert result.status is ViesStatus.UNAVAILABLE, why
    assert not result.status.permits_zero_rating, why
    # Dated even though nothing was learned: "we asked and got nothing" is what
    # explains why the number was never confirmed.
    assert result.checked_at is not None
    # Operator-facing, so an outage is diagnosable from the record.
    assert result.detail


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out reading"),
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=lambda exc: type(exc).__name__,
)
async def test_a_transport_failure_is_unavailable(error: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    result = await _validator(handler).validate(NL)

    assert result.status is ViesStatus.UNAVAILABLE
    assert result.vat_number == NL


async def test_an_outage_never_raises() -> None:
    """NFR-026: core bookkeeping continues when an integration is down. A raise
    here would mean a Dutch bookkeeper cannot save a German customer because a
    server in Saarbrücken is being patched.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    # No pytest.raises: reaching the assertion at all is the test.
    result = await _validator(handler).validate(NL)
    assert isinstance(result, ViesResult)


# --- the syntax gate --------------------------------------------------------


async def test_a_malformed_number_is_refused_without_a_call() -> None:
    """Politeness towards a rate-limited free public service, and - more
    importantly - it keeps "you mistyped this" apart from "the register does
    not recognise this" on the customer record.
    """
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"isValid": True})

    result = await _validator(handler).validate("NL12345B01")

    assert result.status is ViesStatus.SYNTAX_INVALID
    assert not called, "a string that cannot be a VAT number was sent to VIES anyway"
    # No verdict, so no date - migration 0039's customer_vat_verdict_is_dated.
    assert result.checked_at is None


async def test_a_non_eu_number_is_syntax_invalid_rather_than_denied() -> None:
    """A Swiss or British number is real; VIES simply cannot be asked about it.
    Recording INVALID would be a wrong statement about a correct number.
    """
    result = await _responds({"isValid": True}).validate("CH123456789")
    assert result.status is ViesStatus.SYNTAX_INVALID


@pytest.mark.parametrize("empty", [None, "", "   "])
async def test_no_number_is_unchecked_not_invalid(empty: str | None) -> None:
    """A customer without a VAT number is ordinary. "Nobody asked" and "the
    register said no" must not look the same on screen.
    """
    result = await _responds({"isValid": False}).validate(empty)

    assert result.status is ViesStatus.UNCHECKED
    assert result.checked_at is None


# --- withheld fields --------------------------------------------------------


@pytest.mark.parametrize("withheld", ["---", "", "   ", "-", "-----"])
async def test_a_withheld_name_is_none_rather_than_punctuation(withheld: str) -> None:
    """Several member states return `---` for a name they do not disclose.
    Stored verbatim it would show three dashes where a company name belongs.
    """
    result = await _responds({"isValid": True, "name": withheld, "address": withheld}).validate(NL)

    assert result.status is ViesStatus.VALID
    assert result.name is None
    assert result.address is None


async def test_a_missing_name_does_not_weaken_a_valid_verdict() -> None:
    result = await _responds({"isValid": True}).validate(NL)

    assert result.status is ViesStatus.VALID
    assert result.name is None


# --- the statuses themselves ------------------------------------------------


def test_only_valid_permits_zero_rating() -> None:
    """The decision with a tax assessment attached. `!= INVALID` is the
    tempting, wrong way to write it: it would treat both UNCHECKED and
    UNAVAILABLE as good enough for an intra-Community supply.
    """
    permitting = {status for status in ViesStatus if status.permits_zero_rating}
    assert permitting == {ViesStatus.VALID}


def test_exactly_the_three_answered_states_carry_a_date() -> None:
    """Mirrors 0039's customer_vat_verdict_is_dated CHECK. If this and the
    constraint disagreed, the service would write rows the database refuses.
    """
    dated = {status for status in ViesStatus if status.is_a_verdict}
    assert dated == {ViesStatus.VALID, ViesStatus.INVALID, ViesStatus.UNAVAILABLE}


# --- the local stand-in -----------------------------------------------------


async def test_the_syntax_only_validator_never_claims_valid() -> None:
    """It has consulted no register, so it cannot confirm anything. A dev
    environment must not be able to produce a customer that looks verified.
    """
    result = await SyntaxOnlyViesValidator().validate(NL)

    assert result.status is ViesStatus.UNAVAILABLE
    assert not result.status.permits_zero_rating


async def test_the_syntax_only_validator_never_claims_invalid_either() -> None:
    """Symmetric, and just as important: it must not manufacture a rejection
    of a number it has not checked.
    """
    for number in ("NL123456789B01", "DE123456789", "BE0123456789"):
        result = await SyntaxOnlyViesValidator().validate(number)
        assert result.status is not ViesStatus.INVALID


async def test_both_validators_agree_about_what_is_worth_a_call() -> None:
    """A stand-in that accepted strings the real one rejects would let tests
    pass on data production refuses. The shared `_syntax_gate` is what makes
    this hold; this is the test that it keeps holding.
    """
    rest = _responds({"isValid": True})
    for number in ("NL123456789B01", "NL12345B01", "GB123456789", "", None, "   "):
        local_status = (await SyntaxOnlyViesValidator().validate(number)).status
        remote_status = (await rest.validate(number)).status

        if local_status in (ViesStatus.UNCHECKED, ViesStatus.SYNTAX_INVALID):
            assert remote_status == local_status, number
        else:
            # The real one went on to ask; the local one stopped. That is the
            # only permitted difference between them.
            assert remote_status is ViesStatus.VALID, number


def test_build_selects_by_provider_and_has_no_off_switch() -> None:
    assert isinstance(build_vies_validator("syntax-only"), SyntaxOnlyViesValidator)
    assert isinstance(build_vies_validator("vies-rest"), RestViesValidator)

    for absent in ("off", "none", "disabled", ""):
        with pytest.raises(ValueError, match="unknown VIES validator provider"):
            build_vies_validator(absent)

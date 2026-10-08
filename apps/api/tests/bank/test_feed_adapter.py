"""The GoCardless Bank Account Data adapter (ADR-108), against a fake of its HTTP API.

What is pinned here is the translation, not the network: which requests go out, how the provider's
answers become Boeklite's own types, and which failures are refusals (the bank or consent said no)
versus unavailability (try again). No credentials, no network.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from api.bank.adapters import (
    ConsentState,
    FeedNotConfigured,
    GoCardlessBankFeed,
    ProviderRefused,
    ProviderUnavailable,
    UnconfiguredBankFeed,
    build_bank_feed_provider,
    statement_row_from_gocardless,
)

BASE = "https://bankaccountdata.example/api/v2"


class FakeGoCardless:
    """Answers like the real API, and records what it was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.overrides: dict[tuple[str, str], httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v2")
        key = (request.method, path)
        if key in self.overrides:
            return self.overrides[key]
        if key == ("POST", "/token/new/"):
            self.token_calls += 1
            return httpx.Response(200, json={"access": "token-1", "access_expires": 86400})
        if request.headers.get("Authorization") != "Bearer token-1":
            return httpx.Response(401, json={"detail": "no token"})
        if key == ("GET", "/institutions/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "ING_INGBNL2A",
                        "name": "ING",
                        "bic": "INGBNL2A",
                        "logo": "https://x/ing.png",
                    },
                    {"id": "RABOBANK_RABONL2U", "name": "Rabobank", "bic": "RABONL2U"},
                ],
            )
        if key == ("POST", "/agreements/enduser/"):
            return httpx.Response(201, json={"id": "agreement-1"})
        if key == ("POST", "/requisitions/"):
            return httpx.Response(
                201,
                json={"id": "req-1", "link": "https://bank.example/consent/req-1", "status": "CR"},
            )
        if key == ("GET", "/requisitions/req-1/"):
            return httpx.Response(
                200, json={"id": "req-1", "status": "LN", "accounts": ["acc-1", "acc-2"]}
            )
        if key == ("GET", "/accounts/acc-1/details/"):
            return httpx.Response(200, json={"account": {"iban": "NL91ABNA0417164300"}})
        if key == ("GET", "/accounts/acc-1/transactions/"):
            return httpx.Response(
                200,
                json={
                    "transactions": {
                        "booked": [
                            {
                                "transactionId": "t1",
                                "bookingDate": "2026-09-05",
                                "transactionAmount": {"amount": "-12.50", "currency": "EUR"},
                                "creditorName": "KPN",
                                "creditorAccount": {"iban": "NL00 KPN0 0000 0000 00"},
                                "remittanceInformationUnstructured": "Telefoon september",
                            },
                            {
                                "transactionId": "t2",
                                "bookingDate": "2026-09-15",
                                "transactionAmount": {"amount": "605.00", "currency": "EUR"},
                                "debtorName": "De Vries Holding",
                                "debtorAccount": {"iban": "NL00DVH0000000000"},
                                "remittanceInformationUnstructuredArray": ["Factuur", "2026-041"],
                            },
                            {
                                "transactionId": "t3",
                                "bookingDate": "2026-09-16",
                                "transactionAmount": {"amount": "0.00", "currency": "EUR"},
                            },
                        ],
                        "pending": [
                            {
                                "bookingDate": "2026-09-20",
                                "transactionAmount": {"amount": "-99.00", "currency": "EUR"},
                            }
                        ],
                    }
                },
            )
        if key == ("DELETE", "/requisitions/req-1/"):
            return httpx.Response(200, json={"summary": "deleted"})
        return httpx.Response(404, json={"detail": "not found"})


def feed(fake: FakeGoCardless) -> GoCardlessBankFeed:
    return GoCardlessBankFeed(
        secret_id="id",
        secret_key="key",
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(fake),
    )


async def test_lists_institutions_and_reuses_one_token() -> None:
    fake = FakeGoCardless()
    provider = feed(fake)
    institutions = await provider.list_institutions(country="NL")
    await provider.list_institutions(country="NL")

    assert [i.id for i in institutions] == ["ING_INGBNL2A", "RABOBANK_RABONL2U"]
    assert institutions[0].bic == "INGBNL2A" and institutions[1].logo is None
    assert fake.token_calls == 1
    assert fake.requests[1].url.params["country"] == "nl"


async def test_starts_a_consent_with_our_reference_and_return_address() -> None:
    fake = FakeGoCardless()
    link = await feed(fake).start_consent(
        institution_id="ING_INGBNL2A",
        redirect_url="https://boeklite.nl/bank/feed-return",
        reference="conn-42",
        language="nl",
        history_days=90,
        consent_days=90,
    )
    assert link.reference == "req-1"
    assert link.link == "https://bank.example/consent/req-1"

    agreement = json.loads(
        next(r for r in fake.requests if r.url.path.endswith("/agreements/enduser/")).content
    )
    assert agreement == {
        "institution_id": "ING_INGBNL2A",
        "max_historical_days": 90,
        "access_valid_for_days": 90,
        "access_scope": ["details", "transactions"],
    }
    requisition = json.loads(
        next(r for r in fake.requests if r.url.path.endswith("/requisitions/")).content
    )
    assert requisition["reference"] == "conn-42"
    assert requisition["redirect"] == "https://boeklite.nl/bank/feed-return"
    assert requisition["agreement"] == "agreement-1"
    assert requisition["user_language"] == "NL"


@pytest.mark.parametrize(
    ("status", "accounts", "state"),
    [
        ("LN", ["acc-1"], ConsentState.LINKED),
        ("LN", [], ConsentState.FAILED),
        ("GA", [], ConsentState.PENDING),
        ("CR", [], ConsentState.PENDING),
        ("RJ", [], ConsentState.FAILED),
        ("EX", [], ConsentState.EXPIRED),
    ],
)
async def test_reads_the_consent_state(
    status: str, accounts: list[str], state: ConsentState
) -> None:
    fake = FakeGoCardless()
    fake.overrides[("GET", "/requisitions/req-9/")] = httpx.Response(
        200, json={"status": status, "accounts": accounts}
    )
    result = await feed(fake).consent_status(reference="req-9")
    assert result.state is state
    assert result.account_ids == tuple(accounts)


async def test_reads_an_accounts_iban_without_spaces() -> None:
    assert await feed(FakeGoCardless()).account_iban(account_id="acc-1") == "NL91ABNA0417164300"


async def test_turns_booked_lines_into_statement_rows_and_ignores_pending() -> None:
    fake = FakeGoCardless()
    rows = await feed(fake).fetch_transactions(account_id="acc-1", date_from=date(2026, 9, 1))

    assert fake.requests[-1].url.params["date_from"] == "2026-09-01"
    assert len(rows) == 2  # the zero line and the pending line are not imported
    out, money_in = rows
    assert out.amount == Decimal("-12.50") and isinstance(out.amount, Decimal)
    assert out.counterparty_name == "KPN"
    assert out.counterparty_iban == "NL00KPN00000000000"  # spaces gone
    assert out.description == "Telefoon september"
    assert money_in.amount == Decimal("605.00")
    assert money_in.counterparty_name == "De Vries Holding"
    assert money_in.description == "Factuur 2026-041"


def test_a_line_without_a_date_or_amount_is_skipped_not_guessed() -> None:
    assert statement_row_from_gocardless({"transactionAmount": {"amount": "1.00"}}) is None
    assert (
        statement_row_from_gocardless(
            {"bookingDate": "2026-09-01", "transactionAmount": {"amount": "abc"}}
        )
        is None
    )


async def test_a_refusal_and_an_outage_are_told_apart() -> None:
    fake = FakeGoCardless()
    fake.overrides[("GET", "/accounts/gone/transactions/")] = httpx.Response(409, json={})
    fake.overrides[("GET", "/accounts/busy/transactions/")] = httpx.Response(503, json={})
    provider = feed(fake)
    with pytest.raises(ProviderRefused):
        await provider.fetch_transactions(account_id="gone", date_from=date(2026, 9, 1))
    with pytest.raises(ProviderUnavailable):
        await provider.fetch_transactions(account_id="busy", date_from=date(2026, 9, 1))


async def test_a_network_failure_is_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    provider = GoCardlessBankFeed(
        secret_id="id",
        secret_key="key",
        base_url=BASE,
        timeout_seconds=1,
        transport=httpx.MockTransport(broken),
    )
    with pytest.raises(ProviderUnavailable):
        await provider.list_institutions(country="NL")


async def test_revoking_a_consent_already_gone_is_not_an_error() -> None:
    fake = FakeGoCardless()
    await feed(fake).revoke(reference="req-1")
    await feed(fake).revoke(reference="already-deleted")  # 404 at the provider


async def test_without_a_provider_every_call_says_so() -> None:
    provider = UnconfiguredBankFeed()
    assert provider.is_configured is False
    with pytest.raises(FeedNotConfigured):
        await provider.list_institutions(country="NL")
    with pytest.raises(FeedNotConfigured):
        await provider.fetch_transactions(account_id="a", date_from=date(2026, 1, 1))


def test_gocardless_without_both_secrets_is_off_not_broken() -> None:
    assert build_bank_feed_provider("gocardless", secret_id="id").is_configured is False
    assert build_bank_feed_provider("none").is_configured is False
    assert build_bank_feed_provider("gocardless", secret_id="id", secret_key="k").is_configured
    with pytest.raises(ValueError):
        build_bank_feed_provider("plaid")

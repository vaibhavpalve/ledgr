"""The live bank feed (PSD2/AISP) behind an adapter - FR-BNK-001, ADR-108, CLAUDE.md #4.

The same shape `api.customers.peppol` and ADR-081's `InvoiceExtractor` take: a `Protocol` for the
provider, chosen by configuration (`BANK_FEED_PROVIDER`), and an `UnconfiguredBankFeed` that
plainly says nobody has connected one - never that an account has no transactions.

Everything a provider hands back is already in Boeklite's own shape: institutions, a consent link,
the consent's state, and `StatementRow`s, the same rows a statement file parses into. So the
service, the `bank_transaction` table and the reconciliation screen do not know or care which
provider, or which source, a line came from. Replacing GoCardless with Tink or Enable Banking is a
new class in this module and a configuration value; nothing else changes.

--- What is GoCardless-specific, and only here ---

GoCardless Bank Account Data (the former Nordigen) works in four steps:

    POST /token/new/                 our secret id + key -> a short-lived access token
    POST /agreements/enduser/        how much history, for how long, which scopes
    POST /requisitions/              -> a link the person opens to give consent at their bank;
                                        the bank sends them back to `redirect` with ?ref=<ours>
    GET  /requisitions/{id}/         status "LN" (linked) and the consented account ids
    GET  /accounts/{id}/details/     the account's IBAN, to pick the right one
    GET  /accounts/{id}/transactions/?date_from=
                                     booked (final) and pending lines; only booked are used
"""

from __future__ import annotations

import enum
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from api.bank.csv_parser import StatementRow


class BankFeedError(Exception):
    """Base for every refusal a provider adapter raises."""


class FeedNotConfigured(BankFeedError):
    """No provider is configured on this deployment."""


class ProviderUnavailable(BankFeedError):
    """The provider could not be reached, or answered with an error. Retryable."""


class ProviderRefused(BankFeedError):
    """The provider refused this request: an unknown bank, an expired or revoked consent."""


@dataclass(frozen=True, slots=True)
class Institution:
    id: str
    name: str
    bic: str | None
    logo: str | None


@dataclass(frozen=True, slots=True)
class ConsentLink:
    """A consent was started: `reference` is the provider's id for it, `link` where the person
    goes to give it at their bank."""

    reference: str
    link: str


class ConsentState(enum.Enum):
    PENDING = "pending"
    LINKED = "linked"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ConsentStatus:
    state: ConsentState
    account_ids: tuple[str, ...]


class BankFeedProvider(Protocol):
    """A licensed account-information provider, in Boeklite's own terms."""

    @property
    def name(self) -> str: ...

    @property
    def is_configured(self) -> bool: ...

    async def list_institutions(self, *, country: str) -> Sequence[Institution]: ...

    async def start_consent(
        self,
        *,
        institution_id: str,
        redirect_url: str,
        reference: str,
        language: str,
        history_days: int,
        consent_days: int,
    ) -> ConsentLink: ...

    async def consent_status(self, *, reference: str) -> ConsentStatus: ...

    async def account_iban(self, *, account_id: str) -> str | None: ...

    async def fetch_transactions(
        self, *, account_id: str, date_from: date
    ) -> Sequence[StatementRow]: ...

    async def revoke(self, *, reference: str) -> None: ...


class UnconfiguredBankFeed:
    """The provider on a deployment without one. Every call says so; nothing pretends."""

    name = "none"
    is_configured = False

    async def list_institutions(self, *, country: str) -> Sequence[Institution]:
        raise FeedNotConfigured(country)

    async def start_consent(self, **_: Any) -> ConsentLink:
        raise FeedNotConfigured("start_consent")

    async def consent_status(self, *, reference: str) -> ConsentStatus:
        raise FeedNotConfigured(reference)

    async def account_iban(self, *, account_id: str) -> str | None:
        raise FeedNotConfigured(account_id)

    async def fetch_transactions(
        self, *, account_id: str, date_from: date
    ) -> Sequence[StatementRow]:
        raise FeedNotConfigured(account_id)

    async def revoke(self, *, reference: str) -> None:
        raise FeedNotConfigured(reference)


# -- GoCardless Bank Account Data ------------------------------------------------------------

#: Requisition statuses (GoCardless): CR created, GC giving consent, UA undergoing authentication,
#: SA selecting accounts, GA granting access, LN linked, RJ rejected, EX expired, ID/IA suspended.
_PENDING = frozenset({"CR", "GC", "UA", "SA", "GA"})


def _first_text(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = " ".join(str(item).strip() for item in value if str(item).strip())
            if joined:
                return joined
    return None


def statement_row_from_gocardless(raw: dict[str, Any]) -> StatementRow | None:
    """One booked transaction as a `StatementRow`, or None for a line without a usable date or
    a non-zero amount. The amount is read as a decimal STRING (NFR-031): it is never a float."""
    raw_date = raw.get("bookingDate") or raw.get("valueDate")
    amount_block = raw.get("transactionAmount") or {}
    try:
        booking_date = date.fromisoformat(str(raw_date))
        amount = Decimal(str(amount_block.get("amount")))
    except (ValueError, InvalidOperation):
        return None
    if amount == 0:
        return None
    # Money out names its creditor; money in its debtor.
    if amount < 0:
        name = raw.get("creditorName")
        account = raw.get("creditorAccount") or {}
    else:
        name = raw.get("debtorName")
        account = raw.get("debtorAccount") or {}
    iban = account.get("iban") if isinstance(account, dict) else None
    description = _first_text(
        raw.get("remittanceInformationUnstructured"),
        raw.get("remittanceInformationUnstructuredArray"),
        raw.get("remittanceInformationStructured"),
        raw.get("additionalInformation"),
    )
    return StatementRow(
        booking_date=booking_date,
        amount=amount,
        counterparty_name=_first_text(name),
        counterparty_iban=(iban or "").replace(" ", "").upper() or None,
        description=description,
    )


class GoCardlessBankFeed:
    """GoCardless Bank Account Data over its REST API. The access token is fetched with the
    secret id and key and reused until shortly before it expires."""

    name = "gocardless"

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._token: str | None = None
        self._token_valid_until = 0.0

    @property
    def is_configured(self) -> bool:
        return True

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, transport=self._transport
        )

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token is not None and time.monotonic() < self._token_valid_until:
            return self._token
        response = await self._send(
            client,
            "POST",
            "/token/new/",
            json={"secret_id": self._secret_id, "secret_key": self._secret_key},
            authenticated=False,
        )
        token = response.get("access")
        if not isinstance(token, str):
            raise ProviderUnavailable("no access token in the provider's answer")
        expires = response.get("access_expires")
        lifetime = float(expires) if isinstance(expires, (int, float)) else 3600.0
        self._token = token
        self._token_valid_until = time.monotonic() + max(lifetime - 60.0, 0.0)
        return token

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {await self._access_token(client)}"
        try:
            response = await client.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(type(exc).__name__) from exc
        if response.status_code in {400, 401, 403, 404, 409, 410}:
            # The provider's own words stay out of the message: they can echo identifiers.
            raise ProviderRefused(f"{method} {path.split('?')[0]} -> {response.status_code}")
        if response.status_code >= 300:
            raise ProviderUnavailable(f"{method} {path.split('?')[0]} -> {response.status_code}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def list_institutions(self, *, country: str) -> Sequence[Institution]:
        async with self._client() as client:
            raw = await self._send(
                client, "GET", "/institutions/", params={"country": country.lower()}
            )
        if not isinstance(raw, list):
            raise ProviderUnavailable("institutions is not a list")
        return [
            Institution(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                bic=item.get("bic") or None,
                logo=item.get("logo") or None,
            )
            for item in raw
            if isinstance(item, dict) and item.get("id")
        ]

    async def start_consent(
        self,
        *,
        institution_id: str,
        redirect_url: str,
        reference: str,
        language: str,
        history_days: int,
        consent_days: int,
    ) -> ConsentLink:
        async with self._client() as client:
            agreement = await self._send(
                client,
                "POST",
                "/agreements/enduser/",
                json={
                    "institution_id": institution_id,
                    "max_historical_days": history_days,
                    "access_valid_for_days": consent_days,
                    "access_scope": ["details", "transactions"],
                },
            )
            requisition = await self._send(
                client,
                "POST",
                "/requisitions/",
                json={
                    "redirect": redirect_url,
                    "institution_id": institution_id,
                    "reference": reference,
                    "agreement": agreement.get("id"),
                    "user_language": language.upper(),
                },
            )
        requisition_id = requisition.get("id")
        link = requisition.get("link")
        if not isinstance(requisition_id, str) or not isinstance(link, str):
            raise ProviderUnavailable("requisition without id or link")
        return ConsentLink(reference=requisition_id, link=link)

    async def consent_status(self, *, reference: str) -> ConsentStatus:
        async with self._client() as client:
            raw = await self._send(client, "GET", f"/requisitions/{reference}/")
        status = str(raw.get("status", ""))
        accounts = tuple(str(account) for account in raw.get("accounts") or ())
        if status == "LN":
            state = ConsentState.LINKED if accounts else ConsentState.FAILED
        elif status in _PENDING:
            state = ConsentState.PENDING
        elif status == "EX":
            state = ConsentState.EXPIRED
        else:
            state = ConsentState.FAILED
        return ConsentStatus(state=state, account_ids=accounts)

    async def account_iban(self, *, account_id: str) -> str | None:
        async with self._client() as client:
            raw = await self._send(client, "GET", f"/accounts/{account_id}/details/")
        account = raw.get("account") or {}
        iban = account.get("iban") if isinstance(account, dict) else None
        return iban.replace(" ", "").upper() if isinstance(iban, str) and iban else None

    async def fetch_transactions(
        self, *, account_id: str, date_from: date
    ) -> Sequence[StatementRow]:
        async with self._client() as client:
            raw = await self._send(
                client,
                "GET",
                f"/accounts/{account_id}/transactions/",
                params={"date_from": date_from.isoformat()},
            )
        booked = (raw.get("transactions") or {}).get("booked") or []
        rows = [statement_row_from_gocardless(item) for item in booked if isinstance(item, dict)]
        return [row for row in rows if row is not None]

    async def revoke(self, *, reference: str) -> None:
        async with self._client() as client:
            try:
                await self._send(client, "DELETE", f"/requisitions/{reference}/")
            except ProviderRefused:
                # Already gone at the provider (expired, or deleted there): revoked either way.
                return


def build_bank_feed_provider(
    provider: str,
    *,
    secret_id: str | None = None,
    secret_key: str | None = None,
    base_url: str = "https://bankaccountdata.gocardless.com/api/v2",
    timeout_seconds: float = 20.0,
) -> BankFeedProvider:
    if provider == "none":
        return UnconfiguredBankFeed()
    if provider == "gocardless":
        if not secret_id or not secret_key:
            return UnconfiguredBankFeed()
        return GoCardlessBankFeed(
            secret_id=secret_id,
            secret_key=secret_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"unknown bank feed provider: {provider!r}")

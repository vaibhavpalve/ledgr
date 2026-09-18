"""KvK number lookup - SI-03: auto-fill a new customer's legal name, trade
name and address from the KvK (Kamer van Koophandel, the Dutch Chamber of
Commerce) register, given only the number, before anything is saved.

CLAUDE.md's fourth architectural non-negotiable, the same shape
`api.customers.vies` already takes for VIES: a Protocol the caller depends
on, implementations selected by configuration, and nothing above this module
knowing that the KvK register speaks HTTP.

--- Why this is a lookup, not a validator ---

`ViesValidator` answers a yes/no about a number a CUSTOMER RECORD already
holds, and it writes a verdict back onto that record (migration 0039).
`KvkLookupService` answers a completely different question - "what does the
register know about this number" - asked from an EMPTY "new customer" form,
before any record exists to hold a verdict. There is nothing to persist here
at all: the caller uses the answer to fill in blank fields locally, exactly
once, and the KvK register is not consulted again just because the customer
was saved.

--- "Unavailable" is a verdict, not an error - same reasoning as VIES ---

A KvK outage, rate limit or malformed response must not stop somebody from
typing a customer in by hand: NFR-026 applies here exactly as it does to
VIES. `KvkLookupStatus.UNAVAILABLE` is the honest answer for "the register
did not tell us," and it is never conflated with `NOT_FOUND` ("the register
positively has no such number") - a screen that showed the wrong one of
those two would either invent a company that does not exist or tell someone
their real KvK number is wrong because the network hiccupped.

--- The real client's response shape is UNVERIFIED against a live account ---

`RestKvkLookup` targets the KvK "Zoeken" (search) API v2 as publicly
documented. Unlike VIES - a single, stable, EU-published REST contract this
codebase has tested end to end - the KvK API requires a registered API key
this deployment does not have, and the exact field names below
(`resultaten`, `naam`, `adres.straatnaam`, `adres.huisnummer`,
`adres.postcode`, `adres.plaats`) are transcribed from memory of that
documentation, not verified against a real response. Every parsing step
therefore fails to UNAVAILABLE rather than raising or guessing, so a schema
drift degrades to "look it up yourself" instead of silently filling a form
with wrong data. Treat this adapter as unverified until it has been run
once against KvK's own test environment with a real key - see
`build_kvk_lookup`'s docstring.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

__all__ = [
    "KvkLookupStatus",
    "KvkLookupResult",
    "KvkLookupService",
    "RestKvkLookup",
    "SyntaxOnlyKvkLookup",
    "build_kvk_lookup",
]

#: KvK's public "Zoeken" (search) API v2. `apikey` is a header, not a query
#: parameter - KvK's own documented convention, unlike VIES which needs no key
#: at all.
_KVK_SEARCH_URL = "https://api.kvk.nl/api/v1/zoeken"

#: A KvK number is exactly eight digits. There is no letter suffix and no
#: publicly simple checksum the way an EU VAT number has one, so this is a
#: shape check only - the register itself is the authority on whether eight
#: digits are actually issued to anyone.
_KVK_NUMBER = re.compile(r"^\d{8}$")


class KvkLookupStatus(enum.Enum):
    """Five states, the same discipline `ViesStatus` uses and for the same
    reason: collapsing UNAVAILABLE into NOT_FOUND would tell someone their
    real KvK number does not exist because the register timed out.
    """

    UNCHECKED = "unchecked"
    SYNTAX_INVALID = "syntax_invalid"
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class KvkLookupResult:
    """One lookup. Nothing here is persisted anywhere - see the module
    docstring - so this is purely what the caller hands back to a form.
    """

    status: KvkLookupStatus
    #: The number as asked about, normalised (digits only). None where there
    #: was nothing to ask about.
    kvk_number: str | None
    legal_name: str | None = None
    #: The establishment's own trading name, where the register discloses one
    #: distinct from the legal name. Often identical to `legal_name` for a
    #: single-establishment business, which is not an error - see
    #: `RestKvkLookup`'s own note on why this adapter does not distinguish
    #: the two any harder than KvK's search result already does.
    trade_name: str | None = None
    address_line1: str | None = None
    postal_code: str | None = None
    city: str | None = None
    #: Operator-facing. Never shown to a user (FR-UX-007).
    detail: str | None = None

    @classmethod
    def unchecked(cls, kvk_number: str | None = None) -> KvkLookupResult:
        return cls(status=KvkLookupStatus.UNCHECKED, kvk_number=kvk_number)


class KvkLookupService(Protocol):
    """The seam. Takes the raw string a person typed, not a normalised
    number - deciding whether it parses is part of looking it up, the same
    reasoning `ViesValidator.validate` documents.
    """

    async def lookup(self, kvk_number: str | None) -> KvkLookupResult: ...


def _syntax_gate(kvk_number: str | None) -> tuple[str | None, KvkLookupResult | None]:
    """Shared by both implementations, mirroring `api.customers.vies`'s own
    `_syntax_gate` - written once so a stand-in cannot accept a shape the
    real adapter would reject.
    """
    if kvk_number is None or not kvk_number.strip():
        return None, KvkLookupResult.unchecked()

    normalised = re.sub(r"\s", "", kvk_number.strip())
    if not _KVK_NUMBER.match(normalised):
        return None, KvkLookupResult(
            status=KvkLookupStatus.SYNTAX_INVALID,
            kvk_number=kvk_number.strip(),
            detail="not eight digits",
        )
    return normalised, None


class SyntaxOnlyKvkLookup:
    """Dev and test only. NOT a KvK client - it makes no network call.

    Named for what it is, matching `SyntaxOnlyViesValidator`,
    `LocalDenylistBreachChecker` and every other "this is a stand-in" adapter
    in this codebase - finding it in a production configuration should be
    obviously wrong.
    """

    name = "syntax-only"

    async def lookup(self, kvk_number: str | None) -> KvkLookupResult:
        normalised, refusal = _syntax_gate(kvk_number)
        if refusal is not None:
            return refusal
        assert normalised is not None

        return KvkLookupResult(
            status=KvkLookupStatus.UNAVAILABLE,
            kvk_number=normalised,
            detail=(
                "no KvK lookup was made: this deployment is configured with the "
                "syntax-only lookup, which cannot confirm a number exists"
            ),
        )


class RestKvkLookup:
    """Production. See the module docstring's caveat on the response shape."""

    name = "kvk-api"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _KVK_SEARCH_URL,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def lookup(self, kvk_number: str | None) -> KvkLookupResult:
        normalised, refusal = _syntax_gate(kvk_number)
        if refusal is not None:
            return refusal
        assert normalised is not None

        try:
            response = await self._client.get(
                self._base_url,
                params={"kvkNummer": normalised},
                headers={"apikey": self._api_key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # Timeout, DNS, TLS, connection reset - all "we do not know",
            # none of them "this number does not exist".
            return self._unavailable(normalised, f"{type(exc).__name__}: {exc}")

        if response.status_code == 404:
            return KvkLookupResult(status=KvkLookupStatus.NOT_FOUND, kvk_number=normalised)
        if response.status_code >= 400:
            return self._unavailable(normalised, f"HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError:
            return self._unavailable(normalised, "response was not JSON")
        if not isinstance(body, dict):
            return self._unavailable(normalised, "response was not a JSON object")

        results = body.get("resultaten")
        if not isinstance(results, list):
            # Wrong SHAPE - we do not understand this response, which is a
            # different fact from the register positively returning zero
            # matches below.
            return self._unavailable(normalised, "response carried no 'resultaten' list")
        if not results:
            return KvkLookupResult(status=KvkLookupStatus.NOT_FOUND, kvk_number=normalised)

        first = results[0]
        if not isinstance(first, dict):
            return self._unavailable(normalised, "malformed result entry")

        name = _text(first.get("naam"))
        raw_address = first.get("adres")
        address = raw_address if isinstance(raw_address, dict) else {}

        return KvkLookupResult(
            status=KvkLookupStatus.FOUND,
            kvk_number=normalised,
            legal_name=name,
            # The search result names one establishment, not the legal
            # entity's separate "handelsnamen" list (only the fuller
            # "basisprofiel" endpoint carries that) - see the module
            # docstring. Using the same name for both is the honest answer
            # for what THIS call actually learned, not a claim that the two
            # are always identical.
            trade_name=name,
            address_line1=_street_line(address),
            postal_code=_text(address.get("postcode")),
            city=_text(address.get("plaats")),
        )

    def _unavailable(self, kvk_number: str, detail: str) -> KvkLookupResult:
        return KvkLookupResult(
            status=KvkLookupStatus.UNAVAILABLE, kvk_number=kvk_number, detail=detail
        )


def _text(value: object) -> str | None:
    """A field the register actually filled in, or None - the same
    `_disclosed` helper `api.customers.vies` uses, minus VIES's `"---"`
    convention, which KvK's API does not share.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _street_line(address: dict[str, object]) -> str | None:
    """`address_line1` is one string (migration 0039) - "Damrak 70" - not the
    separate `straatnaam`/`huisnummer` KvK returns.
    """
    street = _text(address.get("straatnaam"))
    if street is None:
        return None
    house_number = address.get("huisnummer")
    if house_number in (None, ""):
        return street
    return f"{street} {house_number}"


def build_kvk_lookup(provider: str, *, api_key: str | None = None) -> KvkLookupService:
    """Selects the lookup from configuration, the same shape
    `build_vies_validator` and `api.crypto.kms.build_kms` already take.

    Before selecting `kvk-api` in a real deployment: KvK issues API keys
    through https://developers.kvk.nl, including a published test
    environment with a fixed test key and fixed test numbers - that is the
    first place this adapter should be exercised, precisely because its
    response-parsing was written from documentation, not from a live call
    (see the module docstring).
    """
    if provider == "syntax-only":
        return SyntaxOnlyKvkLookup()
    if provider == "kvk-api":
        if not api_key:
            raise ValueError("kvk_provider is 'kvk-api' but KVK_API_KEY is not set")
        return RestKvkLookup(api_key=api_key)
    raise ValueError(
        f"unknown KvK lookup provider {provider!r}. 'syntax-only' is dev and test "
        f"only and never confirms a number exists; a production deployment needs "
        f"'kvk-api' (SI-03)."
    )

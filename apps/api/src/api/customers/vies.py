"""VIES VAT number validation - FR-ONB-003, and PRD §13's integration surface.

    FR-ONB-003  Capture and validate BTW-identificatienummer and OB-nummer;
                validate EU VAT numbers via VIES.

CLAUDE.md's fourth architectural non-negotiable: a Protocol the domain depends
on, implementations selected by config, and nothing above this module knowing
that VIES speaks HTTP. The same shape `api.crypto.kms`, `api.auth.breach_check`
and `api.documents.scanning` take.

--- "Unavailable" is a verdict, not an error ---

This is the decision the rest of the module is built around.

VIES is not one service. It is a thin federating layer in front of 27 national
registers, and it fails PER MEMBER STATE, without notice, routinely - the
Commission publishes an availability page precisely because outages are
expected. A design that treated "no answer" as an exception would mean a Dutch
bookkeeper cannot save a German customer this afternoon because a server in
Saarbrücken is being patched.

NFR-026 settles it: "if OCR, bank feeds or Peppol are unavailable, core
bookkeeping continues and queued work resumes automatically". So
`ViesStatus.UNAVAILABLE` is a normal return value, stored on the customer row
by migration 0039, and the customer saves either way. Nothing in this package
raises because the Commission is having a bad day.

The corollary matters just as much: UNAVAILABLE must never be recorded as
VALID. An unchecked number silently marked valid is how an intra-Community
supply gets zero-rated against a number that was deregistered two years ago,
and the assessment for that lands on the supplier.

--- Why the check is stored rather than recomputed ---

For an intra-Community supply (`btw_icp`), zero-rating depends on the customer
holding a valid VAT identification number, and the supplier's defence when the
Belastingdienst asks is the evidence that they checked - on a date, with a
result. That is a historical fact about a moment, not a property that can be
recomputed later, and it is the same argument migration 0037 makes for freezing
VAT totals and CMP-014 makes for effective-dated rates.

Hence `consultation_number`: VIES issues one when the caller identifies itself
as a requester, and it is the reference a tax authority asks for. It is
recorded when the service returns one and never fabricated.

--- Syntax first, and the network second ---

`parse` (api.customers.vat_number) runs before any call. A string that cannot
be a VAT number gets `SYNTAX_INVALID` with no request made. That is not only
politeness towards a free public service with rate limits - it also keeps the
two failures apart on the customer record, and "you have mistyped this" and
"the register does not recognise this" need different sentences in front of a
person.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from api.customers.vat_number import VatNumber, parse

__all__ = [
    "ViesStatus",
    "ViesResult",
    "ViesValidator",
    "RestViesValidator",
    "SyntaxOnlyViesValidator",
    "build_vies_validator",
]

#: The Commission's REST front end for VIES. The older SOAP endpoint
#: (checkVatService) is still published, and this is deliberately not it: the
#: REST service returns the same fields as JSON, needs no XML toolchain, and
#: reports per-member-state availability in the body rather than as a SOAP
#: fault - which is exactly the distinction UNAVAILABLE depends on.
_VIES_CHECK_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}"


class ViesStatus(enum.Enum):
    """Mirrors the `vat_number_status` CHECK in migration 0039.

    Five states and no boolean anywhere, because collapsing any two of them
    loses something a person needs:

        UNCHECKED       nobody has asked. Not a judgment.
        SYNTAX_INVALID  cannot be a VAT number; no call was made.
        VALID           the member state's register confirmed it.
        INVALID         the member state's register denied it.
        UNAVAILABLE     VIES could not answer. Says nothing about the number.
    """

    UNCHECKED = "unchecked"
    SYNTAX_INVALID = "syntax_invalid"
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"

    @property
    def is_a_verdict(self) -> bool:
        """Whether the register actually answered.

        VALID, INVALID and UNAVAILABLE are the three that carry a timestamp in
        0039 - UNAVAILABLE included, because "we asked on this date and got
        nothing" is itself a fact worth having when somebody asks why the
        number was never confirmed.
        """
        return self in (ViesStatus.VALID, ViesStatus.INVALID, ViesStatus.UNAVAILABLE)

    @property
    def permits_zero_rating(self) -> bool:
        """Whether an intra-Community supply may be zero-rated on this alone.

        Only VALID. Stated as a property rather than left to each caller,
        because this is the decision with an assessment attached to it and
        `!= INVALID` is the tempting, wrong way to write it - it would treat
        both UNCHECKED and UNAVAILABLE as good enough.

        Not yet consulted by anything: `btw_icp` invoices are gated on the
        customer VAT number being PRESENT (api.invoicing.statutory), not on it
        being confirmed. Tightening that is FR-VAT-006's ICP work - see
        ADR-038's gaps.
        """
        return self is ViesStatus.VALID


@dataclass(frozen=True, slots=True)
class ViesResult:
    """One consultation. Everything migration 0039 stores, and nothing else."""

    status: ViesStatus
    #: The number as asked about, normalised. None where there was nothing to
    #: ask about (an empty field, or a non-EU prefix).
    vat_number: str | None
    checked_at: datetime | None = None
    #: The registered name, where the member state discloses one. Several do
    #: not, and an absent name is not a weaker VALID.
    name: str | None = None
    address: str | None = None
    #: VIES's own reference for this consultation, where one was issued.
    consultation_number: str | None = None
    #: Operator-facing. Never shown to a user (FR-UX-007) - the user-facing
    #: sentence comes from the catalogue, keyed on `status`.
    detail: str | None = None

    @classmethod
    def unchecked(cls, vat_number: str | None = None) -> ViesResult:
        return cls(status=ViesStatus.UNCHECKED, vat_number=vat_number)


class ViesValidator(Protocol):
    """The seam. Takes the raw string a person typed, not a parsed number:
    deciding whether it parses is part of validating it, and a caller made to
    parse first would have to invent an answer for the case where it does not.
    """

    async def validate(self, vat_number: str | None) -> ViesResult: ...


def _syntax_gate(vat_number: str | None) -> tuple[VatNumber | None, ViesResult | None]:
    """Shared by both implementations: parse, or produce the refusal.

    Returns the parsed number and no result, or no number and the result to
    return. Written once because the two implementations must agree about what
    is worth a network call - a stand-in that accepted strings the real one
    rejects would let tests pass on data production refuses.
    """
    if vat_number is None or not vat_number.strip():
        return None, ViesResult.unchecked()

    parsed = parse(vat_number)
    if parsed is None:
        return None, ViesResult(
            status=ViesStatus.SYNTAX_INVALID,
            vat_number=vat_number.strip().upper(),
            detail="not a well-formed EU VAT number, or not an EU member state prefix",
        )
    return parsed, None


class SyntaxOnlyViesValidator:
    """Dev and test only. NOT a VIES client - it makes no network call.

    It runs the format check and stops, returning UNAVAILABLE for anything
    well-formed. That is the honest answer for an implementation that has not
    consulted a register: it never claims VALID, so a development environment
    cannot produce a customer record that looks confirmed and is not, and it
    never claims INVALID, so it cannot manufacture a rejection either.

    Named for what it is, so finding it in a production configuration is
    obviously wrong - the posture `api.documents.scanning.LocalPatternScanner`
    and `api.auth.breach_check.LocalDenylistBreachChecker` take for the same
    reason.
    """

    name = "syntax-only"

    async def validate(self, vat_number: str | None) -> ViesResult:
        parsed, refusal = _syntax_gate(vat_number)
        if refusal is not None:
            return refusal
        assert parsed is not None

        return ViesResult(
            status=ViesStatus.UNAVAILABLE,
            vat_number=parsed.value,
            checked_at=datetime.now(UTC),
            detail=(
                "no VIES consultation was made: this deployment is configured with "
                "the syntax-only validator, which cannot confirm a number exists"
            ),
        )


class RestViesValidator:
    """Production. The Commission's VIES REST service.

    Every failure mode below resolves to UNAVAILABLE rather than an exception,
    and that is the module's central decision - see the docstring. The one
    thing that is never inferred is VALID: it is returned only when the service
    explicitly says `"valid": true`.
    """

    name = "vies-rest"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        # Eight seconds rather than the three `api.auth.breach_check` allows
        # HIBP. VIES forwards to a national register synchronously and is
        # genuinely slow; a tight timeout here would turn ordinary latency into
        # a stream of UNAVAILABLE verdicts, which is the specific failure that
        # makes people stop trusting the field.
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def validate(self, vat_number: str | None) -> ViesResult:
        parsed, refusal = _syntax_gate(vat_number)
        if refusal is not None:
            return refusal
        assert parsed is not None

        url = _VIES_CHECK_URL.format(country=parsed.country_code, number=parsed.national_number)
        try:
            response = await self._client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            # Timeout, DNS, TLS, connection reset. All of them are "we do not
            # know", none of them is "the number is bad".
            return self._unavailable(parsed, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            return self._unavailable(parsed, f"HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError:
            # A 200 carrying an error page. It happens during maintenance, and
            # a body we cannot read is not a verdict.
            return self._unavailable(parsed, "response was not JSON")

        if not isinstance(body, dict):
            return self._unavailable(parsed, "response was not a JSON object")

        # The service reports a member state being down IN THE BODY of an
        # otherwise successful response. Checked before `valid`, because a
        # response carrying both a service fault and a default `valid: false`
        # would otherwise be recorded as a rejection of a number nobody looked
        # at - the exact confusion this module exists to prevent.
        fault = body.get("userError") or body.get("actionSucceed")
        if isinstance(fault, str) and fault.upper() not in ("VALID", "INVALID"):
            return self._unavailable(parsed, f"VIES reported {fault}")
        if body.get("actionSucceed") is False:
            return self._unavailable(parsed, "VIES reported the consultation did not succeed")

        valid = body.get("isValid")
        if valid is None:
            valid = body.get("valid")
        if not isinstance(valid, bool):
            return self._unavailable(parsed, "response carried no validity flag")

        return ViesResult(
            status=ViesStatus.VALID if valid else ViesStatus.INVALID,
            vat_number=parsed.value,
            checked_at=datetime.now(UTC),
            # Several member states return the literal "---" for a name they do
            # not disclose. Kept as None rather than stored, so a screen does
            # not show three dashes where a company name belongs.
            name=_disclosed(body.get("name")),
            address=_disclosed(body.get("address")),
            consultation_number=_disclosed(body.get("requestIdentifier")),
        )

    def _unavailable(self, parsed: VatNumber, detail: str) -> ViesResult:
        return ViesResult(
            status=ViesStatus.UNAVAILABLE,
            vat_number=parsed.value,
            # Dated even though nothing was learned: "we asked on this date and
            # got nothing" is the fact that explains why the number was never
            # confirmed, and it is what a retry schedule reads.
            checked_at=datetime.now(UTC),
            detail=detail,
        )


def _disclosed(value: object) -> str | None:
    """A field the member state actually filled in, or None.

    VIES returns `"---"` for withheld values and empty strings for absent
    ones. Both mean "not disclosed", and storing either verbatim would put
    punctuation where a company name should be.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or set(text) <= {"-"}:
        return None
    return text


def build_vies_validator(provider: str) -> ViesValidator:
    """Selects the validator from configuration.

    There is no "off". A deployment that wanted no VAT validation at all would
    set every customer to UNCHECKED, which `SyntaxOnlyViesValidator` already
    approximates honestly - and an explicit off switch is the one somebody
    finds enabled during a VAT audit.
    """
    if provider == "syntax-only":
        return SyntaxOnlyViesValidator()
    if provider == "vies-rest":
        return RestViesValidator()
    raise ValueError(
        f"unknown VIES validator provider {provider!r}. 'syntax-only' is dev and "
        f"test only and never confirms a number exists; a production deployment "
        f"needs 'vies-rest' (FR-ONB-003)."
    )

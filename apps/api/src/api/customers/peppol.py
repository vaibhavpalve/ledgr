"""Peppol participant ID discovery - FR-AR-006's last field, and a STUB.

    FR-AR-006  Customer master with ... Peppol participant ID discovery ...
    FR-AR-005  ... structured e-invoice over Peppol (BIS Billing 3.0 / NLCIUS,
               EN 16931) added in P2.

Peppol delivery is P2 (PRD §5). This module is the interface it will implement,
built now so that the customer master has a shape to hold the identifier and so
that FR-AR-005 has a seam to fill rather than a refactor to perform. It performs
no lookup.

--- What a stub is allowed to say ---

This is the only interesting decision here, and it is a safety one.

The tempting stub returns "not registered" and lets everything downstream carry
on. It is wrong, and the wrongness is invisible: "this customer is not on the
Peppol network" and "this deployment has not been configured to look" are
different facts with different consequences. The first correctly routes an
invoice to email. The second routes it to email while the customer sits waiting
for it on Peppol, and nobody finds out until the invoice is overdue - or, once
the ViDA mandate lands (PRD §2.1), until a structured invoice was legally
required and a PDF was sent instead.

So `NOT_CONFIGURED` exists as its own outcome and is what the stub returns. It
never becomes `NOT_REGISTERED`, and there is no default that collapses them.

--- Why the identifier is still storable ---

Discovery is not the only way a participant ID arrives. A customer can simply
tell their supplier what theirs is, and a business that knows it should be able
to record it. So `customer.peppol_participant_id` (migration 0039) is writable
directly, and this module's absence limits DISCOVERY, not the field.

--- How discovery will actually work, when it lands ---

Peppol's SMP/SML lookup is a DNS query for a hash of the participant
identifier, followed by an HTTP fetch of the Service Metadata Publisher record
it points to, and then a check that the participant advertises the document
type being sent. Two consequences are already visible in the interface below:

  * a participant ID is not enough. `supports_document_type` exists because a
    participant registered to RECEIVE orders but not invoices is a real and
    common state, and treating "found in the SML" as "can be sent an invoice"
    is the standard way to get a delivery rejected by the receiving access
    point.
  * discovery is derived from an identifier the customer already has - a KvK
    number under scheme 0106, or a VAT number under 9944. Hence
    `candidate_identifiers`, which is pure and testable now, independently of
    any network.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from api.customers.vat_number import parse as parse_vat_number

__all__ = [
    "PeppolScheme",
    "PeppolParticipantId",
    "DiscoveryStatus",
    "DiscoveryResult",
    "PeppolDirectory",
    "UnconfiguredPeppolDirectory",
    "build_peppol_directory",
    "candidate_identifiers",
]

#: The invoice document Peppol delivery would ask about. Named here rather than
#: passed as a bare string by every caller, because a typo in it produces
#: "participant does not support this document type", which reads like a fact
#: about the customer.
BIS_BILLING_INVOICE = (
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice"
    "##urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0::2.1"
)


class PeppolScheme(enum.Enum):
    """The ICD schemes a Dutch supplier's customers are addressed under.

    Not the full Peppol code list, which runs to hundreds of entries. These are
    the two that matter for NL and the one for a generic EU VAT-addressed
    participant; anything else arrives as a value somebody typed in, which
    migration 0039's CHECK accepts as four digits and a colon.
    """

    #: Dutch Chamber of Commerce number. The usual way a Dutch business is
    #: addressed on Peppol.
    KVK = "0106"
    #: Dutch OIN, used by government bodies (Digipoort's world).
    OIN = "0190"
    #: EU VAT number.
    VAT = "9944"


@dataclass(frozen=True, slots=True)
class PeppolParticipantId:
    """`scheme:identifier`, the form Peppol addresses on and 0039 stores."""

    scheme: str
    identifier: str

    @property
    def value(self) -> str:
        return f"{self.scheme}:{self.identifier}"

    def __str__(self) -> str:
        return self.value


class DiscoveryStatus(enum.Enum):
    """The outcome of a lookup, including the two that are not about the
    customer at all.
    """

    #: The participant is registered and can receive the document type asked
    #: about.
    REGISTERED = "registered"
    #: The participant is not in the network. A real, negative fact.
    NOT_REGISTERED = "not_registered"
    #: Registered, but not for this document type. See the module docstring.
    DOCUMENT_TYPE_UNSUPPORTED = "document_type_unsupported"
    #: The network could not be reached. Says nothing about the customer -
    #: NFR-026's degradation, the same posture `api.customers.vies` takes.
    UNAVAILABLE = "unavailable"
    #: NOTHING WAS LOOKED UP. This deployment has no directory configured.
    #: Deliberately distinct from NOT_REGISTERED - see the module docstring.
    NOT_CONFIGURED = "not_configured"

    @property
    def is_deliverable(self) -> bool:
        """Whether an invoice may be SENT over Peppol on the strength of this.

        Only REGISTERED. Written as a property for the reason
        `ViesStatus.permits_zero_rating` is: the tempting `!= NOT_REGISTERED`
        would treat NOT_CONFIGURED as deliverable, which is precisely the
        failure this module is shaped to prevent.
        """
        return self is DiscoveryStatus.REGISTERED


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    status: DiscoveryStatus
    participant_id: PeppolParticipantId | None = None
    #: Which identifiers were tried, in order. Kept because "we looked and
    #: found nothing" is only meaningful if you know what was looked for - a
    #: customer with no KvK number and no VAT number cannot be discovered at
    #: all, and that is a data gap rather than an absence from the network.
    tried: tuple[PeppolParticipantId, ...] = ()
    #: Operator-facing. Never shown to a user (FR-UX-007).
    detail: str | None = None


class PeppolDirectory(Protocol):
    """CLAUDE.md non-negotiable #4's adapter seam, for the Peppol SMP/SML.

    Takes the identifiers rather than a customer row, so the P2 implementation
    needs no knowledge of this system's schema and can be exercised against the
    published test participants without a database.
    """

    async def discover(
        self,
        candidates: tuple[PeppolParticipantId, ...],
        *,
        document_type: str = BIS_BILLING_INVOICE,
    ) -> DiscoveryResult: ...


class UnconfiguredPeppolDirectory:
    """The P2 placeholder. Looks nothing up and says so.

    Returns NOT_CONFIGURED, never NOT_REGISTERED. The distinction is the whole
    reason this class exists rather than a `return None` at the call site: a
    caller handed None has to invent a meaning for it, and the meaning it will
    invent is "not on Peppol".
    """

    name = "unconfigured"

    async def discover(
        self,
        candidates: tuple[PeppolParticipantId, ...],
        *,
        document_type: str = BIS_BILLING_INVOICE,
    ) -> DiscoveryResult:
        return DiscoveryResult(
            status=DiscoveryStatus.NOT_CONFIGURED,
            participant_id=None,
            tried=candidates,
            detail=(
                "no Peppol directory is configured; participant discovery is P2 "
                "(FR-AR-005). This is not a statement about whether the customer "
                "is on the network."
            ),
        )


def candidate_identifiers(
    *, kvk_number: str | None, vat_number: str | None
) -> tuple[PeppolParticipantId, ...]:
    """The participant IDs this customer might be addressed under, best first.

    Pure, and testable now - which is the point of writing it before the
    lookup exists. KvK first because scheme 0106 is how a Dutch business is
    ordinarily registered on Peppol, and because a Dutch VAT number identifies
    a fiscal entity that may cover several registered businesses, so a 9944
    match is the weaker evidence of the two.

    A VAT number is only offered as a candidate if it PARSES: an unnormalised
    or malformed string would be a lookup guaranteed to miss, recorded as the
    customer not being on the network.
    """
    candidates: list[PeppolParticipantId] = []

    if kvk_number and kvk_number.strip():
        candidates.append(PeppolParticipantId(PeppolScheme.KVK.value, kvk_number.strip()))

    parsed = parse_vat_number(vat_number)
    if parsed is not None:
        candidates.append(PeppolParticipantId(PeppolScheme.VAT.value, parsed.value))

    return tuple(candidates)


def build_peppol_directory(provider: str) -> PeppolDirectory:
    """Selects the directory from configuration.

    Only one provider exists today, and the error message says what is missing
    rather than implying the setting is wrong - somebody reaching this in P2 is
    looking for the implementation, not for a typo.
    """
    if provider == "none":
        return UnconfiguredPeppolDirectory()
    raise ValueError(
        f"unknown Peppol directory provider {provider!r}. Only 'none' exists: "
        f"participant discovery against the SMP/SML is P2 (FR-AR-005), and this "
        f"is the seam it plugs into - see api.customers.peppol."
    )

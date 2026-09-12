"""api.customers.peppol - FR-AR-006's discovery field, as a P2 stub.

There is one property here that matters more than everything else in the file:

    NOT_CONFIGURED is never NOT_REGISTERED.

"This deployment has not been configured to look" and "this customer is not on
the Peppol network" are different facts. The first, mistaken for the second,
routes an invoice to email while the customer waits for it on Peppol - and once
ViDA lands (PRD §2.1), while a structured invoice was legally required.

The rest of the file tests `candidate_identifiers`, which is pure and therefore
fully testable now, before any network code exists. That is the point of having
written it: the part of discovery that is a judgment about identifiers is
settled and covered, and the part that is a network call is the only thing P2
has left to add.
"""

from __future__ import annotations

import pytest

from api.customers.peppol import (
    BIS_BILLING_INVOICE,
    DiscoveryStatus,
    PeppolParticipantId,
    PeppolScheme,
    UnconfiguredPeppolDirectory,
    build_peppol_directory,
    candidate_identifiers,
)

pytestmark = pytest.mark.anyio


# --- the stub ---------------------------------------------------------------


async def test_the_stub_says_not_configured_and_never_not_registered() -> None:
    result = await UnconfiguredPeppolDirectory().discover(())

    assert result.status is DiscoveryStatus.NOT_CONFIGURED
    assert result.status is not DiscoveryStatus.NOT_REGISTERED


async def test_the_stub_invents_no_participant_id() -> None:
    """A fabricated identifier would be written to the customer row and then
    addressed by a real send, at which point the failure is somebody else's.
    """
    candidates = candidate_identifiers(kvk_number="12345678", vat_number="NL123456789B01")
    result = await UnconfiguredPeppolDirectory().discover(candidates)

    assert result.participant_id is None


async def test_the_stub_reports_what_it_would_have_looked_up() -> None:
    """ "We looked and found nothing" only means something if you know what was
    looked for. A customer with no KvK and no VAT number cannot be discovered
    at all, which is a data gap rather than an absence from the network.
    """
    candidates = candidate_identifiers(kvk_number="12345678", vat_number="NL123456789B01")
    result = await UnconfiguredPeppolDirectory().discover(candidates)

    assert result.tried == candidates


async def test_not_configured_is_not_deliverable() -> None:
    """`!= NOT_REGISTERED` is the tempting, wrong way to ask this question -
    it would treat NOT_CONFIGURED as deliverable, which is the exact failure
    this module is shaped to prevent.
    """
    result = await UnconfiguredPeppolDirectory().discover(())
    assert not result.status.is_deliverable


def test_only_registered_is_deliverable() -> None:
    deliverable = {status for status in DiscoveryStatus if status.is_deliverable}
    assert deliverable == {DiscoveryStatus.REGISTERED}


def test_a_registered_participant_that_cannot_take_invoices_is_not_deliverable() -> None:
    """A participant registered to receive ORDERS but not invoices is a real
    and common state. Treating "found in the SML" as "can be sent an invoice"
    is the standard way to have a delivery rejected by the receiving access
    point.
    """
    assert not DiscoveryStatus.DOCUMENT_TYPE_UNSUPPORTED.is_deliverable


def test_the_document_type_is_the_nlcius_invoice() -> None:
    """A typo here produces "participant does not support this document type",
    which reads like a fact about the customer. Named once, asserted once.
    """
    assert "Invoice-2::Invoice" in BIS_BILLING_INVOICE
    assert "en16931" in BIS_BILLING_INVOICE
    assert "nlcius" in BIS_BILLING_INVOICE


def test_build_selects_by_provider_and_says_what_is_missing() -> None:
    assert isinstance(build_peppol_directory("none"), UnconfiguredPeppolDirectory)

    with pytest.raises(ValueError, match="P2"):
        build_peppol_directory("smp")


# --- candidate identifiers --------------------------------------------------


def test_kvk_comes_before_vat() -> None:
    """Scheme 0106 is how a Dutch business is ordinarily registered on Peppol.
    A Dutch VAT number identifies a FISCAL entity that may cover several
    registered businesses, so a 9944 match is the weaker evidence of the two.
    """
    candidates = candidate_identifiers(kvk_number="12345678", vat_number="NL123456789B01")

    assert [c.scheme for c in candidates] == [PeppolScheme.KVK.value, PeppolScheme.VAT.value]
    assert candidates[0].value == "0106:12345678"
    assert candidates[1].value == "9944:NL123456789B01"


def test_the_vat_candidate_is_normalised() -> None:
    """An unnormalised number would be a lookup guaranteed to miss, recorded
    as the customer not being on the network.
    """
    candidates = candidate_identifiers(kvk_number=None, vat_number="nl 1234.56.789.b01")

    assert candidates == (PeppolParticipantId("9944", "NL123456789B01"),)


def test_an_unparseable_vat_number_is_not_offered_as_a_candidate() -> None:
    candidates = candidate_identifiers(kvk_number=None, vat_number="NL12345B01")
    assert candidates == ()


def test_a_non_eu_vat_number_is_not_offered_either() -> None:
    """VIES cannot answer about a GB number and Peppol's 9944 scheme is the EU
    VAT scheme. Offering it would be a miss dressed up as a lookup.
    """
    assert candidate_identifiers(kvk_number=None, vat_number="GB123456789") == ()


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_customer_with_neither_identifier_has_no_candidates(blank: str | None) -> None:
    """Not an error, and not "not registered" - there is simply nothing to look
    up, and the caller can tell the difference because `tried` is empty.
    """
    assert candidate_identifiers(kvk_number=blank, vat_number=blank) == ()


def test_a_participant_id_renders_as_scheme_colon_identifier() -> None:
    """Peppol's own addressing uses the joined form, and migration 0039's
    CHECK accepts exactly four digits, a colon, then the identifier.
    """
    participant = PeppolParticipantId("0106", "12345678")

    assert participant.value == "0106:12345678"
    assert str(participant) == "0106:12345678"

"""api.invoicing.epc_qr - SI-02's "pay by bank" QR code.

The payload's TEXT is the part with a specification (EPC069-12); a wrong
field position or a missing structurally-required blank line produces a QR
code that scans successfully into a rejected, or wrong, bank transfer. These
tests pin the payload's exact shape, then confirm the PNG segno produces is
one `api.invoicing.pdf.load_image` can actually place.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.invoicing.epc_qr import EpcQrRequest, build_epc_payload, render_epc_qr
from api.invoicing.pdf import load_image

VALID_IBAN = "NL91ABNA0417164300"


def _request(**overrides: object) -> EpcQrRequest:
    defaults: dict[str, object] = {
        "beneficiary_name": "Van Doorn Bouw B.V.",
        "iban": VALID_IBAN,
        "amount": Decimal("1234.56"),
        "reference": "2026-1",
    }
    defaults.update(overrides)
    return EpcQrRequest(**defaults)  # type: ignore[arg-type]


def test_payload_field_order_matches_epc069_12() -> None:
    """Positional - field N is always line N. Getting this order wrong is
    the exact failure mode this module exists to prevent.
    """
    payload = build_epc_payload(_request())
    lines = payload.split("\n")
    assert lines[0] == "BCD"
    assert lines[1] == "002"
    assert lines[2] == "1"
    assert lines[3] == "SCT"
    assert lines[4] == ""  # BIC omitted
    assert lines[5] == "Van Doorn Bouw B.V."
    assert lines[6] == VALID_IBAN
    assert lines[7] == "EUR1234.56"
    # Purpose, structured remittance are always-empty fields preceding the
    # populated reference - trimmed only from the END, never the middle.
    assert lines[8] == ""
    assert lines[9] == ""
    assert lines[10] == "2026-1"


def test_trailing_empty_fields_are_omitted() -> None:
    """No BIC, no structured/unstructured remittance beyond what's given -
    a smaller, more reliably scannable code, per EPC069-12's own guidance.
    """
    payload = build_epc_payload(_request(reference=""))
    lines = payload.split("\n")
    # Field 12 (beneficiary-to-originator info) and field 11 (reference,
    # empty here) both trail off the end and are dropped.
    assert lines[-1] == "EUR1234.56"
    assert len(lines) == 8


def test_bic_is_upper_cased_and_trimmed() -> None:
    payload = build_epc_payload(_request(bic=" abnanl2a "))
    assert payload.split("\n")[4] == "ABNANL2A"


def test_amount_is_formatted_with_exactly_two_decimals() -> None:
    payload = build_epc_payload(_request(amount=Decimal("100")))
    assert "EUR100.00" in payload.split("\n")


@pytest.mark.parametrize(
    "amount",
    [Decimal("0"), Decimal("-0.01"), Decimal("-100")],
)
def test_non_positive_amount_is_rejected(amount: Decimal) -> None:
    """A credit note (negative/zero amount) never reaches this module in
    practice - `rendering.py` gates on `invoice.is_credit_note` - but the
    module refuses it directly too, since a caller mistake here would
    otherwise produce a QR code requesting a nonsensical transfer.
    """
    with pytest.raises(ValueError, match="positive"):
        build_epc_payload(_request(amount=amount))


def test_invalid_iban_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a valid IBAN"):
        build_epc_payload(_request(iban="NL91ABNA0417164301"))


def test_control_characters_are_stripped_not_injected_as_lines() -> None:
    """A name or reference containing a newline would otherwise inject an
    extra "line" into a payload where line position IS the field - shifting
    every field after it.
    """
    payload = build_epc_payload(_request(beneficiary_name="Van Doorn\nBouw B.V."))
    lines = payload.split("\n")
    assert lines[5] == "Van DoornBouw B.V."
    assert len(lines) == 11  # unchanged - no extra line introduced


def test_beneficiary_name_is_truncated_at_70_characters() -> None:
    payload = build_epc_payload(_request(beneficiary_name="A" * 100))
    assert len(payload.split("\n")[5]) == 70


def test_reference_is_truncated_at_140_characters() -> None:
    payload = build_epc_payload(_request(reference="B" * 200))
    assert len(payload.split("\n")[10]) == 140


def test_render_epc_qr_produces_a_placeable_png() -> None:
    """The exact round-trip `rendering.py` performs: render bytes, then hand
    them to the same `load_image` the PDF writer uses for a logo. If segno's
    PNG output were ever something `load_image` can't parse (interlaced,
    palette-based, etc.), this is where that would be caught.
    """
    png = render_epc_qr(_request())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    image = load_image(png)
    assert image.width > 0
    assert image.height == image.width  # QR codes are square
    assert image.colour_space == b"/DeviceGray"


def test_render_epc_qr_raises_for_an_invalid_request() -> None:
    with pytest.raises(ValueError):
        render_epc_qr(_request(iban="not an iban"))

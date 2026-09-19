"""SI-02: the EPC069-12 "pay by bank" QR code on an invoice PDF.

The European Payments Council's "Quick Response Code Guidelines to Enable
Data Capture for the Initiation of a SEPA Credit Transfer" (EPC069-12,
GUF version 002) defines a plain-text, newline-separated payload that every
mainstream Dutch (and wider SEPA) banking app - ING, Rabobank, ABN AMRO,
Bunq - reads natively to pre-fill a bank transfer: no PSP, no per-transaction
fee, no account this deployment has to hold money in. A customer scans the
code on the PDF with their own banking app and the amount, IBAN and
reference are already filled in when the transfer screen opens.

--- The payload, not the image, is the part with a specification ---

The twelve-line text format below is EPC069-12 verbatim; `segno` (a pure
Python QR encoder, no image-library dependency - see pyproject.toml's
comment on why that matters here) turns that text into pixels. Getting the
TEXT wrong (a field in the wrong position, a missing blank line where one is
structurally required) produces a QR code that scans successfully and pays
the wrong account, or nothing - so this module is mostly about the payload's
exact shape, tested against the spec's own field order.

--- Why no PSP/payment link is bundled with this ---

That is FR-AR-009 (payment links via a PSP - iDEAL, card, SEPA DD), a
different, larger feature with its own account, fees and reconciliation.
This QR carries none of that: it is a SEPA credit transfer INITIATION
request the customer's own bank executes, LEDGR never touches the money or
learns the transfer happened, and there is no "did they pay" signal here -
FR-AR-009's auto-matching is what answers that question, separately.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal

import segno

from api.iban import parse as parse_iban

__all__ = ["EpcQrRequest", "build_epc_payload", "render_epc_qr"]

#: EPC069-12 §"Beneficiary Name": max 70 characters.
_MAX_NAME_LENGTH = 70
#: EPC069-12 §"Remittance Information (unstructured)": max 140 characters.
_MAX_REMITTANCE_LENGTH = 140

#: Control characters would inject extra "lines" into a format that is
#: POSITIONAL (field N is always the Nth line) - stripped rather than
#: rejected, since a name or reference containing one is a data-entry
#: accident, not a reason to withhold the QR code entirely.
_CONTROL_CHARS = str.maketrans("", "", "\r\n\t\x00")


@dataclass(frozen=True, slots=True)
class EpcQrRequest:
    """Everything the payload needs. `amount` and `reference` are per
    INVOICE - the QR is never reused across documents - while `beneficiary`
    and `iban` are the supplier's own, read once per render.
    """

    beneficiary_name: str
    iban: str
    #: The invoice's gross amount. Must be positive - see
    #: `build_epc_payload`'s docstring on why a credit note never reaches
    #: this module at all.
    amount: Decimal
    #: The invoice reference ("2026-1"), carried as UNSTRUCTURED remittance
    #: information - the field a Dutch "betalingskenmerk" ordinarily goes
    #: in. STRUCTURED remittance (ISO 11649) is left empty: this deployment
    #: has no structured-reference scheme to populate it with, and the two
    #: are mutually exclusive per EPC069-12.
    reference: str
    bic: str | None = None


def _clean(value: str, *, max_length: int) -> str:
    return value.translate(_CONTROL_CHARS).strip()[:max_length]


def build_epc_payload(request: EpcQrRequest) -> str:
    """The exact twelve-field EPC069-12 (GUF version 002) text block.

    Raises `ValueError` for anything that would produce a payload no banking
    app could read - an invalid IBAN, or an amount that is not strictly
    positive. Both are checked HERE, not left to the caller, because a
    caller that got either wrong would otherwise ship a QR code that scans
    into a rejected or nonsensical transfer.
    """
    iban = parse_iban(request.iban)
    if iban is None:
        raise ValueError(f"{request.iban!r} is not a valid IBAN")
    if request.amount <= 0:
        raise ValueError("an EPC QR amount must be strictly positive")

    fields = [
        "BCD",  # Service Tag
        "002",  # Version (GUF002 - BIC optional for SEPA since Feb 2016)
        "1",  # Character set: 1 = UTF-8
        "SCT",  # Identification: SEPA Credit Transfer
        (request.bic or "").strip().upper(),
        _clean(request.beneficiary_name, max_length=_MAX_NAME_LENGTH),
        iban.value,
        f"EUR{request.amount:.2f}",
        "",  # Purpose (optional, unused)
        "",  # Remittance Information (structured) - mutually exclusive with next
        _clean(request.reference, max_length=_MAX_REMITTANCE_LENGTH),
        "",  # Beneficiary to originator information (optional, unused)
    ]

    # Trailing empty fields may be omitted entirely (EPC069-12 §"General
    # rules") - fewer bytes is a smaller, more reliably scannable QR code at
    # invoice print size, and every field this deployment ever populates
    # comes before the fields that are always empty above.
    while fields and fields[-1] == "":
        fields.pop()

    return "\n".join(fields)


def render_epc_qr(request: EpcQrRequest) -> bytes:
    """The payload, as a PNG - 1-bit `/DeviceGray`, confirmed to round-trip
    through `api.invoicing.pdf.load_image` unchanged (no re-encoding either
    side). `error='m'` is EPC069-12's own recommendation (~15% recovery),
    the standard choice for a code that will be printed and scanned from a
    screen or paper rather than photographed at a distance.
    """
    qr = segno.make(build_epc_payload(request), error="m")
    buffer = io.BytesIO()
    # scale=4: comfortably above the ~2px-per-module floor most phone
    # cameras need to focus reliably at typical invoice print/view sizes.
    # border=2: EPC069-12's own "quiet zone" recommendation is 4 modules,
    # but this deployment additionally has GENUINE white space (`_MARGIN`)
    # around wherever the image is placed on the page - 2 is enough not to
    # rely on that placement alone while not wasting page space duplicating it.
    qr.save(buffer, kind="png", scale=4, border=2, dark="#000000", light="#ffffff")
    return buffer.getvalue()

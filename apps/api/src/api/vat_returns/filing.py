"""How a prepared BTW return reaches the Belastingdienst: FR-VAT-003/004 (CLAUDE.md #4).

    FR-VAT-003  Electronic filing to the Belastingdienst via SBR/Digipoort using the current
                Nederlandse Taxonomie, with receipt confirmation stored as evidence.

Behind a `Protocol`, the shape every integration in this codebase takes (bank feed, Peppol,
extraction): the return service hands a channel the figures and gets back a reference, and does
not know which channel it was.

--- What exists ---

`ManualFiling`: the person filing enters the figures LEDGR shows in Mijn Belastingdienst Zakelijk
(or their accountant's software), and records the reference the Belastingdienst gave back. LEDGR
then stores the return exactly as filed and hard-locks the period. This is a complete, honest
path - the figures, the evidence and the lock are all real - with one step done by a person.

--- What does not, and why ---

Digipoort needs an XBRL instance against the current NT OB taxonomy, signed and sent over a
connection authenticated with a PKIoverheid services server certificate issued to the filing
party. The certificate is the blocker: only the business (or LEDGR as an intermediary, once it
holds one) can obtain it, and an unsigned or unverified submission to a tax authority is not
something to ship on a guess. `DigipoortFiling` implements this Protocol when that exists; the
return service, the table and the screen need no change. ADR-087 records it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from api.vat_returns.model import VatReturn


class FilingRefused(Exception):
    """The channel will not file this return. Carries an i18n reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FilingReceipt:
    channel: str
    reference: str | None


class VatFilingChannel(Protocol):
    @property
    def channel(self) -> str: ...

    async def submit(self, vat_return: VatReturn, *, reference: str | None) -> FilingReceipt: ...


class ManualFiling:
    """The filer submitted it themselves; record what they were given back."""

    channel = "manual"

    async def submit(self, vat_return: VatReturn, *, reference: str | None) -> FilingReceipt:
        del vat_return
        cleaned = reference.strip() if reference else None
        if cleaned is not None and len(cleaned) > 100:
            raise FilingRefused("vat_filing_reference_invalid")
        return FilingReceipt(channel=self.channel, reference=cleaned or None)


def build_filing_channel(provider: str) -> VatFilingChannel:
    if provider == "manual":
        return ManualFiling()
    raise ValueError(f"unknown VAT filing provider: {provider!r}")

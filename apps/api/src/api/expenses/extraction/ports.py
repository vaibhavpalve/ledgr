"""The seam between capture and whatever reads an invoice - non-negotiable 4.

Integrations sit behind adapters so a provider can be replaced without touching
domain logic. Capture and the expense form know `InvoiceExtractor` and nothing
about Vertex, Claude or any other service behind it.
"""

from __future__ import annotations

from typing import Protocol

from api.expenses.extraction.model import ExtractedInvoice

#: What providers can be handed. HEIC is not among them: no provider here reads
#: it, and pretending to would turn a photo into a "failed" reading instead of an
#: honest "not read".
READABLE_CONTENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})


class InvoiceExtractor(Protocol):
    #: Recorded on the expense beside a reading, so a wrong one can be traced to
    #: what produced it.
    provider: str
    model: str

    async def extract(self, *, data: bytes, content_type: str) -> ExtractedInvoice:
        """Reads one invoice. Raises `ExtractionError` when it cannot; anything
        else it raises is treated the same way by the service."""
        ...

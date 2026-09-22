"""The invoice reader named in settings - and none when it is switched off.

Same shape as `api.documents.scanning.build_scanner` and `api.mail.sender.
build_email_sender`: a provider name in, an adapter out. Unlike the scanner
there IS an "off" (`none`): reading is optional by FR-EXP-001c, and its default
is off because switching it on sends invoices to a model (PRIV-010/011,
ADR-081).
"""

from __future__ import annotations

from api.config import Settings
from api.expenses.extraction.google_auth import ServiceAccountToken
from api.expenses.extraction.ports import InvoiceExtractor
from api.expenses.extraction.vertex import VertexClaudeExtractor


class ExtractionNotConfigured(RuntimeError):
    """`vertex-claude` was selected without what it needs to run."""


def build_extractor(settings: Settings) -> InvoiceExtractor | None:
    if settings.extraction_provider == "none":
        return None
    if not settings.extraction_gcp_project:
        # Fail at first use with a plain sentence, not on the first invoice with
        # a provider error: a project id is the one thing that is certainly
        # missing rather than merely unavailable.
        raise ExtractionNotConfigured(
            "EXTRACTION_PROVIDER=vertex-claude needs EXTRACTION_GCP_PROJECT (ADR-081)"
        )
    return VertexClaudeExtractor(
        project=settings.extraction_gcp_project,
        region=settings.extraction_gcp_region,
        model=settings.extraction_model,
        tokens=ServiceAccountToken(),
        timeout_seconds=settings.extraction_timeout_seconds,
    )

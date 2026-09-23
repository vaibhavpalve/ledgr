"""Where a live bank feed (PSD2/AISP) would plug in - not built.

The same shape `api.customers.peppol.UnconfiguredPeppolDirectory` and
ADR-081's `InvoiceExtractor` take: a `Protocol` for the real thing, and one
implementation that plainly says nobody has looked, rather than a route that
doesn't exist and a screen that pretends the capability might.

`api.bank.csv_parser` is the P0 substitute this module is not: statement
import works today, for every bank, at the cost of a manual export step. A
future PSD2/AISP integration (Tink, Enable Banking, Nordigen/GoCardless Bank
Account Data, or similar - see ADR-084's "not yet built" section) would
implement `BankFeedProvider` and be selected by configuration exactly the way
`EXTRACTION_PROVIDER` selects an `InvoiceExtractor` - nothing about the
`bank_transaction` table, the reconciliation service, or this screen would
need to change; a live feed would simply insert rows the same import path
already accepts.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from api.bank.csv_parser import StatementRow


class BankFeedProvider(Protocol):
    """What a live feed would hand back: the same shape a CSV import
    produces, so the reconciliation service does not need to know which
    source a transaction came from.
    """

    async def fetch_transactions(self, *, iban: str) -> Sequence[StatementRow]: ...

    @property
    def is_configured(self) -> bool: ...


class UnconfiguredBankFeed:
    """The only implementation today. Every call says plainly that nobody
    has connected a live feed - never that the account has no transactions.
    """

    is_configured = False

    async def fetch_transactions(self, *, iban: str) -> Sequence[StatementRow]:
        del iban
        return ()


def build_bank_feed_provider(provider: str) -> BankFeedProvider:
    if provider == "none":
        return UnconfiguredBankFeed()
    raise ValueError(f"unknown bank feed provider: {provider!r}")

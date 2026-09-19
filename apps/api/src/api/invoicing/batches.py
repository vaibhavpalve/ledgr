"""Batch invoicing rules - SI-17 (ADR-079).

    Generate invoices for multiple customers/contracts in one pass.

Pure: no I/O, no clock. A batch is a list of ENTRIES, one per invoice to raise. Each names a
customer and, optionally, its own lines, notes and payment term; whatever an entry leaves out
comes from the batch's defaults, so "the same annual fee to 80 customers" is one set of lines
and 80 customer ids, while "each customer's own consumption" is 80 entries with lines.

Line rules are the quote's (`api.invoicing.quotes.validate_quote`): the same shape, the same
Decimal-only, the same limits - one definition of "a usable invoice line" rather than two.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from api.invoicing.quotes import QuoteInvalid, QuoteLine, validate_quote

__all__ = [
    "MAX_ENTRIES",
    "MAX_DUE_DAYS",
    "BatchEntry",
    "BatchInvalid",
    "ResolvedEntry",
    "resolve_entries",
]

#: A pass is meant to be looked at. Beyond this, split it: the response lists every entry.
MAX_ENTRIES = 200
MAX_DUE_DAYS = 365


class BatchInvalid(ValueError):
    """A batch the rules refuse. `code` names why; `position` is the 1-based entry, if one."""

    def __init__(self, code: str, position: int | None = None, message: str | None = None) -> None:
        self.code = code
        self.position = position
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class BatchEntry:
    """One invoice to raise. `lines` None means "the batch's default lines"."""

    customer_id: uuid.UUID
    lines: tuple[QuoteLine, ...] | None = None
    notes: str | None = None
    due_days: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedEntry:
    position: int
    customer_id: uuid.UUID
    lines: tuple[QuoteLine, ...]
    notes: str | None
    due_days: int | None


def resolve_entries(
    *,
    name: str,
    entries: Sequence[BatchEntry],
    default_lines: Sequence[QuoteLine] | None,
    default_notes: str | None,
    default_due_days: int | None,
    today: date,
) -> tuple[ResolvedEntry, ...]:
    """Fill each entry's gaps from the defaults and validate the result, or raise
    `BatchInvalid` naming the first entry that cannot be used."""
    if not (name or "").strip():
        raise BatchInvalid("name_required")
    if not entries:
        raise BatchInvalid("empty")
    if len(entries) > MAX_ENTRIES:
        raise BatchInvalid("too_many", message=f"at most {MAX_ENTRIES} entries")

    resolved: list[ResolvedEntry] = []
    for position, entry in enumerate(entries, start=1):
        lines = entry.lines if entry.lines is not None else tuple(default_lines or ())
        try:
            validate_quote(lines=lines, valid_until=None, today=today)
        except QuoteInvalid as exc:
            raise BatchInvalid("lines", position, str(exc)) from exc

        due_days = entry.due_days if entry.due_days is not None else default_due_days
        if due_days is not None and not 0 <= due_days <= MAX_DUE_DAYS:
            raise BatchInvalid("lines", position, f"a payment term is 0-{MAX_DUE_DAYS} days")

        notes = entry.notes if entry.notes is not None else default_notes
        resolved.append(
            ResolvedEntry(
                position=position,
                customer_id=entry.customer_id,
                lines=tuple(lines),
                notes=(notes or "").strip() or None,
                due_days=due_days,
            )
        )
    return tuple(resolved)

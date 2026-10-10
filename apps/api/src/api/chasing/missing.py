"""Which bank lines are "missing a receipt" - wave-1 contract decision 5, in one place (ADR-114).

The buckets are mutually exclusive for an unmatched bank line: a pending HIGH proposal makes it an
auto-booking; otherwise money OUT with no candidate receipt is a missing receipt; anything else is
bank-to-match. "No candidate receipt" means no captured, not-discarded expense of exactly that
amount that no other line has already settled.

The two predicates below are the definition. The firm worklist
(`api.firm.worklist_repository._BANK_BUCKETS`) imports them rather than keeping a copy, so the
client's list, the chase e-mail's count and the accountant's worklist column cannot drift apart.

Every statement here reads `bank_transaction t` and runs under whichever session it is given: a
tenant session (RLS) for the HTTP routes, the client's own tenant session for the sweep.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The line has a pending HIGH-confidence booking proposal (it is an auto-booking, not missing).
PENDING_HIGH_PROPOSAL = """EXISTS (
                     SELECT 1 FROM booking_proposal p
                      WHERE p.bank_transaction_id = t.id
                        AND p.status = 'pending' AND p.confidence = 'high'
                 )"""

#: Money out, and no receipt of exactly that amount exists that no other line has settled.
#: Discarded captures are not receipts.
NO_CANDIDATE_RECEIPT = """(t.amount < 0 AND NOT EXISTS (
                     SELECT 1 FROM expense e
                       JOIN capture_item ci ON ci.id = e.capture_item_id
                      WHERE e.administration_id = t.administration_id
                        AND e.gross_amount = -t.amount
                        AND ci.discarded_at IS NULL
                        AND NOT EXISTS (SELECT 1 FROM bank_transaction b
                                         WHERE b.matched_expense_id = e.id)
                 ))"""

#: The whole bucket test for one line.
MISSING_RECEIPT = (
    f"t.status = 'unmatched' AND NOT {PENDING_HIGH_PROPOSAL} AND {NO_CANDIDATE_RECEIPT}"
)

_COUNTS = f"""
    SELECT t.administration_id, count(*) AS n
      FROM bank_transaction t
     WHERE t.administration_id = ANY(CAST(:ids AS uuid[]))
       AND {MISSING_RECEIPT}
     GROUP BY t.administration_id
"""

_LINES = f"""
    SELECT t.id, t.booking_date, t.amount, t.counterparty_name, t.description
      FROM bank_transaction t
     WHERE t.administration_id = :admin
       AND {MISSING_RECEIPT}
     ORDER BY t.booking_date DESC, t.id
     LIMIT :limit
"""

_COUNT_ONE = f"""
    SELECT count(*) FROM bank_transaction t
     WHERE t.administration_id = :admin AND {MISSING_RECEIPT}
"""

#: The client's list is a to-do list, not an archive: beyond this the screen says "and more".
MAX_LINES = 500


@dataclass(frozen=True, slots=True)
class MissingLine:
    bank_transaction_id: uuid.UUID
    booking_date: date
    amount: Decimal
    counterparty: str | None
    description: str | None


async def missing_counts(
    session: AsyncSession, administration_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Missing-receipt counts for many administrations in one query; absent means zero."""
    if not administration_ids:
        return {}
    result = await session.execute(text(_COUNTS), {"ids": [str(a) for a in administration_ids]})
    return {row.administration_id: int(row.n) for row in result}


async def missing_count(session: AsyncSession, administration_id: uuid.UUID) -> int:
    result = await session.execute(text(_COUNT_ONE), {"admin": str(administration_id)})
    return int(result.scalar_one())


async def missing_lines(
    session: AsyncSession, administration_id: uuid.UUID, *, limit: int = MAX_LINES
) -> list[MissingLine]:
    result = await session.execute(text(_LINES), {"admin": str(administration_id), "limit": limit})
    return [
        MissingLine(
            bank_transaction_id=row.id,
            booking_date=row.booking_date,
            amount=Decimal(row.amount),
            counterparty=row.counterparty_name,
            description=row.description,
        )
        for row in result
    ]

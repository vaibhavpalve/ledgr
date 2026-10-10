"""SQL for booking proposals (migration 0079, ADR-110).

Writes touch `booking_proposal` only - never a posting table. Every query runs in the caller's
tenant session, so RLS (`app.has_administration_access`) is the first line: a firm session sees the
administrations it has an active engagement on, a business session its own. The listing reads
only the administrations it is handed - the caller's authorized portfolio
(api.firm.worklist_access.require_portfolio_permission) or one administration a route already
authorized - and never decides which those are itself (ADR-109).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.bank.model import CandidateKind
from api.firm.proposals import (
    NewProposal,
    PendingProposal,
    ProposalStatus,
    TargetAccount,
)


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """One proposal, as deciding it needs to know it."""

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    bank_transaction_id: uuid.UUID | None
    document_id: uuid.UUID | None
    document_kind: CandidateKind | None
    status: ProposalStatus
    amount: Decimal
    #: What a rule made from this approval is keyed by (ADR-113).
    counterparty: str | None = None
    account_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalRow:
    """One pending proposal, as the review sheet shows it."""

    id: uuid.UUID
    administration_id: uuid.UUID
    display_name: str
    group_key: str
    counterparty: str | None
    account_code: str | None
    account_name: str | None
    amount: Decimal
    date: date
    description: str | None


_RECORD_COLUMNS = (
    "id, organization_id, administration_id, bank_transaction_id, document_id, document_kind, "
    "status, amount, counterparty, account_code"
)


def _record(row: Any) -> ProposalRecord:
    return ProposalRecord(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        bank_transaction_id=row.bank_transaction_id,
        document_id=row.document_id,
        document_kind=CandidateKind(row.document_kind) if row.document_kind else None,
        status=ProposalStatus(row.status),
        amount=Decimal(row.amount),
        counterparty=row.counterparty,
        account_code=row.account_code,
    )


class SqlProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def isolated(self) -> AsyncIterator[object]:
        """A savepoint: a failure inside rolls back to here and leaves the rest of the request's
        transaction - the import, the other decisions - intact."""
        async with self._session.begin_nested() as savepoint:
            yield savepoint

    # -- generation -------------------------------------------------------------

    async def pending_for(self, *, administration_id: uuid.UUID) -> Sequence[PendingProposal]:
        result = await self._session.execute(
            text(
                "SELECT id, bank_transaction_id, document_id FROM booking_proposal "
                "WHERE administration_id = :admin AND status = 'pending' "
                "ORDER BY created_at, id"
            ),
            {"admin": str(administration_id)},
        )
        return [
            PendingProposal(
                id=row.id,
                bank_transaction_id=row.bank_transaction_id,
                document_id=row.document_id,
            )
            for row in result
        ]

    async def target_accounts(
        self, *, administration_id: uuid.UUID, documents: Sequence[tuple[CandidateKind, uuid.UUID]]
    ) -> dict[uuid.UUID, TargetAccount]:
        """The account each document books into, read from its own posted entry: a receipt's
        largest expense debit (its cost account - "4500 Telefoon"), an invoice's debit (the
        receivable). A document without a posted entry has none."""
        expenses = [str(d) for kind, d in documents if kind is CandidateKind.EXPENSE]
        invoices = [str(d) for kind, d in documents if kind is CandidateKind.SALES_INVOICE]
        if not expenses and not invoices:
            return {}
        result = await self._session.execute(
            text(
                """
                SELECT d.id AS document_id, target.code, target.name
                  FROM (
                        SELECT e.id, e.journal_entry_id FROM expense e
                         WHERE e.administration_id = :admin
                           AND e.id = ANY(cast(:expenses as uuid[]))
                        UNION ALL
                        SELECT s.id, s.journal_entry_id FROM sales_invoice s
                         WHERE s.administration_id = :admin
                           AND s.id = ANY(cast(:invoices as uuid[]))
                       ) d
                  CROSS JOIN LATERAL (
                        SELECT la.code, la.name
                          FROM journal_line jl
                          JOIN ledger_account la ON la.id = jl.account_id
                         WHERE jl.journal_entry_id = d.journal_entry_id AND jl.debit > 0
                         ORDER BY (la.account_type = 'expense') DESC, jl.debit DESC,
                                  jl.line_number
                         LIMIT 1
                       ) target
                """
            ),
            {"admin": str(administration_id), "expenses": expenses, "invoices": invoices},
        )
        return {row.document_id: TargetAccount(code=row.code, name=row.name) for row in result}

    async def insert(self, *, administration_id: uuid.UUID, proposal: NewProposal) -> bool:
        """Writes one pending proposal; False when the line already has one (0079's partial
        unique index - a concurrent generation got there first). The organization comes from the
        bank line itself, never from the caller: in a firm session the caller's organization is
        the firm's, and the proposal belongs to the client's."""
        result = await self._session.execute(
            text(
                """
                INSERT INTO booking_proposal (
                    organization_id, administration_id, bank_transaction_id, document_id,
                    document_kind, proposal_kind, confidence, group_key, counterparty,
                    account_code, amount
                )
                SELECT t.organization_id, t.administration_id, t.id, :document, :document_kind,
                       'bank_match', :confidence, :group_key, :counterparty, :account_code,
                       :amount
                  FROM bank_transaction t
                 WHERE t.id = :transaction AND t.administration_id = :admin
                   AND t.status = 'unmatched'
                ON CONFLICT (bank_transaction_id) WHERE status = 'pending' DO NOTHING
                RETURNING id
                """
            ),
            {
                "admin": str(administration_id),
                "transaction": str(proposal.bank_transaction_id),
                "document": str(proposal.document_id),
                "document_kind": proposal.document_kind.value,
                "confidence": proposal.confidence.value,
                "group_key": proposal.group_key,
                "counterparty": proposal.counterparty,
                "account_code": proposal.account_code,
                "amount": proposal.amount,
            },
        )
        return result.first() is not None

    async def supersede(self, *, administration_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> int:
        if not ids:
            return 0
        result = await self._session.execute(
            text(
                "UPDATE booking_proposal SET status = 'superseded', decided_at = now() "
                "WHERE administration_id = :admin AND status = 'pending' "
                "  AND id = ANY(cast(:ids as uuid[])) RETURNING id"
            ),
            {"admin": str(administration_id), "ids": [str(i) for i in ids]},
        )
        return len(result.all())

    async def supersede_settled(
        self, *, administration_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> int:
        """The line was reconciled: withdraw its own pending proposal, and every pending proposal
        for a document the line settled (another line proposed for the same receipt or invoice)."""
        result = await self._session.execute(
            text(
                """
                UPDATE booking_proposal p SET status = 'superseded', decided_at = now()
                 WHERE p.administration_id = :admin AND p.status = 'pending'
                   AND (
                        p.bank_transaction_id = :transaction
                        OR p.document_id IN (
                            SELECT t.matched_expense_id FROM bank_transaction t
                             WHERE t.id = :transaction AND t.matched_expense_id IS NOT NULL
                            UNION
                            SELECT t.matched_sales_invoice_id FROM bank_transaction t
                             WHERE t.id = :transaction AND t.matched_sales_invoice_id IS NOT NULL
                            UNION
                            SELECT a.sales_invoice_id FROM bank_transaction_allocation a
                             WHERE a.bank_transaction_id = :transaction
                        )
                   )
                RETURNING p.id
                """
            ),
            {"admin": str(administration_id), "transaction": str(transaction_id)},
        )
        return len(result.all())

    # -- deciding -----------------------------------------------------------------

    async def get(self, *, proposal_id: uuid.UUID) -> ProposalRecord | None:
        """RLS-scoped: another tenant's proposal is simply not found."""
        result = await self._session.execute(
            text(f"SELECT {_RECORD_COLUMNS} FROM booking_proposal WHERE id = :id"),
            {"id": str(proposal_id)},
        )
        row = result.first()
        return None if row is None else _record(row)

    async def claim(
        self, *, proposal_id: uuid.UUID, status: ProposalStatus, user_id: uuid.UUID
    ) -> bool:
        """Moves a pending proposal to its decision. The row lock this UPDATE takes is held until
        the request commits, so of two concurrent decisions on one proposal exactly one finds it
        pending; the other waits, then finds nothing to claim (NFR-032)."""
        result = await self._session.execute(
            text(
                "UPDATE booking_proposal SET status = :status, decided_at = now(), "
                "  decided_by_user_id = :user "
                "WHERE id = :id AND status = 'pending' RETURNING id"
            ),
            {"id": str(proposal_id), "status": status.value, "user": str(user_id)},
        )
        return result.first() is not None

    # -- the review sheet ---------------------------------------------------------

    async def pending_rows(
        self,
        *,
        administration_ids: Sequence[uuid.UUID],
    ) -> Sequence[ProposalRow]:
        """Pending proposals on still-unmatched lines, in exactly `administration_ids` - which the
        caller has already authorized. An empty list reads nothing."""
        if not administration_ids:
            return []
        result = await self._session.execute(
            text(
                """
                SELECT p.id, p.administration_id,
                       coalesce(a.trade_name, a.legal_name) AS display_name,
                       p.group_key, p.counterparty, p.account_code,
                       (SELECT la.name FROM ledger_account la
                         WHERE la.administration_id = p.administration_id
                           AND la.code = p.account_code
                         LIMIT 1) AS account_name,
                       p.amount, t.booking_date, t.description
                  FROM booking_proposal p
                  JOIN administration a   ON a.id = p.administration_id
                  JOIN bank_transaction t ON t.id = p.bank_transaction_id
                 WHERE p.status = 'pending'
                   AND t.status = 'unmatched'
                   AND p.administration_id = ANY(cast(:admins as uuid[]))
                 ORDER BY p.group_key, t.booking_date, p.id
                """
            ),
            {"admins": [str(a) for a in administration_ids]},
        )
        return [
            ProposalRow(
                id=row.id,
                administration_id=row.administration_id,
                display_name=row.display_name,
                group_key=row.group_key,
                counterparty=row.counterparty,
                account_code=row.account_code,
                account_name=row.account_name,
                amount=Decimal(row.amount),
                date=row.booking_date,
                description=row.description,
            )
            for row in result
        ]

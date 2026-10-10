"""SQL for approval rules (migration 0082, ADR-113).

Writes touch `booking_rule` and the decision columns of `booking_proposal` - never a posting table;
a rule's booking goes through api.bank.service like a person's approval. Every query runs in the
caller's tenant session, so RLS (`app.has_administration_access`) is the first line, and every
query also names the administration it is for: a rule of client A is never read, applied or
changed while working on client B (FR-BNK-006), whichever firm reaches both.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.bank.model import CandidateKind
from api.firm.rules import Rule, RuleCandidate, RuleStatus

_RULE_COLUMNS = (
    "id, organization_id, administration_id, counterparty_key, account_code, max_amount, "
    "status, created_by_user_id"
)


def _rule(row: Any) -> Rule:
    return Rule(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        counterparty_key=row.counterparty_key,
        account_code=row.account_code,
        max_amount=None if row.max_amount is None else Decimal(row.max_amount),
        status=RuleStatus(row.status),
        created_by_user_id=row.created_by_user_id,
    )


@dataclass(frozen=True, slots=True)
class RuleView:
    """One rule as the client's rules screen shows it."""

    id: uuid.UUID
    counterparty_key: str
    counterparty_label: str | None
    account_code: str
    account_name: str | None
    max_amount: Decimal | None
    status: RuleStatus
    suspended_reason: str | None
    created_by_name: str | None
    created_at: datetime
    postings_count: int
    last_posted_at: datetime | None


@dataclass(frozen=True, slots=True)
class RulePosting:
    """One booking a rule made: an approved proposal naming the rule."""

    proposal_id: uuid.UUID
    rule_id: uuid.UUID
    date: date
    amount: Decimal
    counterparty: str | None
    account_code: str | None
    posted_at: datetime


_VIEW_SQL = """
    SELECT r.id, r.counterparty_key, r.account_code, r.max_amount, r.status,
           r.suspended_reason, r.created_at,
           u.email AS created_by_name,
           (SELECT la.name FROM ledger_account la
             WHERE la.administration_id = r.administration_id AND la.code = r.account_code
             LIMIT 1) AS account_name,
           (SELECT p.counterparty FROM booking_proposal p
             WHERE p.administration_id = r.administration_id
               AND p.group_key = r.counterparty_key || '|' || r.account_code
               AND p.counterparty IS NOT NULL
             ORDER BY p.created_at DESC, p.id
             LIMIT 1) AS counterparty_label,
           (SELECT count(*) FROM booking_proposal p
             WHERE p.rule_id = r.id AND p.status = 'approved') AS postings_count,
           (SELECT max(p.decided_at) FROM booking_proposal p
             WHERE p.rule_id = r.id AND p.status = 'approved') AS last_posted_at
      FROM booking_rule r
      LEFT JOIN users u ON u.id = r.created_by_user_id
     WHERE r.administration_id = :admin
"""


def _view(row: Any) -> RuleView:
    return RuleView(
        id=row.id,
        counterparty_key=row.counterparty_key,
        counterparty_label=row.counterparty_label,
        account_code=row.account_code,
        account_name=row.account_name,
        max_amount=None if row.max_amount is None else Decimal(row.max_amount),
        status=RuleStatus(row.status),
        suspended_reason=row.suspended_reason,
        created_by_name=None if row.created_by_name is None else str(row.created_by_name),
        created_at=row.created_at,
        postings_count=int(row.postings_count),
        last_posted_at=row.last_posted_at,
    )


class SqlRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def isolated(self) -> AsyncIterator[object]:
        """A savepoint: a failure inside rolls back to here and leaves the request's transaction
        - the approval, the import - intact."""
        async with self._session.begin_nested() as savepoint:
            yield savepoint

    # -- the rule itself ------------------------------------------------------------

    async def live_rule(
        self, *, administration_id: uuid.UUID, counterparty_key: str, account_code: str
    ) -> Rule | None:
        result = await self._session.execute(
            text(
                f"SELECT {_RULE_COLUMNS} FROM booking_rule "
                "WHERE administration_id = :admin AND counterparty_key = :key "
                "  AND account_code = :code AND status <> 'retired' "
                "FOR UPDATE"
            ),
            {"admin": str(administration_id), "key": counterparty_key, "code": account_code},
        )
        row = result.first()
        return None if row is None else _rule(row)

    async def get(self, *, administration_id: uuid.UUID, rule_id: uuid.UUID) -> Rule | None:
        result = await self._session.execute(
            text(
                f"SELECT {_RULE_COLUMNS} FROM booking_rule "
                "WHERE administration_id = :admin AND id = :id"
            ),
            {"admin": str(administration_id), "id": str(rule_id)},
        )
        row = result.first()
        return None if row is None else _rule(row)

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        counterparty_key: str,
        account_code: str,
        user_id: uuid.UUID,
    ) -> Rule:
        """`organization_id` is the administration's OWNER (0082's trigger checks it), never the
        caller's: in a firm session the caller's organization is the firm's."""
        result = await self._session.execute(
            text(
                "INSERT INTO booking_rule (organization_id, administration_id, counterparty_key, "
                "  account_code, created_by_user_id) "
                "VALUES (:org, :admin, :key, :code, :user) "
                f"RETURNING {_RULE_COLUMNS}"
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "key": counterparty_key,
                "code": account_code,
                "user": str(user_id),
            },
        )
        return _rule(result.one())

    async def reactivate(self, *, rule_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE booking_rule SET status = 'active', suspended_reason = NULL "
                "WHERE id = :id AND status = 'suspended' RETURNING id"
            ),
            {"id": str(rule_id)},
        )
        return result.first() is not None

    async def suspend(self, *, rule_id: uuid.UUID, reason: str) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE booking_rule SET status = 'suspended', suspended_reason = :reason "
                "WHERE id = :id AND status = 'active' RETURNING id"
            ),
            {"id": str(rule_id), "reason": reason},
        )
        return result.first() is not None

    async def retire(self, *, rule_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE booking_rule SET status = 'retired', suspended_reason = NULL, "
                "  retired_by_user_id = :user, retired_at = now() "
                "WHERE id = :id AND status <> 'retired' RETURNING id"
            ),
            {"id": str(rule_id), "user": str(user_id)},
        )
        return result.first() is not None

    async def set_max_amount(
        self, *, administration_id: uuid.UUID, rule_id: uuid.UUID, max_amount: Decimal | None
    ) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE booking_rule SET max_amount = :amount "
                "WHERE administration_id = :admin AND id = :id AND status <> 'retired' "
                "RETURNING id"
            ),
            {"admin": str(administration_id), "id": str(rule_id), "amount": max_amount},
        )
        return result.first() is not None

    # -- applying -------------------------------------------------------------------

    async def active_rules(self, *, administration_id: uuid.UUID) -> Sequence[Rule]:
        result = await self._session.execute(
            text(
                f"SELECT {_RULE_COLUMNS} FROM booking_rule "
                "WHERE administration_id = :admin AND status = 'active' "
                "ORDER BY created_at, id"
            ),
            {"admin": str(administration_id)},
        )
        return [_rule(row) for row in result]

    async def pending_candidates(
        self, *, administration_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
    ) -> Sequence[RuleCandidate]:
        """Pending proposals on still-unmatched lines among `transaction_ids`, in this
        administration only."""
        if not transaction_ids:
            return []
        result = await self._session.execute(
            text(
                """
                SELECT p.id, p.organization_id, p.administration_id, p.bank_transaction_id,
                       p.document_id, p.document_kind, p.confidence, p.counterparty,
                       p.account_code, p.amount
                  FROM booking_proposal p
                  JOIN bank_transaction t ON t.id = p.bank_transaction_id
                 WHERE p.administration_id = :admin
                   AND p.status = 'pending'
                   AND t.status = 'unmatched'
                   AND p.bank_transaction_id = ANY(cast(:lines as uuid[]))
                 ORDER BY t.booking_date, p.id
                """
            ),
            {"admin": str(administration_id), "lines": [str(t) for t in transaction_ids]},
        )
        return [
            RuleCandidate(
                proposal_id=row.id,
                organization_id=row.organization_id,
                administration_id=row.administration_id,
                bank_transaction_id=row.bank_transaction_id,
                document_id=row.document_id,
                document_kind=CandidateKind(row.document_kind) if row.document_kind else None,
                confidence=row.confidence,
                counterparty=row.counterparty,
                account_code=row.account_code,
                amount=Decimal(row.amount),
            )
            for row in result
        ]

    async def claim_by_rule(self, *, proposal_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
        """pending -> approved, naming the rule; decided_by_user_id stays NULL - the system
        decided. The row lock serialises it against a person's concurrent decision (NFR-032)."""
        result = await self._session.execute(
            text(
                "UPDATE booking_proposal SET status = 'approved', decided_at = now(), "
                "  rule_id = :rule "
                "WHERE id = :id AND status = 'pending' RETURNING id"
            ),
            {"id": str(proposal_id), "rule": str(rule_id)},
        )
        return result.first() is not None

    # -- the rules screen -------------------------------------------------------------

    async def views(self, *, administration_id: uuid.UUID) -> Sequence[RuleView]:
        """Active and suspended rules; retired ones are history, not something to manage."""
        result = await self._session.execute(
            text(_VIEW_SQL + " AND r.status <> 'retired' ORDER BY r.created_at, r.id"),
            {"admin": str(administration_id)},
        )
        return [_view(row) for row in result]

    async def view(self, *, administration_id: uuid.UUID, rule_id: uuid.UUID) -> RuleView | None:
        result = await self._session.execute(
            text(_VIEW_SQL + " AND r.id = :id"),
            {"admin": str(administration_id), "id": str(rule_id)},
        )
        row = result.first()
        return None if row is None else _view(row)

    async def postings(self, *, administration_id: uuid.UUID, limit: int) -> Sequence[RulePosting]:
        result = await self._session.execute(
            text(
                """
                SELECT p.id, p.rule_id, t.booking_date, p.amount, p.counterparty,
                       p.account_code, p.decided_at
                  FROM booking_proposal p
                  JOIN bank_transaction t ON t.id = p.bank_transaction_id
                 WHERE p.administration_id = :admin
                   AND p.status = 'approved'
                   AND p.rule_id IS NOT NULL
                 ORDER BY p.decided_at DESC, p.id
                 LIMIT :limit
                """
            ),
            {"admin": str(administration_id), "limit": limit},
        )
        return [
            RulePosting(
                proposal_id=row.id,
                rule_id=row.rule_id,
                date=row.booking_date,
                amount=Decimal(row.amount),
                counterparty=row.counterparty,
                account_code=row.account_code,
                posted_at=row.decided_at,
            )
            for row in result
        ]

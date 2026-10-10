"""The firm's bulk "Request missing receipts": preview, then request (ADR-114).

Both take the administrations the person ticked, keep only those in their authorized portfolio
(`require_portfolio_permission`, ADR-109) - an id outside it is reported as not found, never
read - and answer for the rest in a fixed number of queries.

A request does not send the e-mail itself. The firm's session cannot see who the client's people
are (0009: an organization's grants are visible only to that organization; ADR-111), so it records
a `chase_request` and the sweep (`api.chasing.sweep`, every few minutes) delivers it from the
client's own tenant context. "sent" in the response therefore counts requests accepted for
delivery; the worklist's `last_chased_at` moves when the sweep has actually sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditOutcome
from api.chasing.model import BlockedReason, blocked_reason
from api.chasing.repository import ChaseFacts, RecordedRequest

#: The worklist's own "Show all" ceiling: a firm can ask about every client on one page.
MAX_ADMINISTRATIONS = 1000

NOT_FOUND = "administration_not_found"


class ChaseStore(Protocol):
    async def missing_counts(
        self, administration_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]: ...

    async def facts(
        self, administration_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ChaseFacts]: ...

    async def record_request(
        self, administration_id: uuid.UUID, *, missing_count: int, user_id: uuid.UUID
    ) -> RecordedRequest | None: ...


class AuditRecorder(Protocol):
    async def record(self, event: AuditEvent) -> object: ...


@dataclass(frozen=True, slots=True)
class PreviewItem:
    administration_id: uuid.UUID
    display_name: str
    missing_count: int
    recipient_count: int | None
    last_chased_at: datetime | None
    blocked_reason: BlockedReason | None


@dataclass(frozen=True, slots=True)
class Skipped:
    administration_id: uuid.UUID
    reason: str


@dataclass
class RequestOutcome:
    sent: int = 0
    skipped: list[Skipped] = field(default_factory=list)


class TooManyAdministrations(ValueError):
    pass


def _unique(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    unique = list(dict.fromkeys(ids))
    if len(unique) > MAX_ADMINISTRATIONS:
        raise TooManyAdministrations(len(unique))
    return unique


class ChaseService:
    def __init__(self, store: ChaseStore, audit: AuditRecorder) -> None:
        self._store = store
        self._audit = audit

    async def preview(
        self,
        *,
        requested: Sequence[uuid.UUID],
        portfolio: Mapping[uuid.UUID, str],
        now: datetime,
    ) -> list[PreviewItem]:
        """One item per requested administration in the portfolio, in the order asked; ids
        outside the portfolio are left out (nothing is disclosed about them)."""
        ids = [a for a in _unique(requested) if a in portfolio]
        counts = await self._store.missing_counts(ids)
        facts = await self._store.facts(ids)
        items: list[PreviewItem] = []
        for administration_id in ids:
            fact = facts.get(administration_id, ChaseFacts())
            missing_count = counts.get(administration_id, 0)
            items.append(
                PreviewItem(
                    administration_id=administration_id,
                    display_name=portfolio[administration_id],
                    missing_count=missing_count,
                    recipient_count=fact.recipient_count,
                    last_chased_at=fact.last_chased_at,
                    blocked_reason=blocked_reason(
                        missing_count=missing_count,
                        last_chased_at=fact.last_chased_at,
                        last_requested_at=fact.last_requested_at,
                        recipient_count=fact.recipient_count,
                        now=now,
                    ),
                )
            )
        return items

    async def request(
        self,
        *,
        requested: Sequence[uuid.UUID],
        portfolio: Mapping[uuid.UUID, str],
        user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
        now: datetime,
    ) -> RequestOutcome:
        """Record a chase request for every requested client that the preview would not block.

        Each accepted request is audited individually under the client's own organization (the
        client reads "my accountant asked for receipts" in its log, ADR-112); the request as a
        whole is audited by the route's declared category."""
        outcome = RequestOutcome()
        unique = _unique(requested)
        for administration_id in unique:
            if administration_id not in portfolio:
                outcome.skipped.append(Skipped(administration_id, NOT_FOUND))
        items = await self.preview(requested=unique, portfolio=portfolio, now=now)
        for item in items:
            if item.blocked_reason is not None:
                outcome.skipped.append(Skipped(item.administration_id, item.blocked_reason.value))
                continue
            recorded = await self._store.record_request(
                item.administration_id, missing_count=item.missing_count, user_id=user_id
            )
            if recorded is None:  # pragma: no cover - the portfolio is visible by construction
                outcome.skipped.append(Skipped(item.administration_id, NOT_FOUND))
                continue
            await self._audit.record(
                AuditEvent(
                    organization_id=recorded.organization_id,
                    administration_id=item.administration_id,
                    category=AuditCategory.CONFIGURATION,
                    action="request_receipt_chase",
                    resource_type="receipt_chase",
                    resource_id=recorded.request_id,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.USER,
                    actor_user_id=user_id,
                    occurred_at=now,
                    detail={
                        "missing_count": item.missing_count,
                        "acting_organization_id": str(acting_organization_id),
                    },
                )
            )
            outcome.sent += 1
        return outcome

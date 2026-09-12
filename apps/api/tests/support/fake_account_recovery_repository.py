"""In-memory AccountRecoveryEventRepository double for testing
api.auth.account_recovery without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from api.auth.account_recovery import AccountRecoveryEvent, RecoveryMethod


class InMemoryAccountRecoveryEventRepository:
    def __init__(self) -> None:
        self._events: list[AccountRecoveryEvent] = []

    async def record(
        self,
        *,
        target_user_id: uuid.UUID,
        method: RecoveryMethod,
        performed_by_user_id: uuid.UUID | None,
        reason: str | None,
    ) -> AccountRecoveryEvent:
        event = AccountRecoveryEvent(
            id=uuid.uuid4(),
            target_user_id=target_user_id,
            method=method,
            performed_by_user_id=performed_by_user_id,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self._events.append(event)
        return event

    async def list_for_user(self, target_user_id: uuid.UUID) -> list[AccountRecoveryEvent]:
        return sorted(
            (e for e in self._events if e.target_user_id == target_user_id),
            key=lambda e: e.created_at,
            reverse=True,
        )

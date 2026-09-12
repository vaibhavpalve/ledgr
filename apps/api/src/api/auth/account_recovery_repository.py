"""SQLAlchemy-backed AccountRecoveryEventRepository, reading/writing
account_recovery_event through an ordinary AsyncSession - see
migrations/0007_account_recovery.sql.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.account_recovery import AccountRecoveryEvent, RecoveryMethod

_SELECT_COLUMNS = "id, target_user_id, method, performed_by_user_id, reason, created_at"


def _row_to_event(row: Row[Any]) -> AccountRecoveryEvent:
    return AccountRecoveryEvent(
        id=row.id,
        target_user_id=row.target_user_id,
        method=row.method,
        performed_by_user_id=row.performed_by_user_id,
        reason=row.reason,
        created_at=row.created_at,
    )


class SqlAccountRecoveryEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        target_user_id: uuid.UUID,
        method: RecoveryMethod,
        performed_by_user_id: uuid.UUID | None,
        reason: str | None,
    ) -> AccountRecoveryEvent:
        result = await self._session.execute(
            text(
                "INSERT INTO account_recovery_event "
                "(target_user_id, method, performed_by_user_id, reason) "
                "VALUES (:target_user_id, :method, :performed_by_user_id, :reason) "
                f"RETURNING {_SELECT_COLUMNS}"
            ),
            {
                "target_user_id": str(target_user_id),
                "method": method,
                "performed_by_user_id": (
                    str(performed_by_user_id) if performed_by_user_id else None
                ),
                "reason": reason,
            },
        )
        return _row_to_event(result.one())

    async def list_for_user(self, target_user_id: uuid.UUID) -> list[AccountRecoveryEvent]:
        result = await self._session.execute(
            text(
                f"SELECT {_SELECT_COLUMNS} FROM account_recovery_event "
                "WHERE target_user_id = :target_user_id ORDER BY created_at DESC"
            ),
            {"target_user_id": str(target_user_id)},
        )
        return [_row_to_event(row) for row in result]

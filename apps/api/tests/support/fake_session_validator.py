"""In-memory SessionValidator for testing api.tenancy without a database.

Not a stub that says yes: it is the real api.auth.sessions.SessionService
over the in-memory SessionRepository the rest of tests/auth/ already uses,
so the middleware tests exercise the same revocation/expiry/idle/ownership
logic the SQL-backed validator runs in production - only the storage
differs. The DB-backed proof that SqlSessionValidator behaves identically
against real rows lives in tests/integration/test_session_backed_context.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from api.auth.models import Session
from api.auth.sessions import SessionService
from tests.support.fake_auth_repository import InMemorySessionRepository


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemorySessionValidator:
    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self.repository = InMemorySessionRepository()
        self.service = SessionService(self.repository, clock=clock)

    async def issue(
        self,
        user_id: uuid.UUID,
        *,
        mfa_verified: bool = True,
        privileged: bool = True,
        active_administration_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        session, _raw = await self.service.issue_session(
            user_id, privileged=privileged, mfa_verified=mfa_verified
        )
        if active_administration_id is not None:
            self.repository.set_active_administration(session.id, active_administration_id)
        return session.id

    async def revoke(self, session_id: uuid.UUID) -> None:
        await self.service.revoke_session(session_id)

    async def validate(self, session_id: uuid.UUID, *, user_id: uuid.UUID) -> Session:
        return await self.service.validate_session_by_id(session_id, user_id=user_id)

"""The per-request session lookup behind api.tenancy.TenantContextMiddleware.

ADR-054 shipped a bearer JWT whose claims RESTATE a session row's state at
the moment of issuance, and named the consequence as a bounded gap: a
session revoked mid-lifetime kept working until the token's own `exp`,
because nothing re-read the row on the requests in between. This module is
the follow-up that closes it (see
docs/decisions/ADR-060-session-backed-tenant-context.md): every non-exempt
request resolves the token's `sid` against `sessions` and takes
`mfa_verified`, `active_administration_id`, revocation, absolute expiry and
the idle timeout (IAM-016) from the ROW. The JWT still proves who minted the
token and which organization it is for; it no longer gets to say whether the
session is alive.

--- Why a Protocol, and why this is not in api.tenancy ---

api.tenancy is imported by api.db (for get_tenant_context), so it cannot
import api.db back to open a database session without a cycle. The SQL
lookup therefore lives here, and api.tenancy reaches it through
`SessionValidator` - a one-method Protocol the middleware is handed at
construction, reads off `app.state.session_validator` when a test installs
one there, or builds lazily from this module when given neither. That is
the same injection shape api.mfa_middleware uses for its enrolment checker,
and for the same reason: the unit suite runs the middleware against an
in-memory fake (tests/support/fake_session_validator.py) and never touches
Postgres, while the DB-backed integration suite exercises SqlSessionValidator
for real - including revoke-then-request. Neither skips the check.

--- One primary-key read, one touch ---

SqlSessionValidator opens its own short transaction per request, separate
from the handler's tenant-scoped one, because the handler's session does not
exist yet when this runs - the whole point is that the tenant context it is
scoped to is being established here. The touch (last_active_at) is what
makes IAM-016's idle timeout a statement about real activity rather than
about token issuance.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.auth.models import Session
from api.auth.repository import SqlSessionRepository
from api.auth.sessions import SessionService
from api.config import settings


class SessionValidator(Protocol):
    async def validate(self, session_id: uuid.UUID, *, user_id: uuid.UUID) -> Session:
        """Returns the live session or raises an
        api.auth.sessions.SessionError subclass saying exactly why it is
        not live (not found, revoked, expired, idle, or owned by someone
        else). Touches last_active_at on success.
        """
        ...


class SqlSessionValidator:
    """Production: a fresh transaction per call over the shared engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def validate(self, session_id: uuid.UUID, *, user_id: uuid.UUID) -> Session:
        async with self._session_factory() as session, session.begin():
            service = SessionService(
                SqlSessionRepository(session),
                max_lifetime_hours=settings.session_max_lifetime_hours,
                idle_timeout_minutes_privileged=settings.session_idle_timeout_minutes_privileged,
            )
            return await service.validate_session_by_id(session_id, user_id=user_id)


def build_session_validator() -> SessionValidator:
    """The default api.tenancy falls back to. Imported lazily from there -
    see the module docstring for the import cycle this avoids.
    """
    from api.db import engine

    return SqlSessionValidator(engine)

"""Persistence boundary for users, password credentials, and sessions.
Same shape as api.crypto.repository: Protocols here so
api.auth.service.AuthenticationService and api.auth.sessions.SessionService
are unit-testable against in-memory fakes
(tests/support/fake_auth_repository.py) without a database; the real
SQLAlchemy-backed implementations below are exercised by
tests/integration/test_auth_schema.py, which proves the actual schema
(migrations/0003_authentication.sql) holds.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.models import PasswordCredential, Session, User


class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...

    async def create(self, email: str) -> User: ...

    async def get_password_credential(self, user_id: uuid.UUID) -> PasswordCredential | None: ...

    async def upsert_password_credential(
        self, user_id: uuid.UUID, *, password_hash: str, algorithm: str = "argon2id"
    ) -> PasswordCredential: ...

    async def mark_email_verified(self, user_id: uuid.UUID, *, at: datetime) -> bool:
        """IAM-010b. Sets users.email_verified_at once - a second call is a
        no-op returning False, so a verification link cannot move the
        timestamp of an address that was already proven.
        """
        ...


class SessionRepository(Protocol):
    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        privileged: bool,
        created_at: datetime,
        expires_at: datetime,
        mfa_verified_at: datetime | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Session: ...

    async def get_by_token_hash(self, token_hash: str) -> Session | None: ...

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        """The lookup api.tenancy.TenantContextMiddleware performs on every
        request: the bearer JWT names the session by id (`sid`), not by its
        raw secret, so this is the read that makes the session row - not the
        token's restated claims - the authority on revocation, expiry, MFA
        and the active administration.
        """
        ...

    async def touch(self, session_id: uuid.UUID, *, at: datetime) -> None: ...

    async def record_reauthentication(self, session_id: uuid.UUID, *, at: datetime) -> None: ...

    async def record_mfa_verification(self, session_id: uuid.UUID, *, at: datetime) -> None: ...

    async def revoke(self, session_id: uuid.UUID, *, at: datetime) -> None: ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]: ...


_USER_COLUMNS = "id, email, status, mfa_enrolled, email_verified_at"
_CREDENTIAL_COLUMNS = "id, user_id, password_hash, algorithm"
_SESSION_COLUMNS = (
    "id, user_id, token_hash, privileged, created_at, expires_at, "
    "last_active_at, last_reauthenticated_at, mfa_verified_at, revoked_at, "
    "ip_address, user_agent, active_administration_id"
)


def _row_to_user(row: Row[Any]) -> User:
    return User(
        id=row.id,
        email=str(row.email),
        status=row.status,
        mfa_enrolled=row.mfa_enrolled,
        email_verified_at=row.email_verified_at,
    )


def _row_to_credential(row: Row[Any]) -> PasswordCredential:
    return PasswordCredential(
        id=row.id, user_id=row.user_id, password_hash=row.password_hash, algorithm=row.algorithm
    )


def _row_to_session(row: Row[Any]) -> Session:
    return Session(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        privileged=row.privileged,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_active_at=row.last_active_at,
        last_reauthenticated_at=row.last_reauthenticated_at,
        mfa_verified_at=row.mfa_verified_at,
        revoked_at=row.revoked_at,
        ip_address=str(row.ip_address) if row.ip_address is not None else None,
        user_agent=row.user_agent,
        active_administration_id=row.active_administration_id,
    )


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            text(f"SELECT {_USER_COLUMNS} FROM users WHERE email = :email"),
            {"email": email},
        )
        row = result.first()
        return _row_to_user(row) if row is not None else None

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            text(f"SELECT {_USER_COLUMNS} FROM users WHERE id = :id"),
            {"id": str(user_id)},
        )
        row = result.first()
        return _row_to_user(row) if row is not None else None

    async def create(self, email: str) -> User:
        result = await self._session.execute(
            text(f"INSERT INTO users (email) VALUES (:email) RETURNING {_USER_COLUMNS}"),
            {"email": email},
        )
        return _row_to_user(result.one())

    async def get_password_credential(self, user_id: uuid.UUID) -> PasswordCredential | None:
        result = await self._session.execute(
            text(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM user_password_credential "
                "WHERE user_id = :user_id"
            ),
            {"user_id": str(user_id)},
        )
        row = result.first()
        return _row_to_credential(row) if row is not None else None

    async def upsert_password_credential(
        self, user_id: uuid.UUID, *, password_hash: str, algorithm: str = "argon2id"
    ) -> PasswordCredential:
        result = await self._session.execute(
            text(
                "INSERT INTO user_password_credential (user_id, password_hash, algorithm) "
                "VALUES (:user_id, :password_hash, :algorithm) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "  password_hash = excluded.password_hash, "
                "  algorithm = excluded.algorithm, "
                "  updated_at = now() "
                f"RETURNING {_CREDENTIAL_COLUMNS}"
            ),
            {"user_id": str(user_id), "password_hash": password_hash, "algorithm": algorithm},
        )
        return _row_to_credential(result.one())

    async def mark_email_verified(self, user_id: uuid.UUID, *, at: datetime) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE users SET email_verified_at = :at, updated_at = now() "
                "WHERE id = :id AND email_verified_at IS NULL RETURNING id"
            ),
            {"id": str(user_id), "at": at},
        )
        return result.first() is not None


class SqlSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        privileged: bool,
        created_at: datetime,
        expires_at: datetime,
        mfa_verified_at: datetime | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Session:
        result = await self._session.execute(
            text(
                "INSERT INTO sessions "
                "(user_id, token_hash, privileged, created_at, expires_at, "
                " last_active_at, last_reauthenticated_at, mfa_verified_at, "
                " ip_address, user_agent) "
                "VALUES "
                "(:user_id, :token_hash, :privileged, :created_at, :expires_at, "
                " :created_at, :created_at, :mfa_verified_at, :ip_address, :user_agent) "
                f"RETURNING {_SESSION_COLUMNS}"
            ),
            {
                "user_id": str(user_id),
                "token_hash": token_hash,
                "privileged": privileged,
                "created_at": created_at,
                "expires_at": expires_at,
                "mfa_verified_at": mfa_verified_at,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        return _row_to_session(result.one())

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        result = await self._session.execute(
            text(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE token_hash = :token_hash"),
            {"token_hash": token_hash},
        )
        row = result.first()
        return _row_to_session(row) if row is not None else None

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        result = await self._session.execute(
            text(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = :id"),
            {"id": str(session_id)},
        )
        row = result.first()
        return _row_to_session(row) if row is not None else None

    async def touch(self, session_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE sessions SET last_active_at = :at WHERE id = :id"),
            {"id": str(session_id), "at": at},
        )

    async def record_reauthentication(self, session_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text(
                "UPDATE sessions SET last_reauthenticated_at = :at, last_active_at = :at "
                "WHERE id = :id"
            ),
            {"id": str(session_id), "at": at},
        )

    async def record_mfa_verification(self, session_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE sessions SET mfa_verified_at = :at WHERE id = :id"),
            {"id": str(session_id), "at": at},
        )

    async def revoke(self, session_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE sessions SET revoked_at = :at WHERE id = :id"),
            {"id": str(session_id), "at": at},
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]:
        result = await self._session.execute(
            text(
                f"SELECT {_SESSION_COLUMNS} FROM sessions "
                "WHERE user_id = :user_id ORDER BY created_at DESC"
            ),
            {"user_id": str(user_id)},
        )
        return [_row_to_session(row) for row in result]

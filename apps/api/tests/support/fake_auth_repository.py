"""In-memory UserRepository/SessionRepository doubles, for testing
api.auth.service and api.auth.sessions without a database. The real
SQLAlchemy-backed repositories (api.auth.repository) are exercised
separately by tests/integration/test_auth_schema.py.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from api.auth.models import PasswordCredential, Session, User


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._users_by_id: dict[uuid.UUID, User] = {}
        self._credentials_by_user_id: dict[uuid.UUID, PasswordCredential] = {}

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._users_by_id.values() if u.email == email), None)

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._users_by_id.get(user_id)

    async def create(self, email: str) -> User:
        # Mirrors the real schema's UNIQUE constraint on users.email.
        if await self.get_by_email(email) is not None:
            raise ValueError(f"user with email {email!r} already exists")
        user = User(id=uuid.uuid4(), email=email, status="active", mfa_enrolled=False)
        self._users_by_id[user.id] = user
        return user

    async def get_password_credential(self, user_id: uuid.UUID) -> PasswordCredential | None:
        return self._credentials_by_user_id.get(user_id)

    async def upsert_password_credential(
        self, user_id: uuid.UUID, *, password_hash: str, algorithm: str = "argon2id"
    ) -> PasswordCredential:
        existing = self._credentials_by_user_id.get(user_id)
        credential = PasswordCredential(
            id=existing.id if existing else uuid.uuid4(),
            user_id=user_id,
            password_hash=password_hash,
            algorithm=algorithm,
        )
        self._credentials_by_user_id[user_id] = credential
        return credential

    def set_status(self, user_id: uuid.UUID, status: str) -> None:
        """Test-only helper - the real schema updates this via ordinary
        application code (offboarding, suspension), not exposed on the
        Protocol because no such flow exists yet to call it from.
        """
        self._users_by_id[user_id] = replace(self._users_by_id[user_id], status=status)


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, Session] = {}

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
        # Mirrors the real schema's UNIQUE constraint on sessions.token_hash.
        if any(s.token_hash == token_hash for s in self._sessions.values()):
            raise ValueError("token_hash collision")
        session = Session(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            privileged=privileged,
            created_at=created_at,
            expires_at=expires_at,
            last_active_at=created_at,
            last_reauthenticated_at=created_at,
            mfa_verified_at=mfa_verified_at,
            revoked_at=None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._sessions[session.id] = session
        return session

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        return next((s for s in self._sessions.values() if s.token_hash == token_hash), None)

    async def touch(self, session_id: uuid.UUID, *, at: datetime) -> None:
        self._sessions[session_id] = replace(self._sessions[session_id], last_active_at=at)

    async def record_reauthentication(self, session_id: uuid.UUID, *, at: datetime) -> None:
        self._sessions[session_id] = replace(
            self._sessions[session_id], last_reauthenticated_at=at, last_active_at=at
        )

    async def record_mfa_verification(self, session_id: uuid.UUID, *, at: datetime) -> None:
        self._sessions[session_id] = replace(self._sessions[session_id], mfa_verified_at=at)

    async def revoke(self, session_id: uuid.UUID, *, at: datetime) -> None:
        self._sessions[session_id] = replace(self._sessions[session_id], revoked_at=at)

    async def list_for_user(self, user_id: uuid.UUID) -> list[Session]:
        return sorted(
            (s for s in self._sessions.values() if s.user_id == user_id),
            key=lambda s: s.created_at,
            reverse=True,
        )

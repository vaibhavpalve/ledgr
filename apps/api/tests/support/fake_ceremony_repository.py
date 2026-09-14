"""In-memory CeremonyRepository double for the e-mail verification kind, so
api.auth.email_verification's issue/verify logic is testable without a
database. Mirrors the SQL implementation's single-use and TTL semantics
(consume is refused for a consumed or expired row) and its "issuing a new
link retires the old one" rule, because those are the properties the tests
are about. The WebAuthn and Google kinds are not modelled - nothing in
tests/ needs them off-database.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from api.auth.ceremony import (
    EMAIL_VERIFICATION_TTL,
    CeremonyNotFoundError,
    GoogleCeremony,
    PasskeyCeremony,
)


@dataclass
class _EmailCeremony:
    id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime
    consumed_at: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryCeremonyRepository:
    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self.email_ceremonies: dict[uuid.UUID, _EmailCeremony] = {}

    async def create_email_verification_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID:
        now = self._clock()
        for ceremony in self.email_ceremonies.values():
            if ceremony.user_id == user_id and ceremony.consumed_at is None:
                ceremony.consumed_at = now
        ceremony_id = uuid.uuid4()
        self.email_ceremonies[ceremony_id] = _EmailCeremony(
            id=ceremony_id, user_id=user_id, expires_at=now + EMAIL_VERIFICATION_TTL
        )
        return ceremony_id

    async def consume_email_verification_ceremony(self, token_id: uuid.UUID) -> uuid.UUID:
        now = self._clock()
        ceremony = self.email_ceremonies.get(token_id)
        if ceremony is None or ceremony.consumed_at is not None or ceremony.expires_at <= now:
            raise CeremonyNotFoundError
        ceremony.consumed_at = now
        return ceremony.user_id

    # The rest of the Protocol, unmodelled - see the module docstring.

    async def create_passkey_ceremony(
        self,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
        challenge: bytes,
        user_id: uuid.UUID | None,
    ) -> uuid.UUID:
        raise NotImplementedError

    async def consume_passkey_ceremony(
        self,
        ceremony_id: uuid.UUID,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
    ) -> PasskeyCeremony:
        raise NotImplementedError

    async def create_google_ceremony(
        self, *, state: str, nonce: str, code_verifier: str
    ) -> uuid.UUID:
        raise NotImplementedError

    async def consume_google_ceremony(self, *, state: str) -> GoogleCeremony:
        raise NotImplementedError

    async def create_google_signup_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID:
        raise NotImplementedError

    async def consume_google_signup_ceremony(self, ticket_id: uuid.UUID) -> uuid.UUID:
        raise NotImplementedError

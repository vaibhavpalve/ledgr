"""Short-lived, single-use storage for the two sign-in methods whose
protocol needs a server-held round trip before the caller is known or
before a second request can be trusted to belong to the first: WebAuthn
challenges and Google OIDC state/nonce/PKCE verifier. See migration 0046's
own comment for why these share one table.

Neither api.auth.passkeys nor api.auth.google_oidc persists this state
themselves - both modules say so in their own docstrings, deliberately
leaving the storage decision to whatever builds the HTTP layer. This module
is that decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CeremonyKind = Literal["passkey_registration", "passkey_authentication", "google_oidc"]

_TTL = timedelta(minutes=5)


class CeremonyError(Exception):
    """Base class for anything wrong with a ceremony lookup."""


class CeremonyNotFoundError(CeremonyError):
    """No such ceremony, it already expired, or it was already consumed -
    deliberately one exception for all three: a caller acting on an id it
    did not itself just receive from begin_* has no legitimate case to
    distinguish them, and an attacker probing for "expired" vs "consumed"
    vs "never existed" learns nothing from the difference.
    """


@dataclass(frozen=True, slots=True)
class PasskeyCeremony:
    id: uuid.UUID
    kind: Literal["passkey_registration", "passkey_authentication"]
    challenge: bytes
    user_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class GoogleCeremony:
    id: uuid.UUID
    state: str
    nonce: str
    code_verifier: str


class CeremonyRepository(Protocol):
    async def create_passkey_ceremony(
        self,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
        challenge: bytes,
        user_id: uuid.UUID | None,
    ) -> uuid.UUID: ...

    async def consume_passkey_ceremony(
        self,
        ceremony_id: uuid.UUID,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
    ) -> PasskeyCeremony: ...

    async def create_google_ceremony(
        self, *, state: str, nonce: str, code_verifier: str
    ) -> uuid.UUID: ...

    async def consume_google_ceremony(self, *, state: str) -> GoogleCeremony: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SqlCeremonyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_passkey_ceremony(
        self,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
        challenge: bytes,
        user_id: uuid.UUID | None,
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO auth_ceremony (kind, webauthn_challenge, user_id, expires_at) "
                "VALUES (:kind, :challenge, :user_id, :expires_at) RETURNING id"
            ),
            {
                "kind": kind,
                "challenge": challenge,
                "user_id": str(user_id) if user_id else None,
                "expires_at": _utcnow() + _TTL,
            },
        )
        ceremony_id: uuid.UUID = result.scalar_one()
        return ceremony_id

    async def consume_passkey_ceremony(
        self,
        ceremony_id: uuid.UUID,
        *,
        kind: Literal["passkey_registration", "passkey_authentication"],
    ) -> PasskeyCeremony:
        # UPDATE ... RETURNING in one statement: the row is claimed
        # (consumed_at set) and read back atomically, so two concurrent
        # completion attempts for the same ceremony cannot both succeed -
        # the second finds zero rows matching `consumed_at is null`.
        result = await self._session.execute(
            text(
                "UPDATE auth_ceremony SET consumed_at = :now "
                "WHERE id = :id AND kind = :kind AND consumed_at IS NULL AND expires_at > :now "
                "RETURNING webauthn_challenge, user_id"
            ),
            {"id": str(ceremony_id), "kind": kind, "now": _utcnow()},
        )
        row = result.first()
        if row is None:
            raise CeremonyNotFoundError
        return PasskeyCeremony(
            id=ceremony_id,
            kind=kind,
            challenge=bytes(row.webauthn_challenge),
            user_id=row.user_id,
        )

    async def create_google_ceremony(
        self, *, state: str, nonce: str, code_verifier: str
    ) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "INSERT INTO auth_ceremony "
                "(kind, google_state, google_nonce, google_code_verifier, expires_at) "
                "VALUES ('google_oidc', :state, :nonce, :code_verifier, :expires_at) "
                "RETURNING id"
            ),
            {
                "state": state,
                "nonce": nonce,
                "code_verifier": code_verifier,
                "expires_at": _utcnow() + _TTL,
            },
        )
        ceremony_id: uuid.UUID = result.scalar_one()
        return ceremony_id

    async def consume_google_ceremony(self, *, state: str) -> GoogleCeremony:
        result = await self._session.execute(
            text(
                "UPDATE auth_ceremony SET consumed_at = :now "
                "WHERE google_state = :state AND consumed_at IS NULL AND expires_at > :now "
                "RETURNING id, google_nonce, google_code_verifier"
            ),
            {"state": state, "now": _utcnow()},
        )
        row = result.first()
        if row is None:
            raise CeremonyNotFoundError
        return GoogleCeremony(
            id=row.id, state=state, nonce=row.google_nonce, code_verifier=row.google_code_verifier
        )

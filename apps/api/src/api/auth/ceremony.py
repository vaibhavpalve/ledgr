"""Short-lived, single-use storage for the sign-in methods whose protocol
needs a server-held round trip before the caller is known or before a
second request can be trusted to belong to the first: WebAuthn challenges,
Google OIDC state/nonce/PKCE verifier, and (added for Google-initiated
signup) a ticket handing a just-created bare user back to a follow-up
"finish your account" request. See migration 0046's own comment for why
the first two share one table; `google_signup` reuses the same table and
TTL discipline for the same reason - one shape, one cleanup policy.

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

CeremonyKind = Literal[
    "passkey_registration",
    "passkey_authentication",
    "google_oidc",
    "google_signup",
    "email_verification",
]

_TTL = timedelta(minutes=5)
# IAM-010b. A verification link is read from a mailbox, not completed in the
# same sitting as a WebAuthn prompt, so the five-minute TTL the sign-in
# ceremonies share would expire most of them before they were opened. A day
# is the conventional window; a link is single-use and superseded by any
# later resend regardless, so the longer life is not a standing replay.
EMAIL_VERIFICATION_TTL = timedelta(hours=24)


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

    async def create_google_signup_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID: ...

    async def consume_google_signup_ceremony(self, ticket_id: uuid.UUID) -> uuid.UUID: ...

    async def create_email_verification_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID: ...

    async def consume_email_verification_ceremony(self, token_id: uuid.UUID) -> uuid.UUID: ...


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

    async def create_google_signup_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID:
        """A one-time ticket handed to the frontend when
        `api.auth.google_signin.GoogleSignInService.sign_in` resolves a
        Google identity with no existing account: that call already
        created a bare `User` row and linked the identity (see its own
        docstring), so what remains is FR-MDL-001's one question -
        account_model/organization_name/kvk_number - which Google's own
        redirect has no room to carry. `user_id` is the only field this
        kind needs; `api.auth.routes.signup_google` resolves it back via
        `consume_google_signup_ceremony` rather than trusting a client-
        supplied id.
        """
        result = await self._session.execute(
            text(
                "INSERT INTO auth_ceremony (kind, user_id, expires_at) "
                "VALUES ('google_signup', :user_id, :expires_at) RETURNING id"
            ),
            {"user_id": str(user_id), "expires_at": _utcnow() + _TTL},
        )
        ceremony_id: uuid.UUID = result.scalar_one()
        return ceremony_id

    async def consume_google_signup_ceremony(self, ticket_id: uuid.UUID) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "UPDATE auth_ceremony SET consumed_at = :now "
                "WHERE id = :id AND kind = 'google_signup' "
                "AND consumed_at IS NULL AND expires_at > :now "
                "RETURNING user_id"
            ),
            {"id": str(ticket_id), "now": _utcnow()},
        )
        row = result.first()
        if row is None or row.user_id is None:
            raise CeremonyNotFoundError
        user_id: uuid.UUID = row.user_id
        return user_id

    async def create_email_verification_ceremony(self, *, user_id: uuid.UUID) -> uuid.UUID:
        """IAM-010b's single-use token, the same shape as the google_signup
        ticket (migration 0049 widens the kind CHECK the way 0047 did): the
        row's own id is the secret in the link. Postgres' gen_random_uuid()
        is 122 bits of CSPRNG output, which is more entropy than the
        24-byte `state` the Google ceremony already stakes its replay
        protection on.

        Any earlier unconsumed link for this user is retired first, so
        "request a new link" means exactly that - the newest one works and
        an older one found in a forwarded mail does not.
        """
        now = _utcnow()
        await self._session.execute(
            text(
                "UPDATE auth_ceremony SET consumed_at = :now "
                "WHERE kind = 'email_verification' AND user_id = :user_id "
                "AND consumed_at IS NULL"
            ),
            {"user_id": str(user_id), "now": now},
        )
        result = await self._session.execute(
            text(
                "INSERT INTO auth_ceremony (kind, user_id, expires_at) "
                "VALUES ('email_verification', :user_id, :expires_at) RETURNING id"
            ),
            {"user_id": str(user_id), "expires_at": now + EMAIL_VERIFICATION_TTL},
        )
        ceremony_id: uuid.UUID = result.scalar_one()
        return ceremony_id

    async def consume_email_verification_ceremony(self, token_id: uuid.UUID) -> uuid.UUID:
        result = await self._session.execute(
            text(
                "UPDATE auth_ceremony SET consumed_at = :now "
                "WHERE id = :id AND kind = 'email_verification' "
                "AND consumed_at IS NULL AND expires_at > :now "
                "RETURNING user_id"
            ),
            {"id": str(token_id), "now": _utcnow()},
        )
        row = result.first()
        if row is None or row.user_id is None:
            raise CeremonyNotFoundError
        user_id: uuid.UUID = row.user_id
        return user_id

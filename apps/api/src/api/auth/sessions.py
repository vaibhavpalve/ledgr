"""Session issuance and validation (IAM-016): 12-hour absolute maximum
lifetime, 30-minute idle timeout for privileged roles, a primitive for
"absolute re-authentication for sensitive actions."

Sessions are stateful (a database row per session, looked up by a hashed
bearer token on every request) rather than self-contained JWTs - see
docs/decisions/ADR-005-authentication-foundation.md for why: IAM-016's idle
timeout and IAM-017's "list sessions and revoke individually" both require
knowing, and being able to invalidate, a specific session's state at
request time, which a stateless token can't do without becoming stateful
anyway.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from api.auth.geolocation import GeoLocationResolver, Location, NullGeoLocationResolver
from api.auth.models import Session
from api.auth.repository import SessionRepository

_TOKEN_BYTES = 32  # 256 bits of entropy


@dataclass(frozen=True, slots=True)
class SessionListing:
    """IAM-017: a session enriched with its resolved location, ready to
    show a user their device list. Wraps rather than extends Session so
    Session itself (the persisted record) stays free of a value that is
    never stored, only computed on demand at listing time.
    """

    session: Session
    location: Location | None


class SessionError(Exception):
    """Base class for every reason validate_session can fail."""


class SessionNotFoundError(SessionError):
    pass


class SessionRevokedError(SessionError):
    pass


class SessionExpiredError(SessionError):
    """The 12-hour absolute maximum lifetime (IAM-016) has passed."""


class SessionIdleTimeoutError(SessionError):
    """The 30-minute idle timeout for privileged roles (IAM-016) has
    passed since the session's last activity.
    """


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        *,
        max_lifetime_hours: int = 12,
        idle_timeout_minutes_privileged: int = 30,
        clock: Callable[[], datetime] = _utcnow,
        geolocation: GeoLocationResolver | None = None,
    ) -> None:
        self._repository = repository
        self._max_lifetime = timedelta(hours=max_lifetime_hours)
        self._idle_timeout = timedelta(minutes=idle_timeout_minutes_privileged)
        self._clock = clock
        self._geolocation = geolocation or NullGeoLocationResolver()

    async def issue_session(
        self,
        user_id: uuid.UUID,
        *,
        privileged: bool,
        mfa_verified: bool = False,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[Session, str]:
        """Returns the session record AND the raw bearer token - the only
        moment the raw token is ever available. Only its hash is
        persisted; it cannot be recovered from storage afterward.

        `privileged` is supplied by the caller because this module has no
        role/permission model to derive it from yet (IAM-030+ is not
        built). Callers without a real answer should pass True - the
        safer, tighter-timeout default - not False.

        `mfa_verified` (IAM-011) means a second factor was ALREADY proven
        as part of establishing this session - e.g. a password+TOTP login,
        or a Google sign-in whose amr claim asserted 2FA
        (api.auth.mfa.google_asserts_second_factor, IAM-010e). Defaults to
        False - the safe default is "not verified," never "assume yes."
        """
        raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = self._clock()
        session = await self._repository.create(
            user_id=user_id,
            token_hash=_hash_token(raw_token),
            privileged=privileged,
            created_at=now,
            expires_at=now + self._max_lifetime,
            mfa_verified_at=now if mfa_verified else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return session, raw_token

    async def validate_session(self, raw_token: str) -> Session:
        """Looks up, validates, and - on success - touches (updates
        last_active_at on) the session referenced by raw_token. Raises a
        specific SessionError subclass describing exactly why validation
        failed, so callers (and tests) don't have to guess from one
        generic exception.
        """
        session = await self._repository.get_by_token_hash(_hash_token(raw_token))
        if session is None:
            raise SessionNotFoundError

        now = self._clock()

        if session.revoked_at is not None:
            raise SessionRevokedError
        if now >= session.expires_at:
            raise SessionExpiredError
        if session.privileged and (now - session.last_active_at) > self._idle_timeout:
            raise SessionIdleTimeoutError

        await self._repository.touch(session.id, at=now)
        return session

    async def record_reauthentication(self, session_id: uuid.UUID) -> None:
        """Call after a sensitive action re-proves the user's credential
        (IAM-016: "absolute re-authentication for sensitive actions").
        Updates last_reauthenticated_at for requires_recent_reauthentication
        below to check against. No sensitive-action endpoints exist yet to
        call this from - it is the primitive they will need.
        """
        await self._repository.record_reauthentication(session_id, at=self._clock())

    async def record_mfa_verification(self, session_id: uuid.UUID) -> None:
        """Call after a step-up MFA challenge succeeds mid-session (IAM-011).
        Marks the session MFA-verified from this point forward - a future
        HTTP-layer session integration is what would thread this through
        to api.mfa_middleware (via TenantContext.mfa_verified); this
        method just records the fact.
        """
        await self._repository.record_mfa_verification(session_id, at=self._clock())

    def requires_recent_reauthentication(self, session: Session, *, within: timedelta) -> bool:
        """True if the session's last full re-authentication is older than
        `within` - i.e. a sensitive action gated by this check should
        demand fresh credentials before proceeding.
        """
        return (self._clock() - session.last_reauthenticated_at) > within

    async def revoke_session(self, session_id: uuid.UUID) -> None:
        await self._repository.revoke(session_id, at=self._clock())

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        sessions = await self._repository.list_for_user(user_id)
        active = [s for s in sessions if s.revoked_at is None]
        for session in active:
            await self._repository.revoke(session.id, at=self._clock())
        return len(active)

    async def list_sessions(self, user_id: uuid.UUID) -> list[Session]:
        """IAM-017: the raw device session records - last-use
        (last_active_at) is on Session directly. For location, see
        list_sessions_with_location below.
        """
        return await self._repository.list_for_user(user_id)

    async def list_sessions_with_location(self, user_id: uuid.UUID) -> list[SessionListing]:
        """IAM-017's "listed to the user with location and last-use," in
        full: each session paired with its resolved location. A session
        with no recorded ip_address (or a resolver that can't place it -
        NullGeoLocationResolver always, or IpApiGeoLocationResolver on a
        private/loopback address or provider outage) gets location=None,
        never a placeholder value pretending to know where it came from.
        """
        sessions = await self._repository.list_for_user(user_id)
        listings = []
        for session in sessions:
            location = (
                await self._geolocation.resolve(session.ip_address)
                if session.ip_address is not None
                else None
            )
            listings.append(SessionListing(session=session, location=location))
        return listings

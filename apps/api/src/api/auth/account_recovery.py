"""Account recovery (IAM-018): "recovery never grants access on the
strength of an email alone; recovery requires a second verified factor or
an admin-initiated, logged reset."

Two paths, both logged (account_recovery_event, migrations/0007_account_recovery.sql):

  1. Self-service recovery, gated on proving a currently-enrolled second
     factor - TOTP or passkey - not merely receiving or clicking
     something sent to an email address. A compromised mailbox is a
     common, real account-takeover vector; requiring cryptographic proof
     of a factor the user actually still controls is what closes it.
     Google is deliberately NOT a recovery factor here: if a user can
     still sign in via Google, they are not locked out and don't need
     recovery - they can add a password through an ordinary authenticated
     settings flow instead (IAM-010f, api.auth.account_continuity).
     Recovery specifically serves the case where the user cannot get in
     at all through any authenticated path.
  2. Admin-initiated reset, for an account holding neither factor. This
     module does not itself verify the calling actor is authorized to
     perform a reset - that is IAM-030+'s (not yet built) job. It does
     make the actor's identity a required, permanently logged parameter -
     "admin-initiated, logged" holds regardless of what authorizes it.

Both paths revoke every existing session for the account afterward
(SessionService.revoke_all_for_user) - a credential reset must not leave
a potentially-compromised prior session still valid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from webauthn.helpers.structs import AuthenticationCredential

from api.auth.breach_check import PasswordBreachChecker
from api.auth.passkeys import PasskeyError, WebAuthnService
from api.auth.passwords import hash_password, validate_password_policy
from api.auth.repository import UserRepository
from api.auth.sessions import SessionService
from api.auth.totp import TotpError, TotpService

# Matches account_recovery_event's CHECK constraint exactly
# (migrations/0007_account_recovery.sql).
RecoveryMethod = Literal["totp", "passkey", "admin_reset"]


class AccountRecoveryError(Exception):
    """Base class for anything preventing a recovery attempt from
    succeeding.
    """


class InvalidRecoveryProofError(AccountRecoveryError):
    """The factor proof did not verify: no such account, a wrong TOTP
    code, or a passkey that does not belong to the account named. A
    single exception type deliberately covers all three - the same
    enumeration-avoidance reasoning as api.auth.service.
    InvalidCredentialsError (SEC-008): a caller must not be able to tell
    "no such account" apart from "wrong proof" from the error alone.
    """


class UserNotFoundError(AccountRecoveryError):
    """Admin-reset path only. The calling context there is already an
    authenticated admin flow (not yet built), where "no such user"
    genuinely is safe to disclose - unlike the self-service paths, there
    is no anonymous caller here to avoid leaking account existence to.
    """


class RecoveryReasonRequiredError(AccountRecoveryError):
    """Admin-initiated resets must record why - "logged" means more than
    just who and when.
    """


@dataclass(frozen=True, slots=True)
class AccountRecoveryEvent:
    id: uuid.UUID
    target_user_id: uuid.UUID
    method: RecoveryMethod
    performed_by_user_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class AccountRecoveryEventRepository(Protocol):
    async def record(
        self,
        *,
        target_user_id: uuid.UUID,
        method: RecoveryMethod,
        performed_by_user_id: uuid.UUID | None,
        reason: str | None,
    ) -> AccountRecoveryEvent: ...

    async def list_for_user(self, target_user_id: uuid.UUID) -> list[AccountRecoveryEvent]: ...


class AccountRecoveryService:
    def __init__(
        self,
        users: UserRepository,
        sessions: SessionService,
        totp: TotpService,
        webauthn: WebAuthnService,
        recovery_log: AccountRecoveryEventRepository,
        breach_checker: PasswordBreachChecker,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._totp = totp
        self._webauthn = webauthn
        self._recovery_log = recovery_log
        self._breach_checker = breach_checker

    async def recover_with_totp(self, *, email: str, code: str, new_password: str) -> uuid.UUID:
        """TOTP codes are not self-identifying (unlike a passkey
        assertion, which resolves its own owner) - email is required here
        specifically to know whose secret to check the code against, not
        as a substitute for proof of anything.
        """
        user = await self._users.get_by_email(email.strip().lower())
        if user is None:
            raise InvalidRecoveryProofError

        try:
            await self._totp.verify_code(user_id=user.id, code=code)
        except TotpError as exc:
            raise InvalidRecoveryProofError from exc

        await self._finish_recovery(
            target_user_id=user.id, new_password=new_password, method="totp"
        )
        return user.id

    async def recover_with_passkey(
        self, *, challenge: bytes, credential: AuthenticationCredential, new_password: str
    ) -> uuid.UUID:
        """No email parameter - WebAuthn is discoverable/usernameless by
        design (see api.auth.passkeys), so the credential itself resolves
        which account it belongs to.
        """
        try:
            passkey = await self._webauthn.complete_authentication(
                challenge=challenge, credential=credential
            )
        except PasskeyError as exc:
            raise InvalidRecoveryProofError from exc

        await self._finish_recovery(
            target_user_id=passkey.user_id, new_password=new_password, method="passkey"
        )
        return passkey.user_id

    async def admin_reset_password(
        self,
        *,
        target_user_id: uuid.UUID,
        new_password: str,
        performed_by_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise RecoveryReasonRequiredError

        user = await self._users.get_by_id(target_user_id)
        if user is None:
            raise UserNotFoundError(target_user_id)

        await self._finish_recovery(
            target_user_id=user.id,
            new_password=new_password,
            method="admin_reset",
            performed_by_user_id=performed_by_user_id,
            reason=reason.strip(),
        )

    async def _finish_recovery(
        self,
        *,
        target_user_id: uuid.UUID,
        new_password: str,
        method: RecoveryMethod,
        performed_by_user_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        # Password policy (IAM-013) applies to a recovery-set password
        # exactly as it does at registration - no relaxed rules for this
        # path. Raises WeakPasswordError on failure, propagated as-is.
        await validate_password_policy(new_password, breach_checker=self._breach_checker)
        await self._users.upsert_password_credential(
            target_user_id, password_hash=hash_password(new_password)
        )
        await self._sessions.revoke_all_for_user(target_user_id)
        await self._recovery_log.record(
            target_user_id=target_user_id,
            method=method,
            performed_by_user_id=performed_by_user_id,
            reason=reason,
        )

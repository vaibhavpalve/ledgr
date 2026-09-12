"""Ties user records and password credential storage together:
registration and password-based authentication. Session issuance
(api.auth.sessions.SessionService) is a separate, deliberate step - this
module answers "is this email/password combination valid," not "give this
user a session."
"""

from __future__ import annotations

from api.auth.breach_check import PasswordBreachChecker
from api.auth.models import User
from api.auth.passwords import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from api.auth.repository import UserRepository


class UserAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    """Deliberately generic: covers "no such user" and "wrong password"
    identically, at both the exception type and (via passwords.DUMMY_HASH)
    the timing level - SEC-008 requires error responses to never disclose
    whether an account exists.
    """


class AccountNotActiveError(Exception):
    """Raised only AFTER a correct password has been verified - the user
    already proved they hold valid credentials, so naming the actual
    account status here does not leak anything SEC-008 is concerned with.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"account status is {status!r}, not active")


class AuthenticationService:
    def __init__(self, users: UserRepository, breach_checker: PasswordBreachChecker) -> None:
        self._users = users
        self._breach_checker = breach_checker

    async def register_user(self, email: str, password: str) -> User:
        normalized_email = email.strip().lower()
        if await self._users.get_by_email(normalized_email) is not None:
            raise UserAlreadyExistsError(normalized_email)

        # Raises WeakPasswordError with specific reasons on failure -
        # length, and breach screening. No composition-rule or
        # rotation-related check exists here; see api.auth.passwords.
        await validate_password_policy(password, breach_checker=self._breach_checker)

        user = await self._users.create(normalized_email)
        await self._users.upsert_password_credential(user.id, password_hash=hash_password(password))
        return user

    async def authenticate_with_password(self, email: str, password: str) -> User:
        normalized_email = email.strip().lower()
        user = await self._users.get_by_email(normalized_email)

        if user is None:
            verify_password(password, DUMMY_HASH)  # timing-safety, see InvalidCredentialsError
            raise InvalidCredentialsError

        credential = await self._users.get_password_credential(user.id)
        if credential is None:
            verify_password(password, DUMMY_HASH)
            raise InvalidCredentialsError

        if not verify_password(password, credential.password_hash):
            raise InvalidCredentialsError

        if user.status != "active":
            raise AccountNotActiveError(user.status)

        if needs_rehash(credential.password_hash):
            await self._users.upsert_password_credential(
                user.id, password_hash=hash_password(password)
            )

        return user

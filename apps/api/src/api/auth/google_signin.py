"""Resolves a verified Google identity (from api.auth.google_oidc) to a
LEDGR user account, and links one to an existing account (IAM-010c).

The attack IAM-010c defends against: whoever controls a given email
address at Google today gets silently merged into an existing LEDGR
account that proved that same email through a *different* credential.
Controlling the mailbox (or a Google account claiming that email) is not
proof of controlling the LEDGR account - only the LEDGR account's own
credential is. So:

  - sign_in() looks up an existing link by Google `sub` first, never by
    email - a returning Google user is recognized safely, no linking
    decision needed;
  - if no link exists but a user with a matching email already exists,
    sign_in() returns GoogleSignInLinkRequired rather than linking - it
    performs NO account mutation in that case;
  - the ONLY way a GoogleSignInLinkRequired becomes an actual link is
    confirm_link_with_password, which re-authenticates the EXISTING
    account via its own password credential before linking anything. A
    caller cannot skip this by constructing its own proof or calling a
    lower-level method - there is no other public method on
    GoogleSignInService that performs a link against an existing account.
    (Linking a brand-new account during sign_in is not this case: there is
    no existing account to protect there, so no re-authentication makes
    sense to demand.)

tests/auth/test_google_account_linking.py exercises this directly,
including the attack case: an attacker who controls a verified Google
identity for a victim's email, and does not know the victim's password,
gains nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from api.auth.google_oidc import GoogleIdentity
from api.auth.models import User
from api.auth.repository import UserRepository
from api.auth.service import AuthenticationService


class GoogleIdentityRepository(Protocol):
    async def get_user_id_by_subject(self, subject: str) -> uuid.UUID | None: ...

    async def link(self, user_id: uuid.UUID, identity: GoogleIdentity) -> None: ...

    async def exists_for_user(self, user_id: uuid.UUID) -> bool: ...


class GoogleAccountLinkError(Exception):
    """Base class for anything preventing an account link."""


class LinkIdentityMismatchError(GoogleAccountLinkError):
    """The re-authenticated user does not match the account
    GoogleSignInLinkRequired named. confirm_link_with_password
    re-authenticates by the SAME email the challenge carries, so in normal
    operation this can only happen if the challenge object itself was
    stale or tampered with - checked explicitly anyway, as a second line
    of defense for a security-relevant decision, not merely assumed from
    the email match.
    """


@dataclass(frozen=True, slots=True)
class GoogleSignInLinkRequired:
    """No Google identity is linked yet, but a user with this email
    already exists via another sign-in method. IAM-010c: pass this to
    GoogleSignInService.confirm_link_with_password along with the
    existing account's password - there is no other way to turn this into
    a link.
    """

    existing_user_id: uuid.UUID
    google_identity: GoogleIdentity


class GoogleSignInService:
    def __init__(
        self,
        users: UserRepository,
        google_identities: GoogleIdentityRepository,
        authentication: AuthenticationService,
    ) -> None:
        self._users = users
        self._google_identities = google_identities
        self._authentication = authentication

    async def sign_in(self, identity: GoogleIdentity) -> User | GoogleSignInLinkRequired:
        linked_user_id = await self._google_identities.get_user_id_by_subject(identity.subject)
        if linked_user_id is not None:
            user = await self._users.get_by_id(linked_user_id)
            assert user is not None, "user_google_identity references a nonexistent user"
            return user

        existing_user = await self._users.get_by_email(identity.email)
        if existing_user is not None:
            # IAM-010c: no account mutation happens here - see module docstring.
            return GoogleSignInLinkRequired(
                existing_user_id=existing_user.id, google_identity=identity
            )

        new_user = await self._users.create(identity.email)
        await self._google_identities.link(new_user.id, identity)
        return new_user

    async def confirm_link_with_password(
        self, link_required: GoogleSignInLinkRequired, *, password: str
    ) -> User:
        """IAM-010c, enforced rather than merely documented: re-authenticates
        the EXISTING account via its password credential, using the same
        email the Google identity presented, before linking anything.

        Raises whatever api.auth.service.AuthenticationService.
        authenticate_with_password raises on failure (most notably
        InvalidCredentialsError for a wrong password, or an account with
        no password credential at all) - in every failure case, nothing is
        linked. There is no partial-success state.
        """
        user = await self._authentication.authenticate_with_password(
            link_required.google_identity.email, password
        )

        # Defense in depth: the lookup above is by the same email the
        # challenge carries, so this should be structurally unreachable -
        # checked anyway rather than trusted, since the consequence of
        # being wrong here is exactly the account takeover IAM-010c exists
        # to prevent.
        if user.id != link_required.existing_user_id:
            raise LinkIdentityMismatchError

        await self._google_identities.link(user.id, link_required.google_identity)
        return user

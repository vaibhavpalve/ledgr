"""IAM-010f: business continuity against losing the Google account.

"Loss of the Google account must not lock a user out of their books: any
user whose only method is Google is prompted to add a passkey or password
before they can post to the ledger."

Why this matters: LEDGR does not control account recovery for a
third-party identity provider. If Google sign-in is a user's only path
into their account and something goes wrong with it - lost access,
provider-side suspension, the user switching Google accounts - they would
otherwise be permanently locked out of financial records Dutch fiscal law
requires them to retain for years (CMP-001; PRD §10.4's 7-year retention).
This gate makes that scenario structurally impossible for records created
after this control exists: an account cannot start creating them while
Google-only in the first place.

This is scoped to ONE action - posting to the ledger - not every
authenticated request, unlike IAM-011's MFA requirement
(api.mfa_middleware, ADR-008). A Google-only account can still sign in,
browse, and read its books freely; the gate below fires only on the
specific action this requirement names. That is why enforcement here is a
FastAPI dependency - the same per-endpoint mechanism api.tenancy.
get_tenant_context and api.db.get_db_session already use - rather than
ASGI middleware: middleware is the right tool for a blanket, nearly-every-
request concern (see ADR-008 for why MFA is middleware); a dependency is
the right tool for gating one specific action, declared only on the route
that needs it. There is no ledger-posting endpoint in this codebase yet
(FR-GL is unbuilt) - require_ledger_posting_eligibility is the dependency
such a route will declare via Depends(...) once it exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.google_repository import SqlGoogleIdentityRepository
from api.auth.google_signin import GoogleIdentityRepository
from api.auth.passkeys import PasskeyRepository
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.repository import SqlUserRepository, UserRepository
from api.db import get_db_session
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context


class GoogleOnlyAccountError(Exception):
    """IAM-010f: this account's only sign-in method is Google. The caller
    must prompt the user to add a passkey or password before the action
    they were attempting can proceed.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        super().__init__(
            f"user {user_id} has no sign-in method other than Google and cannot post to the ledger"
        )


@dataclass(frozen=True, slots=True)
class SignInMethodStatus:
    has_password: bool
    has_google: bool
    has_passkey: bool

    @property
    def is_google_only(self) -> bool:
        """True only when Google is the SOLE method present. An account
        with Google alongside a password or passkey is fine. An account
        with NO methods at all is deliberately NOT flagged here - that is
        a different, structurally-unreachable-today failure mode (every
        real account is created with at least one method), not the one
        IAM-010f describes, and conflating the two would make this
        property answer a broader question than it is meant to.
        """
        return self.has_google and not self.has_password and not self.has_passkey


class SignInMethodChecker:
    """Combines the three sign-in-method sources into one status. Notably
    does NOT consult TOTP (api.auth.totp) - TOTP is a second factor
    (IAM-012), never a standalone sign-in method, so it is irrelevant to
    "which primary method(s) does this account have."
    """

    def __init__(
        self,
        users: UserRepository,
        google_identities: GoogleIdentityRepository,
        passkeys: PasskeyRepository,
    ) -> None:
        self._users = users
        self._google_identities = google_identities
        self._passkeys = passkeys

    async def check(self, user_id: uuid.UUID) -> SignInMethodStatus:
        password_credential = await self._users.get_password_credential(user_id)
        has_google = await self._google_identities.exists_for_user(user_id)
        registered_passkeys = await self._passkeys.list_for_user(user_id)

        return SignInMethodStatus(
            has_password=password_credential is not None,
            has_google=has_google,
            has_passkey=any(not p.is_revoked for p in registered_passkeys),
        )


async def ensure_can_post_to_ledger(checker: SignInMethodChecker, user_id: uuid.UUID) -> None:
    """The enforcement point itself: evaluated fresh on every call, never
    cached - an account that was fine yesterday (e.g. had a passkey) but
    revoked its only alternative method since must be caught today, not
    remembered as having passed a check that no longer holds.
    """
    status = await checker.check(user_id)
    if status.is_google_only:
        raise GoogleOnlyAccountError(user_id)


async def get_sign_in_method_checker(
    session: AsyncSession = Depends(get_db_session),
) -> SignInMethodChecker:
    """Real, SQL-backed checker for production routes. Tests override this
    dependency (FastAPI's own app.dependency_overrides) with a checker
    built from the in-memory repository fakes instead - no database
    involved in those tests at all.
    """
    return SignInMethodChecker(
        SqlUserRepository(session),
        SqlGoogleIdentityRepository(session),
        SqlPasskeyRepository(session),
    )


async def require_ledger_posting_eligibility(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    checker: SignInMethodChecker = Depends(get_sign_in_method_checker),
) -> None:
    """FastAPI dependency a future ledger-posting route declares via
    Depends(require_ledger_posting_eligibility). Raises HTTPException(403)
    with a specific, structured reason a future UI turns into the actual
    PROMPT IAM-010f calls for ("add a passkey or password"), not a generic
    access-denied message.
    """
    if tenant.user_id is None:
        # Structurally unreachable via the normal middleware chain, same
        # defensive reasoning as api.tenancy.get_tenant_context and
        # api.mfa_middleware's equivalent check.
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    try:
        await ensure_can_post_to_ledger(checker, tenant.user_id)
    except GoogleOnlyAccountError as exc:
        # FR-UX-007 / FR-LOC-001: IAM-010f asks for a prompt rather than a
        # denial, which makes this one of the few refusals in the product that
        # is really an instruction - and an instruction a person cannot read
        # is not one. The `reason` stays the stable token a client branches on
        # to render the actual "add a passkey" affordance.
        raise problem(
            request, 403, "errors.google_only_account", reason="google_only_account"
        ) from exc

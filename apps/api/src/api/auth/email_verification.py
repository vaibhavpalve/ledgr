"""IAM-010b: e-mail verification for accounts that did not arrive via Google.

    IAM-010b  Google sign-in requires a verified Google e-mail address; an
              unverified one is rejected. (For every other signup method,
              LEDGR verifies the address itself.)

ADR-054 shipped password signup with the account active immediately and
named this as the one gap it deliberately left open. This module closes it
without unwinding anything: a `users.email_verified_at` column (migration
0049), a single-use link token stored as an `auth_ceremony` of kind
`email_verification` (the same shape as the Google signup ticket), the mail
that carries it, and the gate.

--- What an unverified account can and cannot do ---

It can sign in, enrol MFA and complete onboarding: none of those create
records anybody else relies on, and refusing them would turn a delayed
e-mail into a locked-out customer. It cannot POST TO THE LEDGER. That is the
line IAM-010f already draws for Google-only accounts, and for the same
reason - a journal entry is a fiscal record with a seven-year life, and the
address on the account that made it should be one somebody actually holds.
`require_verified_email` is a FastAPI dependency in exactly
api.auth.account_continuity.require_ledger_posting_eligibility's shape,
declared on the routes that post (expense posting, invoice issue and
credit), rather than middleware: it gates one action, not every request.

--- Why the link is the ceremony id ---

`auth_ceremony.id` is a gen_random_uuid() - 122 bits from Postgres' CSPRNG
- and the Google signup ticket already uses it as a bearer secret (ADR-054
addendum). Hashing it would protect against a database read handing out
live links, which is a real property, but the same read hands out live
Google `state` values and passkey challenges today; the ceremony table's
posture is "short-lived, single-use, superseded by the next issue", and this
kind follows it rather than inventing a second one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.ceremony import EMAIL_VERIFICATION_TTL, CeremonyNotFoundError, CeremonyRepository
from api.auth.repository import SqlUserRepository, UserRepository
from api.config import settings
from api.db import get_db_session
from api.i18n.catalogue import translate
from api.i18n.http import problem
from api.i18n.language import Language
from api.mail.sender import EmailMessage, EmailOutcome, EmailSender
from api.tenancy import TenantContext, get_tenant_context

logger = logging.getLogger("api.auth.email_verification")


class EmailVerificationError(Exception):
    """Base class for everything that can go wrong verifying an address."""


class VerificationLinkInvalidError(EmailVerificationError):
    """Expired, already used, or never issued - one exception for all
    three, for the same reason api.auth.ceremony.CeremonyNotFoundError is
    one: the person who clicked it should request a new link either way,
    and somebody probing tokens learns nothing from the difference.
    """


class EmailAlreadyVerifiedError(EmailVerificationError):
    """A resend was requested for an address that needs nothing."""


class UnknownUserError(EmailVerificationError):
    """A ceremony named a user that does not exist. Structurally unreachable
    (users are never deleted - 0003) and refused rather than assumed, the
    posture api.auth.routes.signup_google takes for the same case.
    """


def verification_link(token: uuid.UUID) -> str:
    """Where the mail sends the person. The web app owns this path and turns
    the query parameter into POST /v1/auth/verify-email.
    """
    return f"{settings.app_base_url.rstrip('/')}/verify-email?token={token}"


def build_verification_message(*, to: str, token: uuid.UUID, language: Language) -> EmailMessage:
    """FR-LOC-001: every e-mail exists in both languages, from the catalogue.
    Plain text, the same posture api.mail.sender takes for invoices (no HTML,
    no tracking surface).
    """
    hours = int(EMAIL_VERIFICATION_TTL.total_seconds() // 3600)
    body = "\n".join(
        [
            translate("auth.email_verification.greeting", language),
            "",
            translate("auth.email_verification.intro", language, product=settings.webauthn_rp_name),
            "",
            verification_link(token),
            "",
            translate("auth.email_verification.expiry", language, hours=hours),
            translate(
                "auth.email_verification.ignore", language, product=settings.webauthn_rp_name
            ),
            "",
            translate("auth.email_verification.signoff", language),
            settings.webauthn_rp_name,
        ]
    )
    return EmailMessage(
        to=to,
        subject=translate(
            "auth.email_verification.subject", language, product=settings.webauthn_rp_name
        ),
        body=body,
        from_address=settings.email_from_address,
        from_name=settings.webauthn_rp_name,
    )


@dataclass(frozen=True, slots=True)
class IssuedVerification:
    token: uuid.UUID
    outcome: EmailOutcome


class EmailVerificationService:
    def __init__(
        self, users: UserRepository, ceremonies: CeremonyRepository, sender: EmailSender
    ) -> None:
        self._users = users
        self._ceremonies = ceremonies
        self._sender = sender

    async def issue(
        self, *, user_id: uuid.UUID, email: str, language: Language
    ) -> IssuedVerification:
        """Issues a fresh token (retiring any earlier one) and hands the mail
        to the sender. The sender's outcome is returned rather than raised:
        a mail provider being down is not the signup's failure, and the
        person can ask for another link.

        With the collecting provider (local development) the link is also
        written to the log at INFO, so a developer without the dev outbox
        route still has it in front of them.
        """
        token = await self._ceremonies.create_email_verification_ceremony(user_id=user_id)
        outcome = await self._sender.send(
            build_verification_message(to=email, token=token, language=language)
        )
        if outcome.provider == "collecting":
            logger.info(
                "email_verification_link_collected",
                extra={"email": email, "link": verification_link(token)},
            )
        elif not outcome.accepted:
            logger.warning(
                "email_verification_send_failed",
                extra={"email": email, "provider": outcome.provider, "detail": outcome.detail},
            )
        return IssuedVerification(token=token, outcome=outcome)

    async def verify(self, token: uuid.UUID) -> uuid.UUID:
        """Consumes the token and stamps the user. Returns the user id so the
        route can record the event against the right organization. A second
        click on the same link is refused like an expired one; the address
        stays verified from the first.
        """
        try:
            user_id = await self._ceremonies.consume_email_verification_ceremony(token)
        except CeremonyNotFoundError as exc:
            raise VerificationLinkInvalidError from exc
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UnknownUserError
        await self._users.mark_email_verified(user_id, at=datetime.now(UTC))
        return user_id


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class EmailVerificationChecker:
    """Evaluated fresh on every call, never cached - the same reason
    api.auth.account_continuity.ensure_can_post_to_ledger gives: the answer
    can change between two requests and the second one must see it.
    """

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def is_verified(self, user_id: uuid.UUID) -> bool:
        user = await self._users.get_by_id(user_id)
        return user is not None and user.email_verified


async def get_email_verification_checker(
    session: AsyncSession = Depends(get_db_session),
) -> EmailVerificationChecker:
    """Real, SQL-backed. Tests override this dependency with a checker over
    the in-memory user repository - see
    tests/test_email_verification_gate.py.
    """
    return EmailVerificationChecker(SqlUserRepository(session))


async def require_verified_email(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    checker: EmailVerificationChecker = Depends(get_email_verification_checker),
) -> None:
    """Declare via Depends(require_verified_email) on a route that posts to
    the ledger. 403 with reason `email_not_verified`, which the client turns
    into the persistent "confirm your e-mail" banner's call to action rather
    than a generic refusal.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    if not await checker.is_verified(tenant.user_id):
        raise problem(request, 403, "errors.email_not_verified", reason="email_not_verified")

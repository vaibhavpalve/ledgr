"""IAM-010/FR-MDL-001/FR-ONB-001a: the HTTP surface every prior auth ADR
(005, 006, 007, 008) deliberately left unbuilt - "no login UI yet" appears
in every one of them. See docs/decisions/ADR-054-signup-and-login.md.

--- The shape every flow shares: a session that exists before MFA is satisfied ---

Signing up or logging in with a password, a passkey, or Google all produce
the SAME thing first: a real, stateful api.auth.sessions.Session
(mfa_verified=False) and a JWT restating its claims for
api.tenancy.TenantContextMiddleware to read (api.auth.tokens). That token is
real and tenant-scoped - it is not a "logged out" state - but
api.mfa_middleware.MfaEnforcementMiddleware will refuse everything with it
except the small set of paths in MFA_EXEMPT_PATHS below, until one of the
MFA endpoints re-mints it with mfa_verified=True. This is deliberate: MFA
enrolment has to happen INSIDE an authenticated context (it enrols a factor
for a specific user), but must not itself require the thing it is
establishing.

Passkey sign-in is the one path that skips this intermediate state: IAM-012
already counts a passkey as a full MFA factor, so successfully
authenticating with one is treated as having satisfied MFA in the same
step, not merely as identifying the user (see login_passkey_finish).

--- Email verification (IAM-010b) ---

ADR-054 marked a password signup's account active immediately and named
verification as the one gap it left open. Closed by ADR-060: `signup()`
issues a single-use link (api.auth.email_verification) after its own
transaction commits, `verify_email` consumes it with no session at all (the
link is opened wherever the mail is read - api.tenancy.EXEMPT_PATHS), and
`resend_verification_email` is the signed-in account holder asking again.
An unverified account signs in, enrols MFA and onboards as before; only
posting to the ledger is gated (api.auth.email_verification.
require_verified_email). Google-created accounts are verified at creation -
the identity provider already did it (api.auth.google_signin).

--- Google sign-in creating a brand-new account ---

`GoogleSignInService.sign_in` creates a bare `User` row and links the
identity the moment a Google sign-in matches neither an existing linked
subject nor an existing email - see that module's own docstring. What this
module adds is the rest of FR-MDL-001: `login_google_callback` responds
`{"status": "signup_required", "ticket": ..., "email": ...}` for that bare
user rather than 403-refusing it, and `signup_google` (below) is the
follow-up screen's endpoint - it asks the same one question `signup()`
asks, consumes the ticket to resolve the already-created user, and calls
`SignupService.provision_organization` (the part of `signup()` that runs
after a user exists) to create the organization and grant founding Owner.
The ticket is a `google_signup` `auth_ceremony` row (migration 0047),
because Google's own OAuth redirect has no room to carry
account_model/organization_name/kvk_number through it - collecting them
happens in a genuinely separate request, after the identity is already
resolved and linked.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_registration_credential_json,
)

from api.audit.log import ActorType, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.audit.trail import AuditTrail
from api.auth.breach_check import build_breach_checker
from api.auth.ceremony import CeremonyNotFoundError, SqlCeremonyRepository
from api.auth.email_verification import (
    EmailAlreadyVerifiedError,
    EmailVerificationService,
    UnknownUserError,
    VerificationLinkInvalidError,
)
from api.auth.google_oidc import (
    InvalidGoogleIdentityError,
    UnverifiedGoogleEmailError,
    build_google_oidc_client,
)
from api.auth.google_repository import SqlGoogleIdentityRepository
from api.auth.google_signin import (
    GoogleAccountLinkError,
    GoogleSignInLinkRequired,
    GoogleSignInService,
)
from api.auth.mfa import MfaEnrollmentChecker, google_asserts_second_factor
from api.auth.models import User
from api.auth.passkeys import PasskeyError, PasskeyRepository, WebAuthnService
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.passwords import WeakPasswordError
from api.auth.rate_limiting import AuthRateLimiter, LoggingAnomalyAlerter
from api.auth.rate_limiting_repository import SqlAuthAttemptRepository
from api.auth.repository import SqlSessionRepository, SqlUserRepository
from api.auth.service import (
    AccountNotActiveError,
    AuthenticationService,
    InvalidCredentialsError,
    UserAlreadyExistsError,
)
from api.auth.sessions import SessionService
from api.auth.signup import OwnerRoleMissingError, SignupService
from api.auth.tokens import issue_access_token
from api.auth.totp import (
    InvalidTotpCodeError,
    TotpAlreadyEnrolledError,
    TotpNotEnrolledError,
    TotpService,
)
from api.auth.totp_repository import SqlTotpRepository
from api.config import settings
from api.crypto.kms import build_kms
from api.db import engine, get_bootstrap_db_session, get_db_session
from api.i18n.http import message, problem, request_language
from api.i18n.language import Language, parse_language
from api.mail.outbox import get_email_sender
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/auth"

# For the verification mail's own transaction after signup commits - the
# same "a session cannot be shared across transactions" reason
# api.mfa_middleware builds its own.
_post_commit_session_factory = async_sessionmaker(engine, expire_on_commit=False)

# Reachable with a valid, tenant-scoped token whose mfa_verified is still
# False - the enrolment and step-up-verification endpoints themselves, plus
# logout (someone stuck mid-enrolment must still be able to leave). Kept
# separate from api.tenancy.EXEMPT_PATHS (signup/login/passkey-login/Google,
# which need no tenant context at all - see that module's own comment on why
# the dependency runs in that direction and not this one): these need a
# real user id, so they must still pass TenantContextMiddleware - they are
# exempt from MfaEnforcementMiddleware alone.
MFA_EXEMPT_PATHS = frozenset(
    {
        f"{_BASE}/mfa/totp/enroll/begin",
        f"{_BASE}/mfa/totp/enroll/confirm",
        f"{_BASE}/mfa/totp/verify",
        f"{_BASE}/mfa/passkey/enroll/begin",
        f"{_BASE}/mfa/passkey/enroll/finish",
        f"{_BASE}/mfa/passkey/verify/begin",
        f"{_BASE}/mfa/passkey/verify/finish",
        f"{_BASE}/logout",
    }
)


def register(app: FastAPI) -> None:
    """Added directly to app.routes rather than via include_router - see
    api.documents.routes.register's docstring for why: this FastAPI
    version hides an included router's routes behind a wrapper the
    coverage checks (and, here, EXEMPT_PATHS matching by exact path string)
    cannot see through.
    """
    app.add_api_route(f"{_BASE}/signup", signup, methods=["POST"], name="auth_signup")
    app.add_api_route(f"{_BASE}/login", login, methods=["POST"], name="auth_login")
    app.add_api_route(f"{_BASE}/logout", logout, methods=["POST"], name="auth_logout")

    app.add_api_route(
        f"{_BASE}/mfa/totp/enroll/begin",
        mfa_totp_enroll_begin,
        methods=["POST"],
        name="mfa_totp_enroll_begin",
    )
    app.add_api_route(
        f"{_BASE}/mfa/totp/enroll/confirm",
        mfa_totp_enroll_confirm,
        methods=["POST"],
        name="mfa_totp_enroll_confirm",
    )
    app.add_api_route(
        f"{_BASE}/mfa/totp/verify", mfa_totp_verify, methods=["POST"], name="mfa_totp_verify"
    )

    app.add_api_route(
        f"{_BASE}/mfa/passkey/enroll/begin",
        mfa_passkey_enroll_begin,
        methods=["POST"],
        name="mfa_passkey_enroll_begin",
    )
    app.add_api_route(
        f"{_BASE}/mfa/passkey/enroll/finish",
        mfa_passkey_enroll_finish,
        methods=["POST"],
        name="mfa_passkey_enroll_finish",
    )
    app.add_api_route(
        f"{_BASE}/mfa/passkey/verify/begin",
        mfa_passkey_verify_begin,
        methods=["POST"],
        name="mfa_passkey_verify_begin",
    )
    app.add_api_route(
        f"{_BASE}/mfa/passkey/verify/finish",
        mfa_passkey_verify_finish,
        methods=["POST"],
        name="mfa_passkey_verify_finish",
    )

    app.add_api_route(
        f"{_BASE}/login/passkey/begin",
        login_passkey_begin,
        methods=["POST"],
        name="login_passkey_begin",
    )
    app.add_api_route(
        f"{_BASE}/login/passkey/finish",
        login_passkey_finish,
        methods=["POST"],
        name="login_passkey_finish",
    )

    app.add_api_route(
        f"{_BASE}/login/google/start",
        login_google_start,
        methods=["POST"],
        name="login_google_start",
    )
    app.add_api_route(
        f"{_BASE}/login/google/callback",
        login_google_callback,
        methods=["POST"],
        name="login_google_callback",
    )
    app.add_api_route(
        f"{_BASE}/signup/google",
        signup_google,
        methods=["POST"],
        name="signup_google",
    )

    app.add_api_route(f"{_BASE}/verify-email", verify_email, methods=["POST"], name="verify_email")
    app.add_api_route(
        f"{_BASE}/verify-email/resend",
        resend_verification_email,
        methods=["POST"],
        name="resend_verification_email",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _webauthn_service(repository: PasskeyRepository) -> WebAuthnService:
    return WebAuthnService(
        repository,
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        expected_origin=settings.webauthn_origin,
    )


def _session_service_for(session: AsyncSession) -> SessionService:
    return SessionService(
        SqlSessionRepository(session),
        max_lifetime_hours=settings.session_max_lifetime_hours,
        idle_timeout_minutes_privileged=settings.session_idle_timeout_minutes_privileged,
    )


def _audit_trail_for(session: AsyncSession) -> AuditTrail:
    return AuditTrail(AuditLog(SqlAuditRepository(session)))


def _email_verification_for(session: AsyncSession) -> EmailVerificationService:
    return EmailVerificationService(
        SqlUserRepository(session), SqlCeremonyRepository(session), get_email_sender()
    )


async def _record_authentication_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    source_ip: str | None = None,
    request: Request | None = None,
    set_org_context: bool = False,
) -> None:
    """IAM-090's "authentication events", recorded from the service/handler
    layer rather than declared on the route - see api.audit.trail's
    docstring for why: every route in this module runs before any
    permission can be evaluated (it IS how a permission-bearing identity
    gets established), so there is no require_permission(audit=...) to hang
    a declaration on. tests/test_audit_coverage.py's AUDIT_EXEMPT_PATHS
    entry for these paths names this function as the actual recording
    mechanism.

    `set_org_context` is needed for the handlers using
    get_bootstrap_db_session (login, the passkey/Google sign-in paths):
    that session never had app.current_org_id set, unlike get_db_session's
    sessions (logout, MFA enrol/verify), which already carry it. Signup is
    its own case - api.auth.signup.SignupService already sets it in the
    same transaction once the organization exists - so it passes False here.
    """
    if set_org_context:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )
    await _audit_trail_for(session).authentication(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.USER,
        action=action,
        outcome=AuditOutcome.SUCCESS,
        source_ip=source_ip,
        user_agent=request.headers.get("user-agent") if request else None,
    )


async def _home_organization_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The organization this user is a member of (FR-MDL-001's founding
    grant, or one they were invited into) - as opposed to an
    administration they may separately hold a firm-staff grant on
    (IAM-107, scoped to the administration, not the organization). A user
    is a member of exactly one organization in the account-model sense;
    see docs/decisions/ADR-054-signup-and-login.md for why this is the
    right question to ask at login rather than "every scope this user can
    reach."

    Every caller of this helper (login, login_passkey_finish,
    login_google_callback) runs on api.db.get_bootstrap_db_session -
    deliberately the session with no app.current_org_id set, because
    finding that value is this function's whole job. role_assignment's own
    RLS policy reads `scope_id = app.current_org_id()`, which is `scope_id
    = NULL` on that session and therefore false for every row, for every
    caller, always - a plain SELECT here could never see the row it is
    looking for, no matter whose it was. See
    0048_login_home_organization_lookup.sql for the bug this was (found by
    running the login flow against a real Postgres for the first time) and
    why a SECURITY DEFINER function, not a raw query, is the fix: the same
    RLS-bootstrap problem 0001_tenancy_core.sql already solved for
    signup's writes, here on the read side.
    """
    result = await session.execute(
        text("SELECT app.user_home_organization_id(:user_id)"),
        {"user_id": str(user_id)},
    )
    return result.scalar_one_or_none()


async def _enrollment_status(session: AsyncSession, user_id: uuid.UUID) -> dict[str, bool]:
    checker = MfaEnrollmentChecker(SqlPasskeyRepository(session), SqlTotpRepository(session))
    status = await checker.check(user_id)
    return {"has_passkey": status.has_passkey, "has_totp": status.has_totp}


def _auth_response(
    *, token: str, mfa_verified: bool, enrollment: dict[str, bool] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": token,
        "token_type": "bearer",
        "mfa_verified": mfa_verified,
    }
    if not mfa_verified:
        body["mfa"] = enrollment or {"has_passkey": False, "has_totp": False}
    return body


# ---------------------------------------------------------------------------
# Signup - FR-MDL-001, FR-ONB-001a/001b
# ---------------------------------------------------------------------------


class SignupBody(BaseModel):
    account_model: Literal["self_managed", "firm"]
    organization_name: str
    kvk_number: str | None = None
    email: str
    password: str


async def signup(
    request: Request,
    body: SignupBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    breach_checker = build_breach_checker(settings.breach_checker_provider)
    service = SignupService(session, breach_checker)

    try:
        result = await service.signup(
            account_model=body.account_model,
            organization_name=body.organization_name,
            kvk_number=body.kvk_number,
            email=body.email,
            password=body.password,
        )
    except UserAlreadyExistsError as exc:
        raise problem(
            request, 409, "errors.email_already_registered", reason="email_already_registered"
        ) from exc
    except OwnerRoleMissingError as exc:
        raise problem(
            request, 500, "errors.signup_unavailable", reason="owner_role_missing"
        ) from exc
    except WeakPasswordError as exc:
        raise problem(
            request,
            422,
            "errors.weak_password",
            reason="weak_password",
            weaknesses=list(exc.reasons),
        ) from exc

    session_service = _session_service_for(session)
    new_session, _raw_token = await session_service.issue_session(
        result.user.id, privileged=True, mfa_verified=False
    )
    token = issue_access_token(
        user_id=result.user.id,
        organization_id=result.organization_id,
        session_id=new_session.id,
        mfa_verified=False,
        expires_at=new_session.expires_at,
    )
    await _record_authentication_event(
        session,
        organization_id=result.organization_id,
        actor_user_id=result.user.id,
        action="signup",
        source_ip=request.client.host if request.client else None,
        request=request,
    )
    await session.commit()

    # IAM-010b, after the commit and in its own transaction: an e-mail is an
    # irreversible act by somebody else's server, so it must not go out for
    # a signup that rolled back, and a mail provider being down must not
    # undo a signup that succeeded (the person can ask for a new link).
    # The language is the signup screen's (Accept-Language): the one
    # language this person is known to read, before any stored preference.
    async with _post_commit_session_factory() as mail_session, mail_session.begin():
        await _email_verification_for(mail_session).issue(
            user_id=result.user.id, email=result.user.email, language=request_language(request)
        )
    return _auth_response(token=token, mfa_verified=False)


# ---------------------------------------------------------------------------
# E-mail verification - IAM-010b
# ---------------------------------------------------------------------------


class VerifyEmailBody(BaseModel):
    token: uuid.UUID


async def verify_email(
    request: Request,
    body: VerifyEmailBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    """Consumes the link's token and stamps the address. No bearer token:
    the link is opened wherever the mail is read, and the single-use token
    is the proof (api.tenancy.EXEMPT_PATHS). A second click is refused
    like an expired link; the address stays verified from the first.
    """
    try:
        user_id = await _email_verification_for(session).verify(body.token)
    except (VerificationLinkInvalidError, UnknownUserError) as exc:
        raise problem(
            request, 410, "errors.verification_link_invalid", reason="verification_link_invalid"
        ) from exc

    # Recorded against the user's home organization (IAM-090); a bare Google
    # user with no organization yet cannot reach this path (they are already
    # verified), but a missing home is answered with no audit entry rather
    # than a failed verification.
    organization_id = await _home_organization_id(session, user_id)
    if organization_id is not None:
        await _record_authentication_event(
            session,
            organization_id=organization_id,
            actor_user_id=user_id,
            action="email_verify",
            source_ip=request.client.host if request.client else None,
            request=request,
            set_org_context=True,
        )
    await session.commit()
    return {"status": "verified"}


async def resend_verification_email(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """A new link for the signed-in caller's own address, retiring the old
    one. Rate-limited per account (IAM-019's sliding window) so a stuck
    "resend" button cannot turn LEDGR into a mail cannon aimed at its own
    customer. The language is the stored preference when there is one
    (this is a server-initiated mail in the api.i18n.language sense), else
    the request's.
    """
    user_id = await _require_user(request, tenant)
    user = await SqlUserRepository(session).get_by_id(user_id)
    assert user is not None, "a verified tenant context named a nonexistent user"
    if user.email_verified:
        raise problem(
            request, 409, "errors.email_already_verified", reason="email_already_verified"
        )

    rate_limiter = AuthRateLimiter(SqlAuthAttemptRepository(session), LoggingAnomalyAlerter())
    decision = await rate_limiter.check(endpoint="verify_email_resend", account_key=user.email)
    if not decision.allowed:
        raise problem(
            request,
            429,
            "errors.rate_limited",
            reason=decision.reason,
            retry_after_seconds=decision.retry_after_seconds,
        )
    await rate_limiter.record_attempt(
        endpoint="verify_email_resend",
        account_key=user.email,
        source_ip=request.client.host if request.client else None,
        outcome="success",
    )

    stored = await session.execute(
        text("SELECT language FROM users WHERE id = :user_id"), {"user_id": str(user_id)}
    )
    language: Language = parse_language(stored.scalar_one_or_none()) or request_language(request)
    try:
        issued = await _email_verification_for(session).issue(
            user_id=user_id, email=user.email, language=language
        )
    except EmailAlreadyVerifiedError as exc:  # pragma: no cover - checked above
        raise problem(
            request, 409, "errors.email_already_verified", reason="email_already_verified"
        ) from exc

    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=user_id,
        action="email_verification_resend",
        source_ip=request.client.host if request.client else None,
        request=request,
    )
    await session.commit()
    return {"status": "sent", "email": user.email, "accepted": issued.outcome.accepted}


# ---------------------------------------------------------------------------
# Login (password) - IAM-010, IAM-019
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    email: str
    password: str


async def login(
    request: Request,
    body: LoginBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    rate_limiter = AuthRateLimiter(SqlAuthAttemptRepository(session), LoggingAnomalyAlerter())
    account_key = body.email.strip().lower()
    source_ip = request.client.host if request.client else None

    decision = await rate_limiter.check(endpoint="login", account_key=account_key)
    if not decision.allowed:
        raise problem(
            request,
            429,
            "errors.rate_limited",
            reason=decision.reason,
            retry_after_seconds=decision.retry_after_seconds,
        )

    breach_checker = build_breach_checker(settings.breach_checker_provider)
    authentication = AuthenticationService(SqlUserRepository(session), breach_checker)

    try:
        user = await authentication.authenticate_with_password(body.email, body.password)
    except (InvalidCredentialsError, AccountNotActiveError) as exc:
        await rate_limiter.record_attempt(
            endpoint="login", account_key=account_key, source_ip=source_ip, outcome="failure"
        )
        await session.commit()
        raise problem(
            request, 401, "errors.invalid_credentials", reason="invalid_credentials"
        ) from exc

    await rate_limiter.record_attempt(
        endpoint="login", account_key=account_key, source_ip=source_ip, outcome="success"
    )

    organization_id = await _home_organization_id(session, user.id)
    if organization_id is None:
        await session.commit()
        raise problem(request, 403, "errors.no_organization", reason="no_organization")

    token, mfa_verified = await _issue_post_authentication_token(
        session, user=user, organization_id=organization_id, source_ip=source_ip
    )
    await _record_authentication_event(
        session,
        organization_id=organization_id,
        actor_user_id=user.id,
        action="login",
        source_ip=source_ip,
        request=request,
        set_org_context=True,
    )
    # A pure read with no dependency on anything just written in this
    # transaction, so it runs BEFORE the commit below rather than after.
    # get_bootstrap_db_session opens this session as `session.begin()`
    # (api.db) - calling session.commit() inside that block ends the
    # transaction the context manager is holding open, and any further
    # query on the same session then fails with SQLAlchemy's own
    # "Can't operate on closed transaction inside context manager" rather
    # than the query's own error. This success path was unreachable before
    # 0048_login_home_organization_lookup.sql (organization_id was always
    # None, so login() always took the 403 branch above and commit() was
    # always the last thing that happened) - found the same way that bug
    # was, by running login all the way through against a real Postgres for
    # the first time.
    enrollment = await _enrollment_status(session, user.id) if not mfa_verified else None
    await session.commit()
    return _auth_response(token=token, mfa_verified=mfa_verified, enrollment=enrollment)


async def _issue_post_authentication_token(
    session: AsyncSession,
    *,
    user: User,
    organization_id: uuid.UUID,
    source_ip: str | None,
    mfa_verified: bool = False,
) -> tuple[str, bool]:
    session_service = _session_service_for(session)
    new_session, _raw_token = await session_service.issue_session(
        user.id, privileged=True, mfa_verified=mfa_verified, ip_address=source_ip
    )
    token = issue_access_token(
        user_id=user.id,
        organization_id=organization_id,
        session_id=new_session.id,
        mfa_verified=mfa_verified,
        expires_at=new_session.expires_at,
    )
    return token, mfa_verified


async def logout(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if tenant.session_id is None:
        raise problem(request, 400, "errors.no_session", reason="no_active_session")
    await _session_service_for(session).revoke_session(tenant.session_id)
    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=tenant.user_id,
        action="logout",
        source_ip=request.client.host if request.client else None,
        request=request,
    )
    await session.commit()
    return {"status": "logged_out"}


# ---------------------------------------------------------------------------
# MFA enrolment - IAM-011, IAM-012
# ---------------------------------------------------------------------------


async def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None or tenant.session_id is None:
        raise problem(request, 401, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


async def _reissue_verified_token(
    session: AsyncSession, *, tenant: TenantContext, user_id: uuid.UUID
) -> str:
    assert tenant.session_id is not None
    session_service = _session_service_for(session)
    await session_service.record_mfa_verification(tenant.session_id)
    validated = await session_service.list_sessions(user_id)
    current = next(s for s in validated if s.id == tenant.session_id)
    return issue_access_token(
        user_id=user_id,
        organization_id=tenant.organization_id,
        session_id=tenant.session_id,
        mfa_verified=True,
        expires_at=current.expires_at,
    )


class TotpConfirmBody(BaseModel):
    secret: str
    code: str


async def mfa_totp_enroll_begin(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    user = await SqlUserRepository(session).get_by_id(user_id)
    assert user is not None, "a verified tenant context named a nonexistent user"
    service = TotpService(SqlTotpRepository(session), build_kms())
    enrollment = service.begin_enrollment(account_name=user.email)
    return {"secret": enrollment.secret, "provisioning_uri": enrollment.provisioning_uri}


async def mfa_totp_enroll_confirm(
    request: Request,
    body: TotpConfirmBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    service = TotpService(SqlTotpRepository(session), build_kms())
    try:
        await service.confirm_enrollment(user_id=user_id, secret=body.secret, code=body.code)
    except TotpAlreadyEnrolledError as exc:
        raise problem(
            request, 409, "errors.totp_already_enrolled", reason="already_enrolled"
        ) from exc
    except InvalidTotpCodeError as exc:
        raise problem(request, 422, "errors.invalid_totp_code", reason="invalid_code") from exc

    token = await _reissue_verified_token(session, tenant=tenant, user_id=user_id)
    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=user_id,
        action="mfa_totp_enroll",
        request=request,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=True)


class TotpVerifyBody(BaseModel):
    code: str


async def mfa_totp_verify(
    request: Request,
    body: TotpVerifyBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    service = TotpService(SqlTotpRepository(session), build_kms())
    try:
        await service.verify_code(user_id=user_id, code=body.code)
    except TotpNotEnrolledError as exc:
        raise problem(request, 409, "errors.totp_not_enrolled", reason="not_enrolled") from exc
    except InvalidTotpCodeError as exc:
        raise problem(request, 422, "errors.invalid_totp_code", reason="invalid_code") from exc

    token = await _reissue_verified_token(session, tenant=tenant, user_id=user_id)
    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=user_id,
        action="mfa_verify_totp",
        request=request,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=True)


class PasskeyFinishBody(BaseModel):
    ceremony_id: uuid.UUID
    name: str | None = None
    credential: dict[str, Any]


async def mfa_passkey_enroll_begin(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    user = await SqlUserRepository(session).get_by_id(user_id)
    assert user is not None, "a verified tenant context named a nonexistent user"
    passkeys = SqlPasskeyRepository(session)
    existing = await passkeys.list_for_user(user_id)
    challenge = _webauthn_service(passkeys).begin_registration(
        user_id=user_id, user_email=user.email, existing_passkeys=existing
    )
    ceremony_id = await SqlCeremonyRepository(session).create_passkey_ceremony(
        kind="passkey_registration", challenge=challenge.challenge, user_id=user_id
    )
    await session.commit()
    return {"ceremony_id": str(ceremony_id), "options": challenge.options_json}


async def mfa_passkey_enroll_finish(
    request: Request,
    body: PasskeyFinishBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    try:
        ceremony = await SqlCeremonyRepository(session).consume_passkey_ceremony(
            body.ceremony_id, kind="passkey_registration"
        )
    except CeremonyNotFoundError as exc:
        raise problem(
            request, 410, "errors.ceremony_not_found", reason="ceremony_not_found"
        ) from exc
    if ceremony.user_id != user_id:
        raise problem(request, 403, "errors.ceremony_not_found", reason="ceremony_wrong_user")

    passkeys = SqlPasskeyRepository(session)
    try:
        credential = parse_registration_credential_json(body.credential)
        await _webauthn_service(passkeys).complete_registration(
            user_id=user_id,
            name=(body.name or "Passkey").strip() or "Passkey",
            challenge=ceremony.challenge,
            credential=credential,
        )
    except PasskeyError as exc:
        raise problem(
            request, 422, "errors.passkey_registration_failed", reason="passkey_registration_failed"
        ) from exc

    token = await _reissue_verified_token(session, tenant=tenant, user_id=user_id)
    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=user_id,
        action="mfa_passkey_enroll",
        request=request,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=True)


async def mfa_passkey_verify_begin(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    passkeys = SqlPasskeyRepository(session)
    existing = await passkeys.list_for_user(user_id)
    challenge = _webauthn_service(passkeys).begin_authentication(allow_passkeys=existing)
    ceremony_id = await SqlCeremonyRepository(session).create_passkey_ceremony(
        kind="passkey_authentication", challenge=challenge.challenge, user_id=user_id
    )
    await session.commit()
    return {"ceremony_id": str(ceremony_id), "options": challenge.options_json}


async def mfa_passkey_verify_finish(
    request: Request,
    body: PasskeyFinishBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    try:
        ceremony = await SqlCeremonyRepository(session).consume_passkey_ceremony(
            body.ceremony_id, kind="passkey_authentication"
        )
    except CeremonyNotFoundError as exc:
        raise problem(
            request, 410, "errors.ceremony_not_found", reason="ceremony_not_found"
        ) from exc
    if ceremony.user_id != user_id:
        raise problem(request, 403, "errors.ceremony_not_found", reason="ceremony_wrong_user")

    try:
        credential = parse_authentication_credential_json(body.credential)
        passkey = await _webauthn_service(SqlPasskeyRepository(session)).complete_authentication(
            challenge=ceremony.challenge, credential=credential
        )
    except PasskeyError as exc:
        raise problem(
            request,
            422,
            "errors.passkey_authentication_failed",
            reason="passkey_authentication_failed",
        ) from exc
    if passkey.user_id != user_id:
        raise problem(request, 403, "errors.ceremony_not_found", reason="ceremony_wrong_user")

    token = await _reissue_verified_token(session, tenant=tenant, user_id=user_id)
    await _record_authentication_event(
        session,
        organization_id=tenant.organization_id,
        actor_user_id=user_id,
        action="mfa_verify_passkey",
        request=request,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=True)


# ---------------------------------------------------------------------------
# Passkey sign-in - IAM-010, IAM-012 (a passkey satisfies MFA in one step)
# ---------------------------------------------------------------------------


async def login_passkey_begin(
    request: Request,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    challenge = _webauthn_service(SqlPasskeyRepository(session)).begin_authentication()
    ceremony_id = await SqlCeremonyRepository(session).create_passkey_ceremony(
        kind="passkey_authentication", challenge=challenge.challenge, user_id=None
    )
    await session.commit()
    return {"ceremony_id": str(ceremony_id), "options": challenge.options_json}


class LoginPasskeyFinishBody(BaseModel):
    ceremony_id: uuid.UUID
    credential: dict[str, Any]


async def login_passkey_finish(
    request: Request,
    body: LoginPasskeyFinishBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    try:
        ceremony = await SqlCeremonyRepository(session).consume_passkey_ceremony(
            body.ceremony_id, kind="passkey_authentication"
        )
    except CeremonyNotFoundError as exc:
        raise problem(
            request, 410, "errors.ceremony_not_found", reason="ceremony_not_found"
        ) from exc

    try:
        credential = parse_authentication_credential_json(body.credential)
        passkey = await _webauthn_service(SqlPasskeyRepository(session)).complete_authentication(
            challenge=ceremony.challenge, credential=credential
        )
    except PasskeyError as exc:
        raise problem(
            request,
            401,
            "errors.passkey_authentication_failed",
            reason="passkey_authentication_failed",
        ) from exc

    user = await SqlUserRepository(session).get_by_id(passkey.user_id)
    if user is None or user.status != "active":
        raise problem(request, 401, "errors.invalid_credentials", reason="invalid_credentials")

    organization_id = await _home_organization_id(session, user.id)
    if organization_id is None:
        raise problem(request, 403, "errors.no_organization", reason="no_organization")

    source_ip = request.client.host if request.client else None
    token, _ = await _issue_post_authentication_token(
        session, user=user, organization_id=organization_id, source_ip=source_ip, mfa_verified=True
    )
    await _record_authentication_event(
        session,
        organization_id=organization_id,
        actor_user_id=user.id,
        action="login_passkey",
        source_ip=source_ip,
        request=request,
        set_org_context=True,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=True)


# ---------------------------------------------------------------------------
# Google sign-in - IAM-010a/b/c/e
# ---------------------------------------------------------------------------


async def login_google_start(
    request: Request,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    try:
        client = build_google_oidc_client()
    except RuntimeError as exc:
        raise problem(
            request, 503, "errors.google_signin_unavailable", reason="not_configured"
        ) from exc

    flow = client.start_sign_in()
    await SqlCeremonyRepository(session).create_google_ceremony(
        state=flow.state, nonce=flow.nonce, code_verifier=flow.code_verifier
    )
    await session.commit()
    return {"authorization_url": flow.authorization_url}


class GoogleCallbackBody(BaseModel):
    code: str
    state: str


async def login_google_callback(
    request: Request,
    body: GoogleCallbackBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    try:
        ceremony = await SqlCeremonyRepository(session).consume_google_ceremony(state=body.state)
    except CeremonyNotFoundError as exc:
        raise problem(
            request, 410, "errors.ceremony_not_found", reason="ceremony_not_found"
        ) from exc

    try:
        client = build_google_oidc_client()
        identity = await client.complete_sign_in(
            code=body.code, code_verifier=ceremony.code_verifier, expected_nonce=ceremony.nonce
        )
    except UnverifiedGoogleEmailError as exc:
        raise problem(
            request, 401, "errors.google_signin_unverified_email", reason="unverified_email"
        ) from exc
    except InvalidGoogleIdentityError as exc:
        raise problem(
            request, 401, "errors.google_signin_failed", reason="invalid_identity"
        ) from exc

    breach_checker = build_breach_checker(settings.breach_checker_provider)
    service = GoogleSignInService(
        SqlUserRepository(session),
        SqlGoogleIdentityRepository(session),
        AuthenticationService(SqlUserRepository(session), breach_checker),
    )

    try:
        outcome = await service.sign_in(identity)
    except GoogleAccountLinkError as exc:
        raise problem(request, 409, "errors.google_signin_failed", reason="link_error") from exc

    if isinstance(outcome, GoogleSignInLinkRequired):
        # IAM-010c: no mutation happened. The client must re-submit with the
        # existing account's password via a follow-up call this route does
        # not itself expose today (confirm_link_with_password exists in
        # api.auth.google_signin; wiring a dedicated endpoint for it is a
        # small, separate follow-up, not built in this pass) - answered
        # honestly rather than silently signing nobody in.
        await session.commit()
        return {
            "status": "link_required",
            "message": message(request, "errors.google_signin_link_required"),
        }

    organization_id = await _home_organization_id(session, outcome.id)
    if organization_id is None:
        # A brand new Google identity - GoogleSignInService.sign_in already
        # created outcome's bare user row and linked the identity (see its
        # own docstring). What is missing is FR-MDL-001's one question,
        # which this response hands to a follow-up screen via a one-time
        # ticket - see this module's own docstring and signup_google below.
        ticket = await SqlCeremonyRepository(session).create_google_signup_ceremony(
            user_id=outcome.id
        )
        await session.commit()
        return {"status": "signup_required", "ticket": str(ticket), "email": outcome.email}

    source_ip = request.client.host if request.client else None
    mfa_verified = google_asserts_second_factor(identity)
    token, effective_mfa = await _issue_post_authentication_token(
        session,
        user=outcome,
        organization_id=organization_id,
        source_ip=source_ip,
        mfa_verified=mfa_verified,
    )
    await _record_authentication_event(
        session,
        organization_id=organization_id,
        actor_user_id=outcome.id,
        action="login_google",
        source_ip=source_ip,
        request=request,
        set_org_context=True,
    )
    # Read before commit, not after - see login()'s identical comment above
    # for why: this is the same commit-then-query pattern, on a session
    # opened the same way (get_bootstrap_db_session), and was equally
    # unreachable before 0048 for the equivalent reason (organization_id
    # was never non-None here either, on a returning user's Google
    # sign-in).
    enrollment = await _enrollment_status(session, outcome.id) if not effective_mfa else None
    await session.commit()
    return _auth_response(token=token, mfa_verified=effective_mfa, enrollment=enrollment)


class GoogleSignupBody(BaseModel):
    ticket: uuid.UUID
    account_model: Literal["self_managed", "firm"]
    organization_name: str
    kvk_number: str | None = None


async def signup_google(
    request: Request,
    body: GoogleSignupBody,
    session: AsyncSession = Depends(get_bootstrap_db_session),
) -> dict[str, Any]:
    """FR-MDL-001's one question, for the Google identity
    `login_google_callback` could not carry through Google's own redirect.
    The ticket resolves back to the bare `User` row
    `GoogleSignInService.sign_in` already created and linked - this handler
    never creates a user itself, only the organization and founding grant,
    via `SignupService.provision_organization` (the same code `signup()`
    runs after `register_user`, factored out for this exact reuse).
    """
    try:
        user_id = await SqlCeremonyRepository(session).consume_google_signup_ceremony(body.ticket)
    except CeremonyNotFoundError as exc:
        raise problem(
            request, 410, "errors.ceremony_not_found", reason="ceremony_not_found"
        ) from exc

    user = await SqlUserRepository(session).get_by_id(user_id)
    if user is None:
        # Structurally unreachable: the ticket's user_id came from a row
        # GoogleSignInService.sign_in just inserted in the same table this
        # reads, and users are never deleted (status is set instead) - see
        # migration 0003. Fails closed rather than assuming, the same
        # posture api.tenancy.get_tenant_context takes for the same reason.
        raise problem(request, 410, "errors.ceremony_not_found", reason="ceremony_not_found")

    breach_checker = build_breach_checker(settings.breach_checker_provider)
    service = SignupService(session, breach_checker)
    try:
        result = await service.provision_organization(
            user,
            account_model=body.account_model,
            organization_name=body.organization_name,
            kvk_number=body.kvk_number,
        )
    except OwnerRoleMissingError as exc:
        raise problem(
            request, 500, "errors.signup_unavailable", reason="owner_role_missing"
        ) from exc

    session_service = _session_service_for(session)
    new_session, _raw_token = await session_service.issue_session(
        user.id, privileged=True, mfa_verified=False
    )
    token = issue_access_token(
        user_id=user.id,
        organization_id=result.organization_id,
        session_id=new_session.id,
        mfa_verified=False,
        expires_at=new_session.expires_at,
    )
    await _record_authentication_event(
        session,
        organization_id=result.organization_id,
        actor_user_id=user.id,
        action="signup_google",
        source_ip=request.client.host if request.client else None,
        request=request,
    )
    await session.commit()
    return _auth_response(token=token, mfa_verified=False)

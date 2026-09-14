"""The account's own security settings (docs/founder-review-2026-09-14.md
§4.4): where am I signed in and can I end one of those (IAM-017), which
passkeys and which second factor does this account hold and can I remove
one (IAM-010, IAM-011, IAM-010f), and change my password (IAM-013).

Registered via `register(app)`, not `include_router` - see
api.documents.routes.register's docstring for why. Kept apart from
api.account.routes (GET /v1/me) so the two can be built in parallel; both
sit under /v1/me and both share one argument for being authorization-exempt
(api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS): every route here reads
or changes only rows the verified token's own user owns, and no route names
another user's.

--- The three guards on removing something ---

  IAM-011  MFA is mandatory with no opt-out. So the last second factor
           cannot be removed: DELETE on the only active passkey when no
           authenticator app is enrolled, or on the authenticator app when
           no passkey is, is refused (`last_mfa_factor`). Add the
           replacement first, then remove the old one - the order a person
           replacing a lost phone follows anyway.
  IAM-010f "Any user whose only method is Google is prompted to add a passkey
           or password before they can post to the ledger." Removing a
           passkey must not manufacture that state, or the worse one where
           no sign-in method is left at all: DELETE on a passkey is refused
           (`last_sign_in_method`) when the account has no password and no
           other active passkey.
  IAM-016  "absolute re-authentication for sensitive actions." Changing the
           password re-proves the current one before anything is written -
           a stolen open session cannot set a password its thief knows.
           An account with NO password (Google-only, per IAM-010f's own
           prompt) sets its first one with no current password to prove;
           the MFA-verified session is the proof there.

Every mutation is an IAM-090 authentication event, recorded from the
handler through AuditTrail.authentication - the same mechanism
api.auth.routes uses and for the same reason: these routes carry no
require_permission(audit=...) to hang a declaration on, so
tests/test_audit_coverage.py lists them with that reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import ActorType, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.audit.trail import AuditTrail
from api.auth.breach_check import build_breach_checker
from api.auth.geolocation import build_geolocation_resolver
from api.auth.mfa import MfaEnrollmentChecker
from api.auth.models import Session
from api.auth.passkeys import Passkey, PasskeyNotFoundError, WebAuthnService
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.passwords import WeakPasswordError, hash_password, validate_password_policy
from api.auth.rate_limiting import AuthRateLimiter, LoggingAnomalyAlerter
from api.auth.rate_limiting_repository import SqlAuthAttemptRepository
from api.auth.repository import SqlSessionRepository, SqlUserRepository
from api.auth.service import AuthenticationService, InvalidCredentialsError
from api.auth.sessions import SessionListing, SessionService
from api.auth.totp import TotpNotEnrolledError, TotpService
from api.auth.totp_repository import SqlTotpRepository
from api.config import settings
from api.crypto.kms import build_kms
from api.db import get_db_session
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/me"


def register(app: FastAPI) -> None:
    app.add_api_route(f"{_BASE}/sessions", list_sessions, methods=["GET"], name="list_my_sessions")
    app.add_api_route(
        f"{_BASE}/sessions/{{session_id}}",
        revoke_session,
        methods=["DELETE"],
        name="revoke_my_session",
    )
    app.add_api_route(f"{_BASE}/passkeys", list_passkeys, methods=["GET"], name="list_my_passkeys")
    app.add_api_route(
        f"{_BASE}/passkeys/{{passkey_id}}",
        revoke_passkey,
        methods=["DELETE"],
        name="revoke_my_passkey",
    )
    app.add_api_route(
        f"{_BASE}/password", change_password, methods=["POST"], name="change_my_password"
    )
    app.add_api_route(f"{_BASE}/mfa/totp", remove_totp, methods=["DELETE"], name="remove_my_totp")


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _session_service_for(session: AsyncSession) -> SessionService:
    return SessionService(
        SqlSessionRepository(session),
        max_lifetime_hours=settings.session_max_lifetime_hours,
        idle_timeout_minutes_privileged=settings.session_idle_timeout_minutes_privileged,
        geolocation=build_geolocation_resolver(settings.geolocation_provider),
    )


def _webauthn_service_for(session: AsyncSession) -> WebAuthnService:
    return WebAuthnService(
        SqlPasskeyRepository(session),
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        expected_origin=settings.webauthn_origin,
    )


async def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None or tenant.session_id is None:
        raise problem(request, 401, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


async def _record(
    session: AsyncSession,
    request: Request,
    tenant: TenantContext,
    *,
    action: str,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    detail: dict[str, Any] | None = None,
) -> None:
    """IAM-090's "authentication events", from the handler - see the module
    docstring. On get_db_session's session, so it already carries the
    tenant's app.current_org_id.
    """
    await AuditTrail(AuditLog(SqlAuditRepository(session))).authentication(
        organization_id=tenant.organization_id,
        actor_user_id=tenant.user_id,
        actor_type=ActorType.USER,
        action=action,
        outcome=outcome,
        source_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Sessions - IAM-017
# ---------------------------------------------------------------------------


def _is_live(session: Session, *, now: datetime) -> bool:
    return session.revoked_at is None and session.expires_at > now


def _session_json(
    listing: SessionListing, *, current_session_id: uuid.UUID | None
) -> dict[str, Any]:
    session = listing.session
    return {
        "id": str(session.id),
        "is_current": session.id == current_session_id,
        "created_at": session.created_at.isoformat(),
        "last_active_at": session.last_active_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "mfa_verified": session.mfa_verified,
        "ip_address": session.ip_address,
        "user_agent": session.user_agent,
        # IAM-017's "with location": null when nothing could place it - the
        # local resolver, a private address, a provider outage - never a
        # placeholder pretending to know (ADR-010).
        "location": (
            {"city": listing.location.city, "country": listing.location.country}
            if listing.location is not None
            else None
        ),
        "active_administration_id": (
            str(session.active_administration_id)
            if session.active_administration_id is not None
            else None
        ),
    }


async def list_sessions(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """IAM-017: "device sessions are listed to the user with location and
    last-use". Live sessions only - a revoked or expired one is history the
    person cannot act on, and the list exists for acting (revoking).
    """
    user_id = await _require_user(request, tenant)
    now = _now()
    listings = await _session_service_for(session).list_sessions_with_location(user_id)
    return {
        "sessions": [
            _session_json(listing, current_session_id=tenant.session_id)
            for listing in listings
            if _is_live(listing.session, now=now)
        ]
    }


async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """IAM-017: "individually revocable". Takes effect on that device's very
    next request, because api.tenancy reads the row every time
    (ADR-060). Revoking the session this request arrived on is allowed and
    is simply a sign-out; the response's `was_current` says so.

    404 for a session that is not this user's, exactly as for one that does
    not exist: `sessions` has no RLS (users are global, 0003), so the
    ownership predicate is here, and it must not become an oracle for which
    session ids exist.
    """
    user_id = await _require_user(request, tenant)
    service = _session_service_for(session)
    target = await service.get_session(session_id)
    if target is None or target.user_id != user_id or not _is_live(target, now=_now()):
        raise problem(request, 404, "errors.session_not_found", reason="session_not_found")

    await service.revoke_session(session_id)
    await _record(
        session,
        request,
        tenant,
        action="session_revoke",
        detail={"session_id": str(session_id), "was_current": session_id == tenant.session_id},
    )
    await session.commit()
    return {
        "status": "revoked",
        "session_id": str(session_id),
        "was_current": session_id == tenant.session_id,
    }


# ---------------------------------------------------------------------------
# Passkeys - IAM-010, IAM-010f, IAM-011
# ---------------------------------------------------------------------------


def _passkey_json(passkey: Passkey) -> dict[str, Any]:
    return {
        "id": str(passkey.id),
        "name": passkey.name,
        "created_at": passkey.created_at.isoformat(),
        "last_used_at": passkey.last_used_at.isoformat() if passkey.last_used_at else None,
        # 'platform' (Touch ID, Windows Hello) or 'cross-platform' (a
        # security key, a synced passkey) or null - what the browser
        # reported at enrolment (ADR-008). Shown, not acted on.
        "authenticator_attachment": passkey.authenticator_attachment,
        "backed_up": passkey.backed_up,
    }


async def list_passkeys(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    passkeys = await SqlPasskeyRepository(session).list_for_user(user_id)
    return {"passkeys": [_passkey_json(p) for p in passkeys if not p.is_revoked]}


async def revoke_passkey(
    passkey_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = await _require_user(request, tenant)
    passkeys = SqlPasskeyRepository(session)
    target = await passkeys.get_by_id(passkey_id)
    if target is None or target.user_id != user_id or target.is_revoked:
        # Same answer whether it never existed, is somebody else's, or was
        # already removed - api.auth.passkeys.PasskeyNotFoundError's own
        # reasoning, applied before the guards below so a probe learns
        # nothing about this account from which refusal it gets.
        raise problem(request, 404, "errors.passkey_not_found", reason="passkey_not_found")

    remaining = [
        p for p in await passkeys.list_for_user(user_id) if not p.is_revoked and p.id != passkey_id
    ]
    users = SqlUserRepository(session)
    has_password = await users.get_password_credential(user_id) is not None
    totp = await SqlTotpRepository(session).get_for_user(user_id)
    has_totp = totp is not None and totp.is_active

    # IAM-010f, and its degenerate case. Checked before IAM-011 because it
    # is the more specific instruction: "set a password" is what clears it,
    # where "add another factor" would send the person to enrol an
    # authenticator app that still leaves them unable to sign in.
    if not remaining and not has_password:
        raise problem(request, 409, "errors.last_sign_in_method", reason="last_sign_in_method")
    # IAM-011: mandatory MFA, no opt-out - removing the only factor is one.
    if not remaining and not has_totp:
        raise problem(request, 409, "errors.last_mfa_factor", reason="last_mfa_factor")

    try:
        await _webauthn_service_for(session).revoke_passkey(user_id, passkey_id)
    except PasskeyNotFoundError as exc:
        raise problem(request, 404, "errors.passkey_not_found", reason="passkey_not_found") from exc

    await _record(
        session,
        request,
        tenant,
        action="passkey_revoke",
        detail={"passkey_id": str(passkey_id), "name": target.name},
    )
    await session.commit()
    return {"status": "revoked", "passkey_id": str(passkey_id)}


# ---------------------------------------------------------------------------
# Password - IAM-013, IAM-016
# ---------------------------------------------------------------------------


class ChangePasswordBody(BaseModel):
    # Optional only for an account that has no password yet (Google-only,
    # IAM-010f's prompt). An account that has one must supply it.
    current_password: str | None = None
    new_password: str


async def change_password(
    request: Request,
    body: ChangePasswordBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Re-authenticates with the current password (IAM-016's sensitive-action
    re-authentication), applies IAM-013's policy including breach screening
    to the new one, stores it, and revokes every OTHER live session: a
    password change is the ordinary response to "I think someone else has
    it", and leaving that someone signed in would defeat it. The current
    session is kept and marked freshly re-authenticated.

    Rate-limited per account like login (IAM-019): the current-password
    check is a password oracle to anyone holding a stolen open session.
    """
    user_id = await _require_user(request, tenant)
    assert tenant.session_id is not None
    users = SqlUserRepository(session)
    user = await users.get_by_id(user_id)
    assert user is not None, "a verified tenant context named a nonexistent user"

    limiter = AuthRateLimiter(SqlAuthAttemptRepository(session), LoggingAnomalyAlerter())
    source_ip = request.client.host if request.client else None
    decision = await limiter.check(endpoint="password_change", account_key=user.email)
    if not decision.allowed:
        raise problem(
            request,
            429,
            "errors.rate_limited",
            reason=decision.reason,
            retry_after_seconds=decision.retry_after_seconds,
        )

    breach_checker = build_breach_checker(settings.breach_checker_provider)
    existing = await users.get_password_credential(user_id)
    if existing is not None:
        try:
            await AuthenticationService(users, breach_checker).authenticate_with_password(
                user.email, body.current_password or ""
            )
        except InvalidCredentialsError as exc:
            await limiter.record_attempt(
                endpoint="password_change",
                account_key=user.email,
                source_ip=source_ip,
                outcome="failure",
            )
            await _record(
                session, request, tenant, action="password_change", outcome=AuditOutcome.DENIED
            )
            await session.commit()
            raise problem(
                request,
                401,
                "errors.invalid_current_password",
                reason="invalid_current_password",
            ) from exc

    try:
        await validate_password_policy(body.new_password, breach_checker=breach_checker)
    except WeakPasswordError as exc:
        raise problem(
            request,
            422,
            "errors.weak_password",
            reason="weak_password",
            weaknesses=list(exc.reasons),
        ) from exc

    await users.upsert_password_credential(user_id, password_hash=hash_password(body.new_password))
    await limiter.record_attempt(
        endpoint="password_change", account_key=user.email, source_ip=source_ip, outcome="success"
    )

    sessions = _session_service_for(session)
    await sessions.record_reauthentication(tenant.session_id)
    now = _now()
    others = [
        s
        for s in await sessions.list_sessions(user_id)
        if s.id != tenant.session_id and _is_live(s, now=now)
    ]
    for other in others:
        await sessions.revoke_session(other.id)

    await _record(
        session,
        request,
        tenant,
        action="password_set" if existing is None else "password_change",
        detail={"other_sessions_revoked": len(others)},
    )
    await session.commit()
    return {
        "status": "password_set" if existing is None else "password_changed",
        "other_sessions_revoked": len(others),
    }


# ---------------------------------------------------------------------------
# TOTP - IAM-011, IAM-012
# ---------------------------------------------------------------------------


async def remove_totp(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Removes the authenticator app. IAM-011 refuses when it is the only
    second factor - enrol a passkey first. Re-enrolment afterwards is the
    ordinary enrol/begin + confirm flow (0006's partial unique index allows
    a new active credential once the old one is revoked).
    """
    user_id = await _require_user(request, tenant)
    status = await MfaEnrollmentChecker(
        SqlPasskeyRepository(session), SqlTotpRepository(session)
    ).check(user_id)
    if not status.has_totp:
        raise problem(request, 404, "errors.totp_not_enrolled", reason="not_enrolled")
    if not status.has_passkey:
        raise problem(request, 409, "errors.last_mfa_factor", reason="last_mfa_factor")

    try:
        await TotpService(SqlTotpRepository(session), build_kms()).revoke(user_id)
    except TotpNotEnrolledError as exc:
        raise problem(request, 404, "errors.totp_not_enrolled", reason="not_enrolled") from exc

    await _record(session, request, tenant, action="totp_revoke")
    await session.commit()
    return {"status": "revoked"}

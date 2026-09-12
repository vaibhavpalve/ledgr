"""Proves IAM-011's "no opt-out" at the HTTP edge: MfaEnforcementMiddleware
runs on every non-exempt request, before any handler, and its decision is
driven entirely by the injected policy/enrollment state - no database
needed here, since MfaEnrollmentChecker already accepts the same
PasskeyRepository/TotpRepository Protocols the in-memory test fakes
satisfy. DB-backed proof that the real SQL-backed checker (the production
default) behaves the same way lives in tests/integration/.
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.auth.mfa import MfaEnrollmentChecker, MfaRequirementPolicy
from api.config import settings
from api.main import app as production_app
from api.mfa_middleware import MfaEnforcementMiddleware
from api.tenancy import TenantContext, TenantContextMiddleware, get_tenant_context
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository
from tests.support.fake_totp_repository import InMemoryTotpRepository


def _sign(claims: dict[str, object]) -> str:
    return jwt.encode(claims, settings.jwt_signing_key, algorithm="HS256")


class _NeverRequireMfaPolicy:
    async def is_required(self, *, user_id: uuid.UUID) -> bool:
        return False


def _probe_app(
    *,
    requirement_policy: MfaRequirementPolicy | None = None,
    enrollment_checker: MfaEnrollmentChecker,
) -> tuple[FastAPI, list[TenantContext]]:
    handler_calls: list[TenantContext] = []
    app = FastAPI()
    # Order matters: last-added runs first. TenantContextMiddleware must
    # run before MfaEnforcementMiddleware, so it is added second - see
    # api.mfa_middleware's module docstring and api.main's wiring.
    app.add_middleware(
        MfaEnforcementMiddleware,
        requirement_policy=requirement_policy,
        enrollment_checker=enrollment_checker,
    )
    app.add_middleware(TenantContextMiddleware)

    @app.get("/protected")
    def protected(tenant: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        handler_calls.append(tenant)
        return {"organization_id": str(tenant.organization_id)}

    return app, handler_calls


def _token(*, mfa_verified: bool, user_id: uuid.UUID | None = None) -> str:
    return _sign(
        {
            "org_id": str(uuid.uuid4()),
            "sub": str(user_id or uuid.uuid4()),
            "mfa_verified": mfa_verified,
        }
    )


async def _checker_with_passkey(user_id: uuid.UUID) -> MfaEnrollmentChecker:
    passkeys = InMemoryPasskeyRepository()
    await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=b"cred",
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )
    return MfaEnrollmentChecker(passkeys, InMemoryTotpRepository())


def _empty_checker() -> MfaEnrollmentChecker:
    return MfaEnrollmentChecker(InMemoryPasskeyRepository(), InMemoryTotpRepository())


def test_unenrolled_user_is_blocked_before_the_handler_runs() -> None:
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker())
    client = TestClient(app)

    response = client.get(
        "/protected", headers={"Authorization": f"Bearer {_token(mfa_verified=False)}"}
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "not_enrolled"
    assert handler_calls == []


async def test_enrolled_but_unverified_user_is_blocked_with_a_different_reason() -> None:
    user_id = uuid.uuid4()
    app, handler_calls = _probe_app(enrollment_checker=await _checker_with_passkey(user_id))
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {_token(mfa_verified=False, user_id=user_id)}"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "not_verified"
    assert handler_calls == []


def test_verified_user_reaches_the_handler_even_with_no_enrolled_factor() -> None:
    """mfa_verified=True short-circuits the evaluation entirely - see
    MfaPolicyService.evaluate. This models a session whose second factor
    was already checked at issuance (e.g. Google amr, IAM-010e); the
    THIS-request enrollment lookup is skipped, not merely satisfied by it.
    """
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker())
    client = TestClient(app)

    response = client.get(
        "/protected", headers={"Authorization": f"Bearer {_token(mfa_verified=True)}"}
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


def test_when_mfa_is_not_required_the_handler_runs_regardless_of_verification() -> None:
    app, handler_calls = _probe_app(
        requirement_policy=_NeverRequireMfaPolicy(), enrollment_checker=_empty_checker()
    )
    client = TestClient(app)

    response = client.get(
        "/protected", headers={"Authorization": f"Bearer {_token(mfa_verified=False)}"}
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


def test_a_token_with_no_sub_claim_is_blocked_fail_closed() -> None:
    """No user_id means MFA status can't be evaluated at all - fails
    closed rather than assuming satisfied.
    """
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker())
    client = TestClient(app)
    token = _sign({"org_id": str(uuid.uuid4()), "mfa_verified": True})  # no sub

    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["reason"] == "no_user_context"
    assert handler_calls == []


def test_default_requirement_policy_is_always_require() -> None:
    # No requirement_policy override passed here - confirms the
    # middleware's own default (AlwaysRequireMfaPolicy) is active, not
    # merely that an explicitly-injected one works.
    app, _ = _probe_app(enrollment_checker=_empty_checker())
    client = TestClient(app)

    response = client.get(
        "/protected", headers={"Authorization": f"Bearer {_token(mfa_verified=False)}"}
    )
    assert response.status_code == 403  # AlwaysRequireMfaPolicy requires it by default


def test_health_endpoint_is_exempt_from_mfa_enforcement() -> None:
    """Against the REAL production app, deliberately - /health must never
    need a database round-trip, and this confirms the exemption is
    checked before the middleware would attempt one.
    """
    client = TestClient(production_app)

    response = client.get("/health")

    assert response.status_code == 200

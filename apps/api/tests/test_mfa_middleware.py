"""Proves IAM-011's "no opt-out" at the HTTP edge: MfaEnforcementMiddleware
runs on every non-exempt request, before any handler, and its decision is
driven entirely by the injected policy/enrollment state - no database
needed here, since MfaEnrollmentChecker already accepts the same
PasskeyRepository/TotpRepository Protocols the in-memory test fakes
satisfy. DB-backed proof that the real SQL-backed checker (the production
default) behaves the same way lives in tests/integration/.

--- Since ADR-060, `mfa_verified` is a fact about the SESSION ROW ---

The token no longer gets to assert it. TenantContextMiddleware resolves the
`sid` claim against the session store on every request and puts the row's
`mfa_verified_at` on the TenantContext this middleware reads, so a probe app
here installs tests/support/fake_session_validator's in-memory validator and
each test ISSUES a session in the state it wants to test - the same thing a
real step-up verification does, minus Postgres. A token minted with no live
session behind it is refused one layer earlier (401), which is why the
"fail closed" case below no longer arrives through a header.
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from api.auth.mfa import MfaEnrollmentChecker, MfaRequirementPolicy
from api.config import settings
from api.main import app as production_app
from api.mfa_middleware import MfaEnforcementMiddleware
from api.tenancy import TenantContext, TenantContextMiddleware, get_tenant_context
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository
from tests.support.fake_session_validator import InMemorySessionValidator
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
    validator: InMemorySessionValidator,
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
    app.add_middleware(TenantContextMiddleware, session_validator=validator)

    @app.get("/protected")
    def protected(tenant: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        handler_calls.append(tenant)
        return {"organization_id": str(tenant.organization_id)}

    return app, handler_calls


async def _signed_in(
    validator: InMemorySessionValidator,
    *,
    mfa_verified: bool,
    user_id: uuid.UUID | None = None,
) -> str:
    """A token naming a real session in the state under test. `mfa_verified`
    is written into the claim as well, exactly as api.auth.tokens writes it
    for the client's benefit - and deliberately: every test below would still
    pass if the server read the claim, except that it does not, which is what
    the session row above is proving.
    """
    user_id = user_id or uuid.uuid4()
    session_id = await validator.issue(user_id, mfa_verified=mfa_verified)
    return _sign(
        {
            "org_id": str(uuid.uuid4()),
            "sub": str(user_id),
            "sid": str(session_id),
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


async def test_unenrolled_user_is_blocked_before_the_handler_runs() -> None:
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker(), validator=validator)
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {await _signed_in(validator, mfa_verified=False)}"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "not_enrolled"
    assert handler_calls == []


async def test_enrolled_but_unverified_user_is_blocked_with_a_different_reason() -> None:
    user_id = uuid.uuid4()
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(
        enrollment_checker=await _checker_with_passkey(user_id), validator=validator
    )
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={
            "Authorization": (
                f"Bearer {await _signed_in(validator, mfa_verified=False, user_id=user_id)}"
            )
        },
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "not_verified"
    assert handler_calls == []


async def test_verified_user_reaches_the_handler_even_with_no_enrolled_factor() -> None:
    """mfa_verified=True short-circuits the evaluation entirely - see
    MfaPolicyService.evaluate. This models a session whose second factor
    was already checked at issuance (e.g. Google amr, IAM-010e); the
    THIS-request enrollment lookup is skipped, not merely satisfied by it.
    """
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker(), validator=validator)
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {await _signed_in(validator, mfa_verified=True)}"},
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


async def test_a_verified_claim_does_not_survive_an_unverified_session_row() -> None:
    """ADR-060, stated as a test: the claim says yes and the row says no, and
    the row wins. Before that change this request reached the handler.
    """
    user_id = uuid.uuid4()
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker(), validator=validator)
    session_id = await validator.issue(user_id, mfa_verified=False)
    token = _sign(
        {
            "org_id": str(uuid.uuid4()),
            "sub": str(user_id),
            "sid": str(session_id),
            "mfa_verified": True,
        }
    )

    response = TestClient(app).get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["reason"] == "not_enrolled"
    assert handler_calls == []


async def test_when_mfa_is_not_required_the_handler_runs_regardless_of_verification() -> None:
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(
        requirement_policy=_NeverRequireMfaPolicy(),
        enrollment_checker=_empty_checker(),
        validator=validator,
    )
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {await _signed_in(validator, mfa_verified=False)}"},
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1


def test_a_token_with_no_sub_claim_never_reaches_the_mfa_gate() -> None:
    """Fail closed, one layer earlier than it used to. Since ADR-060 a token
    naming no user (or no session) is refused by TenantContextMiddleware
    itself, so the 403 this test once asserted is now a 401 from the door.
    The MFA middleware's own no_user_context branch remains as defence in
    depth and is exercised directly below.
    """
    validator = InMemorySessionValidator()
    app, handler_calls = _probe_app(enrollment_checker=_empty_checker(), validator=validator)
    client = TestClient(app)
    token = _sign({"org_id": str(uuid.uuid4()), "mfa_verified": True})  # no sub, no sid

    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert handler_calls == []


def test_a_tenant_context_naming_no_user_is_refused_by_the_mfa_gate_itself() -> None:
    """The defensive branch the test above can no longer reach through a
    header. Driven by planting the context directly, which is the only way in
    now that the layer before this one refuses such a token - the branch is
    kept because "unreachable" is a property of today's middleware order, not
    a guarantee this middleware is entitled to assume about tomorrow's.
    """
    handler_calls: list[TenantContext] = []
    app = FastAPI()

    class _PlantUserlessContext(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            request.state.tenant_context = TenantContext(organization_id=uuid.uuid4(), user_id=None)
            return await call_next(request)

    app.add_middleware(MfaEnforcementMiddleware, enrollment_checker=_empty_checker())
    app.add_middleware(_PlantUserlessContext)

    @app.get("/protected")
    def protected() -> dict[str, str]:
        handler_calls.append(TenantContext(organization_id=uuid.uuid4()))
        return {"status": "ok"}

    response = TestClient(app).get("/protected")

    assert response.status_code == 403
    assert response.json()["reason"] == "no_user_context"
    assert handler_calls == []


async def test_default_requirement_policy_is_always_require() -> None:
    # No requirement_policy override passed here - confirms the
    # middleware's own default (AlwaysRequireMfaPolicy) is active, not
    # merely that an explicitly-injected one works.
    validator = InMemorySessionValidator()
    app, _ = _probe_app(enrollment_checker=_empty_checker(), validator=validator)
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {await _signed_in(validator, mfa_verified=False)}"},
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

"""Proves CLAUDE.md's third non-negotiable at the HTTP edge: a request
without verified tenant context is rejected before it reaches any handler.

And, since ADR-060, the other half of the same rule: the bearer JWT proves
who the caller is, but whether their SESSION is still live - revoked,
expired, idle, MFA-verified, which client it has open - is read from the
session row on every request, never from the token. These tests drive
TenantContextMiddleware against tests/support/fake_session_validator's
in-memory validator, which is the real api.auth.sessions.SessionService
over the in-memory repository - so the revocation/expiry/idle/ownership
logic exercised here is the production logic with only the storage swapped.
tests/integration/test_session_backed_context.py proves the SQL-backed
validator behaves the same against real rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app as production_app
from api.tenancy import TenantContext, TenantContextMiddleware, get_tenant_context
from tests.support.fake_session_validator import InMemorySessionValidator


def _sign(claims: dict[str, object], key: str = settings.jwt_signing_key) -> str:
    return jwt.encode(claims, key, algorithm="HS256")


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _probe_app(
    validator: InMemorySessionValidator,
) -> tuple[FastAPI, list[TenantContext]]:
    handler_calls: list[TenantContext] = []
    probe_app = FastAPI()
    probe_app.add_middleware(TenantContextMiddleware, session_validator=validator)

    @probe_app.get("/protected")
    def protected(tenant: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        handler_calls.append(tenant)  # would prove the handler ran, if it did
        return {"organization_id": str(tenant.organization_id)}

    return probe_app, handler_calls


def _token(*, org_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID, **extra: object) -> str:
    return _sign({"org_id": str(org_id), "sub": str(user_id), "sid": str(session_id), **extra})


@pytest.fixture
def production_validator() -> Iterator[InMemorySessionValidator]:
    """The app.state hook: installs an in-memory validator on the REAL
    api.main app for the duration of one test, so the production middleware
    stack can be driven without Postgres and without skipping the lookup.
    """
    validator = InMemorySessionValidator()
    production_app.state.session_validator = validator
    try:
        yield validator
    finally:
        del production_app.state.session_validator


def test_request_without_tenant_context_never_reaches_the_handler() -> None:
    """The strongest form of the requirement: not just a 401 response, but
    proof the handler body itself was never executed.
    """
    probe_app, handler_calls = _probe_app(InMemorySessionValidator())
    client = TestClient(probe_app)

    response = client.get("/protected")  # no Authorization header at all

    assert response.status_code == 401
    assert handler_calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "not-a-bearer-scheme"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer this-is-not-a-jwt"},
        {"Authorization": f"Bearer {_sign({'sub': str(uuid.uuid4())})}"},  # no org_id claim
        {
            "Authorization": (
                f"Bearer {_sign({'org_id': str(uuid.uuid4())}, key='wrong-signing-key')}"
            )
        },
        {"Authorization": f"Bearer {_sign({'org_id': 'not-a-uuid'})}"},
        # ADR-060: a token naming no user, or no session, has nothing for the
        # row lookup to resolve and is refused before any lookup happens.
        {
            "Authorization": (
                f"Bearer {_sign({'org_id': str(uuid.uuid4()), 'sid': str(uuid.uuid4())})}"
            )
        },
        {
            "Authorization": (
                f"Bearer {_sign({'org_id': str(uuid.uuid4()), 'sub': str(uuid.uuid4())})}"
            )
        },
    ],
    ids=[
        "no-header",
        "not-bearer-scheme",
        "empty-token",
        "malformed-jwt",
        "missing-org-id-claim",
        "wrong-signing-key",
        "org-id-not-a-uuid",
        "missing-sub-claim",
        "missing-sid-claim",
    ],
)
def test_missing_or_invalid_tenant_context_is_rejected(
    headers: dict[str, str], production_validator: InMemorySessionValidator
) -> None:
    client = TestClient(production_app)

    response = client.get("/v1/whoami", headers={**headers, "Accept-Language": "en"})

    assert response.status_code == 401
    assert "Sign in again" in response.json()["detail"]


@pytest.mark.parametrize(
    ("accept_language", "expected"),
    [("nl", "Meld u opnieuw aan"), ("en", "Sign in again"), (None, "Meld u opnieuw aan")],
    ids=["dutch", "english", "no-header-falls-to-the-default"],
)
def test_the_refusal_is_written_in_the_callers_language(
    accept_language: str | None, expected: str
) -> None:
    """FR-UX-007, at the earliest gate in the chain.

    This is the refusal a person meets when their session runs out mid-task,
    and answering it needs no token - knowing which of two languages to write
    in never did, which is the same reason IAM-010g puts a language control on
    the login screen.

    The reason stays a stable, untranslated string: a client redirecting to
    login and an operator grepping a log both read it, and neither should have
    to parse a translated sentence to do so.
    """
    headers = {} if accept_language is None else {"Accept-Language": accept_language}
    client = TestClient(production_app)

    response = client.get("/v1/whoami", headers=headers)

    assert response.status_code == 401
    assert expected in response.json()["detail"]
    assert response.json()["reason"] == "missing bearer token"


@pytest.mark.isolation("GET", "/v1/whoami")
async def test_valid_tenant_context_reaches_the_handler(
    production_validator: InMemorySessionValidator,
) -> None:
    """Also this route's IAM-005 isolation coverage: /v1/whoami returns
    exactly the calling token's own organization_id and nothing else, by
    construction — there is no cross-tenant data it could leak, and this
    assertion is what proves that rather than assuming it.

    The session the token names is MFA-verified (on the ROW - see
    tests/test_mfa_middleware.py for that gate's own tests), so this request
    also clears api.mfa_middleware.MfaEnforcementMiddleware.
    """
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await production_validator.issue(user_id, mfa_verified=True)
    token = _token(org_id=org_id, user_id=user_id, session_id=session_id)

    client = TestClient(production_app)
    response = client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"organization_id": str(org_id)}


def test_health_endpoint_is_exempt_from_tenant_context() -> None:
    client = TestClient(production_app)

    response = client.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# ADR-060: the session row is the authority, the token's claims are not
# ---------------------------------------------------------------------------


async def test_mfa_verified_comes_from_the_session_row_not_the_token() -> None:
    """A token asserting mfa_verified=true for a session whose row says
    otherwise is not believed - and the reverse: a step-up recorded on the
    row is visible to the OLD token on the very next request, with no
    re-mint.
    """
    validator = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(validator)
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await validator.issue(user_id, mfa_verified=False)
    client = TestClient(probe_app)
    token = _token(org_id=org_id, user_id=user_id, session_id=session_id, mfa_verified=True)

    client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert handler_calls[-1].mfa_verified is False

    await validator.service.record_mfa_verification(session_id)
    client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert handler_calls[-1].mfa_verified is True


async def test_active_administration_comes_from_the_session_row_not_the_token() -> None:
    """FR-FRM-000a: the client the header shows is what the switcher wrote
    to the row. A forged `adm` claim asserts nothing.
    """
    validator = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(validator)
    org_id, user_id, administration_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session_id = await validator.issue(user_id, active_administration_id=administration_id)
    client = TestClient(probe_app)
    token = _token(org_id=org_id, user_id=user_id, session_id=session_id, adm=str(uuid.uuid4()))

    client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert handler_calls[-1].active_administration_id == administration_id
    assert handler_calls[-1].session_id == session_id
    assert handler_calls[-1].user_id == user_id


async def test_a_revoked_session_is_refused_on_the_very_next_request() -> None:
    """The gap ADR-054 named, closed: nothing about the token changed, and
    it stops working the moment the row says so.
    """
    validator = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(validator)
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await validator.issue(user_id)
    client = TestClient(probe_app)
    headers = {
        "Authorization": f"Bearer {_token(org_id=org_id, user_id=user_id, session_id=session_id)}"
    }

    assert client.get("/protected", headers=headers).status_code == 200
    await validator.revoke(session_id)
    refused = client.get("/protected", headers=headers)

    assert refused.status_code == 401
    assert refused.json()["reason"] == "session_revoked"
    assert len(handler_calls) == 1


async def test_a_token_naming_a_session_that_does_not_exist_is_refused() -> None:
    validator = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(validator)
    client = TestClient(probe_app)
    token = _token(org_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=uuid.uuid4())

    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["reason"] == "session_not_found"
    assert handler_calls == []


async def test_a_token_naming_someone_elses_session_is_refused() -> None:
    """A signed token with a real `sid` but a different `sub` is a spliced
    token: no session this system issued belongs to anyone but the user in
    its own `sub`. Refused as if the session did not exist.
    """
    validator = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(validator)
    session_id = await validator.issue(uuid.uuid4())
    client = TestClient(probe_app)
    token = _token(org_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=session_id)

    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["reason"] == "session_user_mismatch"
    assert handler_calls == []


async def test_idle_timeout_and_absolute_expiry_are_measured_on_the_row() -> None:
    """IAM-016, finally enforced per request: thirty idle minutes signs a
    privileged session out; activity resets the clock; twelve hours ends it
    regardless.
    """
    clock = _FakeClock()
    validator = InMemorySessionValidator(clock=clock)
    probe_app, _ = _probe_app(validator)
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await validator.issue(user_id, privileged=True)
    client = TestClient(probe_app)
    headers = {
        "Authorization": f"Bearer {_token(org_id=org_id, user_id=user_id, session_id=session_id)}"
    }

    clock.advance(minutes=25)
    assert client.get("/protected", headers=headers).status_code == 200  # activity: touched
    clock.advance(minutes=25)
    assert client.get("/protected", headers=headers).status_code == 200  # 25 since the touch

    clock.advance(minutes=31)
    idle = client.get("/protected", headers=headers)
    assert idle.status_code == 401
    assert idle.json()["reason"] == "session_idle_timeout"

    # A fresh session, then twelve hours of steady activity: absolute expiry
    # wins over the idle clock being reset.
    session_id = await validator.issue(user_id, privileged=True)
    headers = {
        "Authorization": f"Bearer {_token(org_id=org_id, user_id=user_id, session_id=session_id)}"
    }
    for _ in range(24):
        clock.advance(minutes=29)
        client.get("/protected", headers=headers)
    clock.advance(minutes=29)
    expired = client.get("/protected", headers=headers)
    assert expired.status_code == 401
    assert expired.json()["reason"] == "session_expired"


async def test_every_request_touches_last_active_at() -> None:
    clock = _FakeClock()
    validator = InMemorySessionValidator(clock=clock)
    probe_app, _ = _probe_app(validator)
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await validator.issue(user_id)
    client = TestClient(probe_app)
    issued_at = clock.now

    clock.advance(minutes=3)
    client.get(
        "/protected",
        headers={
            "Authorization": (
                f"Bearer {_token(org_id=org_id, user_id=user_id, session_id=session_id)}"
            )
        },
    )

    row = await validator.repository.get_by_id(session_id)
    assert row is not None
    assert row.last_active_at == issued_at + timedelta(minutes=3)


def test_an_exempt_path_never_consults_the_validator() -> None:
    """EXEMPT_PATHS semantics are unchanged: signup and login establish a
    session rather than presenting one, so there is nothing to look up.
    """

    class _Exploding:
        async def validate(self, session_id: uuid.UUID, *, user_id: uuid.UUID) -> object:
            raise AssertionError("an exempt path must not resolve a session")

    probe_app = FastAPI()
    probe_app.add_middleware(TenantContextMiddleware, session_validator=_Exploding())  # type: ignore[arg-type]

    @probe_app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    assert TestClient(probe_app).get("/health").status_code == 200


async def test_the_app_state_hook_takes_precedence_over_the_constructed_validator() -> None:
    """What lets a test drive the real api.main app: whatever is installed
    on app.state.session_validator is consulted, not what the middleware was
    built with.
    """
    constructed = InMemorySessionValidator()
    installed = InMemorySessionValidator()
    probe_app, handler_calls = _probe_app(constructed)
    probe_app.state.session_validator = installed
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    session_id = await installed.issue(user_id)
    client = TestClient(probe_app)

    response = client.get(
        "/protected",
        headers={
            "Authorization": (
                f"Bearer {_token(org_id=org_id, user_id=user_id, session_id=session_id)}"
            )
        },
    )

    assert response.status_code == 200
    assert len(handler_calls) == 1

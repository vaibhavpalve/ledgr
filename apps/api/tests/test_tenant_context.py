"""Proves CLAUDE.md's third non-negotiable at the HTTP edge: a request
without verified tenant context is rejected before it reaches any handler.
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app as production_app
from api.tenancy import TenantContext, TenantContextMiddleware, get_tenant_context


def _sign(claims: dict[str, object], key: str = settings.jwt_signing_key) -> str:
    return jwt.encode(claims, key, algorithm="HS256")


def test_request_without_tenant_context_never_reaches_the_handler() -> None:
    """The strongest form of the requirement: not just a 401 response, but
    proof the handler body itself was never executed.
    """
    handler_calls: list[TenantContext] = []

    probe_app = FastAPI()
    probe_app.add_middleware(TenantContextMiddleware)

    @probe_app.get("/protected")
    def protected(tenant: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
        handler_calls.append(tenant)  # would prove the handler ran, if it did
        return {"organization_id": str(tenant.organization_id)}

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
    ],
    ids=[
        "no-header",
        "not-bearer-scheme",
        "empty-token",
        "malformed-jwt",
        "missing-org-id-claim",
        "wrong-signing-key",
        "org-id-not-a-uuid",
    ],
)
def test_missing_or_invalid_tenant_context_is_rejected(headers: dict[str, str]) -> None:
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
def test_valid_tenant_context_reaches_the_handler() -> None:
    """Also this route's IAM-005 isolation coverage: /v1/whoami returns
    exactly the calling token's own organization_id and nothing else, by
    construction — there is no cross-tenant data it could leak, and this
    assertion is what proves that rather than assuming it.

    The token carries sub and mfa_verified: true so this request also
    clears api.mfa_middleware.MfaEnforcementMiddleware, which now runs on
    every non-exempt route (IAM-011) - see tests/test_mfa_middleware.py
    for that middleware's own dedicated tests. Without mfa_verified this
    request would be correctly blocked with 403, which is a different
    property than the one this test exists to prove.
    """
    org_id = uuid.uuid4()
    token = _sign({"org_id": str(org_id), "sub": str(uuid.uuid4()), "mfa_verified": True})

    client = TestClient(production_app)
    response = client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"organization_id": str(org_id)}


def test_health_endpoint_is_exempt_from_tenant_context() -> None:
    client = TestClient(production_app)

    response = client.get("/health")

    assert response.status_code == 200

"""Proves IAM-010f at the HTTP layer: a Google-only account is blocked
from the specific action this requirement names, before the handler runs,
via a FastAPI dependency - not middleware, since this gates one action,
not every request (see api.auth.account_continuity's module docstring for
why). No real ledger-posting endpoint exists yet (FR-GL is unbuilt); this
test declares a minimal probe route using the dependency, the same way
tests/test_tenant_context.py probes api.tenancy.get_tenant_context.

Uses FastAPI's own dependency_overrides, not a real database session -
api.auth.account_continuity.get_sign_in_method_checker and
api.tenancy.get_tenant_context are both ordinary dependencies, so tests
substitute in-memory-backed / fixed values for them directly.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.auth.account_continuity import (
    SignInMethodChecker,
    get_sign_in_method_checker,
    require_ledger_posting_eligibility,
)
from api.auth.google_oidc import GoogleIdentity
from api.tenancy import TenantContext, get_tenant_context
from tests.support.fake_auth_repository import InMemoryUserRepository
from tests.support.fake_google_identity_repository import InMemoryGoogleIdentityRepository
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository


def _probe_app(*, checker: SignInMethodChecker, user_id: uuid.UUID) -> tuple[FastAPI, list[bool]]:
    handler_calls: list[bool] = []
    app = FastAPI()

    @app.post("/v1/ledger/postings")
    def post_entry(_: None = Depends(require_ledger_posting_eligibility)) -> dict[str, str]:
        handler_calls.append(True)
        return {"status": "posted"}

    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        organization_id=uuid.uuid4(), user_id=user_id
    )
    app.dependency_overrides[get_sign_in_method_checker] = lambda: checker
    return app, handler_calls


async def test_google_only_account_is_blocked_before_the_handler_runs() -> None:
    user_id = uuid.uuid4()
    users = InMemoryUserRepository()
    google = InMemoryGoogleIdentityRepository()
    await google.link(
        user_id, GoogleIdentity(subject="sub-1", email="owner@example.com", name=None, picture=None)
    )
    checker = SignInMethodChecker(users, google, InMemoryPasskeyRepository())

    app, handler_calls = _probe_app(checker=checker, user_id=user_id)
    client = TestClient(app)

    response = client.post("/v1/ledger/postings")

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "google_only_account"
    assert handler_calls == []


async def test_account_with_google_and_a_password_is_allowed_through() -> None:
    user_id = uuid.uuid4()
    users = InMemoryUserRepository()
    google = InMemoryGoogleIdentityRepository()
    await google.link(
        user_id, GoogleIdentity(subject="sub-1", email="owner@example.com", name=None, picture=None)
    )
    await users.upsert_password_credential(user_id, password_hash="a-real-hash")
    checker = SignInMethodChecker(users, google, InMemoryPasskeyRepository())

    app, handler_calls = _probe_app(checker=checker, user_id=user_id)
    client = TestClient(app)

    response = client.post("/v1/ledger/postings")

    assert response.status_code == 200
    assert handler_calls == [True]


async def test_account_with_google_and_a_passkey_is_allowed_through() -> None:
    user_id = uuid.uuid4()
    users = InMemoryUserRepository()
    google = InMemoryGoogleIdentityRepository()
    await google.link(
        user_id, GoogleIdentity(subject="sub-1", email="owner@example.com", name=None, picture=None)
    )
    passkeys = InMemoryPasskeyRepository()
    await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=uuid.uuid4().bytes,
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )
    checker = SignInMethodChecker(users, google, passkeys)

    app, handler_calls = _probe_app(checker=checker, user_id=user_id)
    client = TestClient(app)

    response = client.post("/v1/ledger/postings")

    assert response.status_code == 200
    assert handler_calls == [True]


async def test_an_account_with_no_google_identity_at_all_is_allowed_through() -> None:
    """The gate is specifically about Google-ONLY accounts - a password-
    only or passkey-only account was never at risk and must not be
    blocked by this check.
    """
    user_id = uuid.uuid4()
    users = InMemoryUserRepository()
    await users.upsert_password_credential(user_id, password_hash="a-real-hash")
    checker = SignInMethodChecker(
        users, InMemoryGoogleIdentityRepository(), InMemoryPasskeyRepository()
    )

    app, handler_calls = _probe_app(checker=checker, user_id=user_id)
    client = TestClient(app)

    response = client.post("/v1/ledger/postings")

    assert response.status_code == 200
    assert handler_calls == [True]

"""IAM-090 at the HTTP edge: AuditMiddleware records what a route declares,
with the outcome the request actually got.

No database - the middleware takes an injected AuditLog, and these use the
in-memory one. What is being tested is which entry gets written and what it
says, both of which are the middleware's job rather than the schema's.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.audit.log import ActorType, AuditCategory, AuditLog, AuditOutcome
from api.audit.middleware import CORRELATION_HEADER, AuditMiddleware
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    get_firm_access_register,
    organization_scope,
    require_permission,
)
from api.authz.firm_access_register import FirmAccessRegister
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.tenancy import TenantContext, get_tenant_context
from tests.authz.helpers import World, build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_firm_access_repository import InMemoryFirmAccessRepository


def _app(world: World, *, with_tenant: bool = True) -> tuple[FastAPI, InMemoryAuditRepository]:
    audit = InMemoryAuditRepository()
    app = FastAPI()
    app.add_middleware(AuditMiddleware, audit_log=AuditLog(audit))

    if with_tenant:
        # AuditMiddleware reads request.state.tenant_context, which
        # TenantContextMiddleware sets in the real app - a dependency
        # override cannot reach it, because middleware runs outside the
        # dependency system entirely. Added AFTER AuditMiddleware so it is
        # the outermost layer and runs first (last added = outermost).
        @app.middleware("http")
        async def set_tenant(request, call_next):  # type: ignore[no-untyped-def]
            request.state.tenant_context = TenantContext(
                organization_id=world.acme, user_id=world.user, mfa_verified=True
            )
            return await call_next(request)

    @app.post("/v1/administrations/{administration_id}/entries")
    def post_entry(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "post",
                "journal_entry",
                scope=administration_from_path("administration_id"),
                audit=AuditCategory.POSTING,
            )
        ),
    ) -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/administrations/{administration_id}/boom")
    def boom(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "post",
                "journal_entry",
                scope=administration_from_path("administration_id"),
                audit=AuditCategory.POSTING,
            )
        ),
    ) -> dict[str, str]:
        raise HTTPException(status_code=422, detail="bad input")

    @app.get("/v1/undeclared")
    def undeclared(
        _: AuthorizationDecision = Depends(
            require_permission("view", "administration", scope=organization_scope())
        ),
    ) -> dict[str, str]:
        return {"status": "ok"}

    if with_tenant:
        app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
            organization_id=world.acme, user_id=world.user, mfa_verified=True
        )
    app.dependency_overrides[get_authorization_service] = lambda: AuthorizationService(
        world.repository
    )
    app.dependency_overrides[get_firm_access_register] = lambda: FirmAccessRegister(
        InMemoryFirmAccessRepository(world.repository)
    )
    return app, audit


def _owner_world() -> World:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    return world


async def test_a_successful_mutating_request_is_recorded() -> None:
    world = _owner_world()
    app, audit = _app(world)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_books}/entries")
    assert response.status_code == 200

    entries = await audit.search(organization_id=world.acme)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.category is AuditCategory.POSTING
    assert entry.outcome is AuditOutcome.SUCCESS
    assert entry.actor_user_id == world.user
    assert entry.actor_type is ActorType.USER
    assert entry.administration_id == world.acme_books
    # Action and resource type come from the permission the route already
    # requires, so the log cannot disagree with the authorization decision.
    assert entry.action == "post"
    assert entry.resource_type == "journal_entry"


async def test_a_denied_request_is_recorded_as_denied() -> None:
    """The entry most worth having. It survives because the middleware runs
    after the response with its own session - inside the request transaction
    it would roll back with everything else.
    """
    world = build_world()  # no grants
    app, audit = _app(world)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_books}/entries")
    assert response.status_code == 403

    entries = await audit.search(organization_id=world.acme)
    assert len(entries) == 1
    assert entries[0].outcome is AuditOutcome.DENIED


async def test_a_failed_request_is_recorded_as_a_failure_not_a_denial() -> None:
    """A refusal is a security signal and a failure is usually operational.
    Collapsing them would make the log worse at the thing it exists for.
    """
    world = _owner_world()
    app, audit = _app(world)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_books}/boom")
    assert response.status_code == 422

    entries = await audit.search(organization_id=world.acme)
    assert entries[0].outcome is AuditOutcome.FAILURE


async def test_a_route_declaring_nothing_records_nothing() -> None:
    world = _owner_world()
    app, audit = _app(world)

    assert TestClient(app).get("/v1/undeclared").status_code == 200

    assert await audit.search(organization_id=world.acme) == []


async def test_the_entry_carries_the_iam_091_request_fields() -> None:
    world = _owner_world()
    app, audit = _app(world)

    TestClient(app).post(
        f"/v1/administrations/{world.acme_books}/entries",
        headers={"user-agent": "LEDGR/1.0", CORRELATION_HEADER: "req-abc-123"},
    )

    entry = (await audit.search(organization_id=world.acme))[0]
    assert entry.user_agent == "LEDGR/1.0"
    assert entry.correlation_id == "req-abc-123"
    assert entry.source_ip is not None
    assert entry.detail["method"] == "POST"
    assert entry.detail["status_code"] == 200


async def test_a_correlation_id_is_generated_when_the_caller_sends_none() -> None:
    world = _owner_world()
    app, audit = _app(world)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_books}/entries")

    entry = (await audit.search(organization_id=world.acme))[0]
    assert entry.correlation_id
    # Echoed back so the caller can quote it in a support conversation.
    assert response.headers[CORRELATION_HEADER] == entry.correlation_id


def test_a_supplied_correlation_id_is_echoed_unchanged() -> None:
    world = _owner_world()
    app, _ = _app(world)

    response = TestClient(app).post(
        f"/v1/administrations/{world.acme_books}/entries",
        headers={CORRELATION_HEADER: "client-trace-9"},
    )

    assert response.headers[CORRELATION_HEADER] == "client-trace-9"


async def test_an_mfa_denial_is_recorded_as_an_authentication_event() -> None:
    """IAM-090's "authentication events", at the one place in the request
    path where such an event has a tenant to attribute it to.

    Recorded as AUTHENTICATION rather than under the route's own category:
    the request never reached the route, so calling it a posting would say
    something that did not happen.
    """
    world = _owner_world()
    audit = InMemoryAuditRepository()
    app = FastAPI()

    # Order mirrors api.main: AuditMiddleware sits OUTSIDE the MFA gate, so a
    # request refused there is still recorded. Last added is outermost, so the
    # MFA stand-in goes first and Audit second.
    @app.middleware("http")
    async def deny_mfa(request, call_next):  # type: ignore[no-untyped-def]
        from starlette.responses import JSONResponse

        request.state.authentication_denial = "not_verified"
        return JSONResponse(status_code=403, content={"reason": "not_verified"})

    app.add_middleware(AuditMiddleware, audit_log=AuditLog(audit))

    @app.middleware("http")
    async def set_tenant(request, call_next):  # type: ignore[no-untyped-def]
        request.state.tenant_context = TenantContext(
            organization_id=world.acme, user_id=world.user, mfa_verified=False
        )
        return await call_next(request)

    @app.post("/v1/administrations/{administration_id}/entries")
    def post_entry(administration_id: uuid.UUID) -> dict[str, str]:
        return {"status": "ok"}

    TestClient(app).post(f"/v1/administrations/{world.acme_books}/entries")

    entries = await audit.search(organization_id=world.acme)
    assert len(entries) == 1
    assert entries[0].category is AuditCategory.AUTHENTICATION
    assert entries[0].action == "verify_mfa"
    assert entries[0].outcome is AuditOutcome.DENIED
    assert entries[0].detail["reason"] == "not_verified"


async def test_a_request_with_no_tenant_records_nothing() -> None:
    """audit_log.organization_id is NOT NULL by design (ADR-020), so there is
    no tenant to attribute the entry to. A request that never established one
    is already recorded in auth_attempt (IAM-019).
    """
    world = _owner_world()
    audit = InMemoryAuditRepository()
    app = FastAPI()
    app.add_middleware(AuditMiddleware, audit_log=AuditLog(audit))

    @app.post("/v1/anything")
    def anything() -> dict[str, str]:
        return {"status": "ok"}

    assert TestClient(app).post("/v1/anything").status_code == 200

    assert await audit.search(organization_id=world.acme) == []


async def test_an_unmatched_path_records_nothing() -> None:
    world = _owner_world()
    app, audit = _app(world)

    assert TestClient(app).post("/v1/no-such-route").status_code == 404

    assert await audit.search(organization_id=world.acme) == []

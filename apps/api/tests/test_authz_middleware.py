"""The runtime half of "a developer cannot forget to call the authorization
library": AuthorizationEnforcementMiddleware refuses to dispatch to a route
that declares no requirement.

Also exercises the full HTTP path of require_permission - target resolution
from a path parameter, IAM-033 attribute extraction from a JSON body, and
the 403 shape - against the real AuthorizationService backed by the
in-memory repository fake. No database.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api.authz.dependencies import (
    AttributeSources,
    _json_body,
    administration_from_path,
    from_body,
    get_authorization_service,
    get_firm_access_register,
    organization_scope,
    require_permission,
)
from api.authz.firm_access_register import FirmAccessRegister
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.authz_middleware import AuthorizationEnforcementMiddleware
from api.tenancy import TenantContext, get_tenant_context
from tests.authz.helpers import World, build_world
from tests.support.fake_firm_access_repository import InMemoryFirmAccessRepository


def _probe_app(world: World) -> tuple[FastAPI, list[str]]:
    """Four routes: one organization-scoped, one administration-scoped, one
    with IAM-033 attribute sources, and one that FORGOT to declare anything.
    handler_calls records which handlers actually ran, so "the request was
    refused before the handler" is asserted rather than assumed.
    """
    handler_calls: list[str] = []
    app = FastAPI()
    app.add_middleware(AuthorizationEnforcementMiddleware)

    @app.get("/v1/administrations")
    def list_administrations(
        _: AuthorizationDecision = Depends(
            require_permission("view", "administration", scope=organization_scope())
        ),
    ) -> dict[str, str]:
        handler_calls.append("list")
        return {"status": "ok"}

    @app.get("/v1/administrations/{administration_id}")
    def get_administration(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "view", "administration", scope=administration_from_path("administration_id")
            )
        ),
    ) -> dict[str, str]:
        handler_calls.append("get")
        return {"status": "ok"}

    @app.post("/v1/administrations/{administration_id}/approvals")
    def approve(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "approve",
                "purchase_invoice",
                scope=administration_from_path("administration_id"),
                attributes=AttributeSources(amount=from_body("amount")),
            )
        ),
    ) -> dict[str, str]:
        handler_calls.append("approve")
        return {"status": "ok"}

    # The mistake this middleware exists to catch: a route that touches
    # tenant data and declares no authorization requirement at all.
    @app.get("/v1/forgotten")
    def forgotten() -> dict[str, str]:
        handler_calls.append("forgotten")
        return {"secret": "should never be reachable"}

    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        organization_id=world.acme, user_id=world.user, mfa_verified=True
    )
    app.dependency_overrides[get_authorization_service] = lambda: AuthorizationService(
        world.repository
    )
    # require_permission records the access (IAM-109) after authorizing, so
    # the register has to be overridden here too - without it the dependency
    # resolves the real SQL-backed one and reaches for a database these
    # tests deliberately do not have.
    app.dependency_overrides[get_firm_access_register] = lambda: FirmAccessRegister(
        InMemoryFirmAccessRepository(world.repository)
    )

    return app, handler_calls


# --- the forgetting case ----------------------------------------------------


def test_a_route_that_declares_no_authorization_is_refused_before_its_handler() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    app, handler_calls = _probe_app(world)

    response = TestClient(app, raise_server_exceptions=False).get("/v1/forgotten")

    assert response.status_code == 500
    assert response.json()["reason"] == "route_declares_no_authorization"
    # The point of the whole mechanism: the handler never ran, so a route
    # that forgot the check leaks nothing while it is being forgotten.
    assert handler_calls == []


def test_the_refusal_message_names_the_route_and_the_fix() -> None:
    world = build_world()
    app, _ = _probe_app(world)

    detail = TestClient(app, raise_server_exceptions=False).get("/v1/forgotten").json()["detail"]

    assert "/v1/forgotten" in detail
    assert "require_permission" in detail
    assert "AUTHORIZATION_EXEMPT_PATHS" in detail


def test_an_unknown_path_is_left_to_the_router_to_404() -> None:
    world = build_world()
    app, _ = _probe_app(world)

    response = TestClient(app, raise_server_exceptions=False).get("/v1/no-such-route")

    assert response.status_code == 404


def test_a_known_path_with_the_wrong_method_is_left_to_the_router_to_405() -> None:
    world = build_world()
    app, _ = _probe_app(world)

    response = TestClient(app, raise_server_exceptions=False).delete("/v1/administrations")

    assert response.status_code == 405


# --- the ordinary path ------------------------------------------------------


def test_a_permitted_caller_reaches_the_handler() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    app, handler_calls = _probe_app(world)

    response = TestClient(app).get("/v1/administrations")

    assert response.status_code == 200
    assert handler_calls == ["list"]


def test_a_caller_without_the_permission_is_denied_before_the_handler() -> None:
    world = build_world()  # no assignment at all
    app, handler_calls = _probe_app(world)

    response = TestClient(app).get("/v1/administrations")

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "no_matching_grant"
    assert handler_calls == []


def test_the_administration_target_comes_from_the_path_not_the_tenant() -> None:
    """A caller holding Accountant on acme_books only. The same route, same
    tenant context, different path parameter - and the answers differ,
    which is what proves the decision is about the administration the
    request names.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    app, handler_calls = _probe_app(world)
    client = TestClient(app)

    granted = client.get(f"/v1/administrations/{world.acme_books}")
    other = client.get(f"/v1/administrations/{world.acme_holding}")

    assert granted.status_code == 200
    assert other.status_code == 403
    assert handler_calls == ["get"]


def test_a_malformed_administration_id_is_a_400_not_a_silent_organization_check() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    app, handler_calls = _probe_app(world)

    response = TestClient(app).get("/v1/administrations/not-a-uuid")

    # FastAPI's own uuid.UUID path conversion rejects it first (422); either
    # way the request must not reach the handler, and must not fall back to
    # an organization-scoped check.
    assert response.status_code in (400, 422)
    assert handler_calls == []


# --- IAM-033 over HTTP ------------------------------------------------------


def test_an_amount_ceiling_is_enforced_against_the_request_body() -> None:
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    app, handler_calls = _probe_app(world)
    client = TestClient(app)
    path = f"/v1/administrations/{world.acme_books}/approvals"

    within = client.post(path, json={"amount": "4200.00"})
    over = client.post(path, json={"amount": "9000.00"})

    assert within.status_code == 200
    assert over.status_code == 403
    assert over.json()["detail"]["reason"] == "condition_not_satisfied"
    assert handler_calls == ["approve"]


async def test_a_bare_json_number_amount_becomes_a_decimal_never_a_float() -> None:
    """CLAUDE.md rule four at the HTTP boundary. A body carrying a bare JSON
    number (not a quoted string) is exactly what stdlib json.loads would
    turn into a float on its way to an amount-ceiling comparison;
    _json_body parses with parse_float=Decimal so it never can.
    """
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", b"application/json")],
        }
    )
    request._body = b'{"amount": 7299.70}'

    parsed = await _json_body(request)

    assert isinstance(parsed["amount"], Decimal)
    assert not isinstance(parsed["amount"], float)
    assert parsed["amount"] == Decimal("7299.70")


def test_an_amount_sent_as_a_json_number_is_still_checked_against_the_ceiling() -> None:
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "7299.70"},
    )
    app, _ = _probe_app(world)
    client = TestClient(app)
    path = f"/v1/administrations/{world.acme_books}/approvals"

    at_ceiling = client.post(
        path, content=b'{"amount": 7299.70}', headers={"Content-Type": "application/json"}
    )
    over = client.post(
        path, content=b'{"amount": 7299.71}', headers={"Content-Type": "application/json"}
    )

    assert at_ceiling.status_code == 200
    assert over.status_code == 403


def test_a_missing_amount_denies_against_a_ceilinged_grant() -> None:
    """The other forgetting case: the route declared where to find the
    amount, but this request did not supply one. An unsupplied attribute is
    never treated as "no limit applies."
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    app, handler_calls = _probe_app(world)

    response = TestClient(app).post(
        f"/v1/administrations/{world.acme_books}/approvals", json={"note": "no amount here"}
    )

    assert response.status_code == 403
    assert handler_calls == []


# --- IAM-109: require_permission records the access -------------------------
#
# Covered here, with no database, as well as in the integration suite. The
# wiring is a single line in require_permission and its only other coverage
# is DB-gated, so without these it could be deleted and every no-DB CI run
# would still pass.


def _recording_app(
    world: World,
) -> tuple[FastAPI, InMemoryFirmAccessRepository]:
    access = InMemoryFirmAccessRepository(world.repository)
    app = FastAPI()
    app.add_middleware(AuthorizationEnforcementMiddleware)

    @app.get("/v1/administrations/{administration_id}")
    def read(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "view", "administration", scope=administration_from_path("administration_id")
            )
        ),
    ) -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/administrations")
    def index(
        _: AuthorizationDecision = Depends(
            require_permission("view", "administration", scope=organization_scope())
        ),
    ) -> dict[str, str]:
        return {"status": "ok"}

    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        organization_id=world.acme, user_id=world.user, mfa_verified=True
    )
    app.dependency_overrides[get_authorization_service] = lambda: AuthorizationService(
        world.repository
    )
    app.dependency_overrides[get_firm_access_register] = lambda: FirmAccessRegister(access)
    return app, access


def test_an_authorized_administration_request_records_the_access() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    app, access = _recording_app(world)

    response = TestClient(app).get(f"/v1/administrations/{world.acme_books}")

    assert response.status_code == 200
    record = access.access_record(user_id=world.user, administration_id=world.acme_books)
    assert record is not None and record.access_count == 1


def test_a_denied_request_records_no_access() -> None:
    """A row claiming someone was in the books must mean they got in.
    Recording runs after authorization, so a refused probe leaves nothing.
    """
    world = build_world()  # no grants at all
    app, access = _recording_app(world)

    response = TestClient(app).get(f"/v1/administrations/{world.acme_books}")

    assert response.status_code == 403
    assert access.access_record(user_id=world.user, administration_id=world.acme_books) is None


def test_an_organization_scoped_request_records_nothing() -> None:
    """An organization-level action is not access to anybody's books, and
    there is no administration to attribute it to.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    app, access = _recording_app(world)

    assert TestClient(app).get("/v1/administrations").status_code == 200

    assert access.access_record(user_id=world.user, administration_id=world.acme_books) is None


# --- FR-FRM-000a: the active-client guard -----------------------------------
#
# "Posting to the wrong client is the single worst usability failure in this
# product." This is the only mechanism in that requirement that PREVENTS the
# failure rather than making it less likely, so it gets the most tests.


def _two_client_app(world: World, *, active: uuid.UUID | None) -> tuple[FastAPI, list[str]]:
    """A session with `active` open, and routes on any administration."""
    calls: list[str] = []
    app = FastAPI()
    app.add_middleware(AuthorizationEnforcementMiddleware)

    @app.get("/v1/administrations/{administration_id}")
    def read(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "view", "administration", scope=administration_from_path("administration_id")
            )
        ),
    ) -> dict[str, str]:
        calls.append("read")
        return {"status": "ok"}

    @app.post("/v1/administrations/{administration_id}/entries")
    def post_entry(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "view", "administration", scope=administration_from_path("administration_id")
            )
        ),
    ) -> dict[str, str]:
        calls.append("post")
        return {"status": "ok"}

    @app.put("/v1/switcher/{administration_id}")
    def switch(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "view",
                "administration",
                scope=administration_from_path("administration_id"),
                allows_cross_client=True,
            )
        ),
    ) -> dict[str, str]:
        calls.append("switch")
        return {"status": "ok"}

    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        organization_id=world.acme,
        user_id=world.user,
        mfa_verified=True,
        active_administration_id=active,
    )
    app.dependency_overrides[get_authorization_service] = lambda: AuthorizationService(
        world.repository
    )
    app.dependency_overrides[get_firm_access_register] = lambda: FirmAccessRegister(
        InMemoryFirmAccessRepository(world.repository)
    )
    return app, calls


def _owner_world() -> World:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    return world


def test_posting_to_a_different_client_than_the_one_open_is_refused() -> None:
    world = _owner_world()
    app, calls = _two_client_app(world, active=world.acme_books)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_holding}/entries")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "active_client_mismatch"
    assert calls == [], "the write never reached the handler"


def test_the_refusal_names_both_clients_so_the_ui_can_explain() -> None:
    world = _owner_world()
    app, _ = _two_client_app(world, active=world.acme_books)

    detail = (
        TestClient(app)
        .post(
            f"/v1/administrations/{world.acme_holding}/entries",
            headers={"Accept-Language": "en"},
        )
        .json()["detail"]
    )

    assert detail["active_administration_id"] == str(world.acme_books)
    assert detail["requested_administration_id"] == str(world.acme_holding)
    assert "Switch client first" in detail["message"]


def test_the_refusal_is_written_in_the_readers_language() -> None:
    """FR-UX-007: this is the refusal in this module a person is most likely
    to meet mid-task, so it is the one that most needs to be in their own
    language.

    The `reason` does NOT move. A client branching on the failure, an operator
    grepping for it and this suite asserting on it all need one stable token,
    which is why the sentence and the machine-readable half are separate
    fields rather than one translated string carrying both.
    """
    world = _owner_world()
    app, _ = _two_client_app(world, active=world.acme_books)
    client = TestClient(app)
    path = f"/v1/administrations/{world.acme_holding}/entries"

    dutch = client.post(path, headers={"Accept-Language": "nl"}).json()["detail"]
    english = client.post(path, headers={"Accept-Language": "en"}).json()["detail"]
    # No header at all: a caller that expressed no preference gets the
    # product's default language, not the last one somebody asked for.
    unspecified = client.post(path).json()["detail"]

    assert "Wissel eerst van klant" in dutch["message"]
    assert "Switch client first" in english["message"]
    assert dutch["message"] != english["message"]
    assert unspecified["message"] == dutch["message"]

    assert {entry["reason"] for entry in (dutch, english, unspecified)} == {
        "active_client_mismatch"
    }


def test_posting_to_the_client_that_is_open_is_allowed() -> None:
    world = _owner_world()
    app, calls = _two_client_app(world, active=world.acme_books)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_books}/entries")

    assert response.status_code == 200
    assert calls == ["post"]


def test_reads_are_never_constrained_by_the_active_client() -> None:
    """Opening a client's page from a link or a search result is ordinary.
    Refusing it would make the product unusable to protect against nothing -
    the failure named is POSTING to the wrong client.
    """
    world = _owner_world()
    app, calls = _two_client_app(world, active=world.acme_books)

    response = TestClient(app).get(f"/v1/administrations/{world.acme_holding}")

    assert response.status_code == 200
    assert calls == ["read"]


def test_a_session_inside_no_client_constrains_nothing() -> None:
    """A non-interactive API caller, or a session at the switcher. There is
    no header showing a name, so there is nothing for the request to
    contradict.
    """
    world = _owner_world()
    app, calls = _two_client_app(world, active=None)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_holding}/entries")

    assert response.status_code == 200
    assert calls == ["post"]


def test_the_switcher_may_write_across_clients() -> None:
    """The one route whose entire job is changing which client is open."""
    world = _owner_world()
    app, calls = _two_client_app(world, active=world.acme_books)

    response = TestClient(app).put(f"/v1/switcher/{world.acme_holding}")

    assert response.status_code == 200
    assert calls == ["switch"]


def test_the_mismatch_is_reported_even_when_the_caller_holds_the_permission() -> None:
    """The dangerous case. A caller who lacks permission is refused anyway;
    the request that would otherwise SUCCEED against the wrong books is the
    one this guard exists for, which is why it runs before authorization.
    """
    world = _owner_world()  # Owner: permitted on both administrations
    app, _ = _two_client_app(world, active=world.acme_books)

    response = TestClient(app).post(f"/v1/administrations/{world.acme_holding}/entries")

    assert response.status_code == 409, "not 403 - this is a client-state bug, not a grant"


def test_a_mismatched_write_records_no_access_against_the_wrong_client() -> None:
    """The guard runs before authorization, so a wrong-client write leaves
    nothing in the other client's access register either - IAM-109's list
    must not show someone as having been in books they never reached.
    """
    world = _owner_world()
    access = InMemoryFirmAccessRepository(world.repository)
    app, _ = _two_client_app(world, active=world.acme_books)
    app.dependency_overrides[get_firm_access_register] = lambda: FirmAccessRegister(access)

    TestClient(app).post(f"/v1/administrations/{world.acme_holding}/entries")

    assert access.access_record(user_id=world.user, administration_id=world.acme_holding) is None


def test_the_handler_can_still_read_a_body_the_dependency_already_parsed() -> None:
    """Reading the body for attribute extraction must not consume it -
    Starlette caches it, and a handler that parses the same request normally
    has to keep working.
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    app, _ = _probe_app(world)

    seen: dict[str, object] = {}

    @app.post("/v1/administrations/{administration_id}/echo")
    async def echo(
        administration_id: uuid.UUID,
        payload: dict[str, object],
        _: AuthorizationDecision = Depends(
            require_permission(
                "approve",
                "purchase_invoice",
                scope=administration_from_path("administration_id"),
                attributes=AttributeSources(amount=from_body("amount")),
            )
        ),
    ) -> dict[str, str]:
        seen.update(payload)
        return {"status": "ok"}

    response = TestClient(app).post(
        f"/v1/administrations/{world.acme_books}/echo",
        json={"amount": "10.00", "memo": "office chairs"},
    )

    assert response.status_code == 200
    assert seen["memo"] == "office chairs"

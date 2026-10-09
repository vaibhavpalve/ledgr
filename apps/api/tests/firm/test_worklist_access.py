"""The firm home's portfolio authorization (api.firm.worklist_access, ADR-109). No database.

What matters here: every administration is put to `authorize()` on its own, a denial leaves
that one out (default deny, per client), the route-level requirement is visible to the
coverage checks, and the request-scoped repository reads each grant set once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from api.authz.dependencies import PERMISSION_MARKER, declared_requirements
from api.authz.model import (
    AdministrationScope,
    AuthorizationDecision,
    AuthorizationRequest,
    Grant,
)
from api.authz.service import AuthorizationService
from api.firm.worklist_access import (
    PORTFOLIO_READ,
    PortfolioAuthorizer,
    RequestScopedAuthorizationRepository,
)
from api.main import app

FIRM_ROUTES = {
    ("GET", "/v1/firm/summary"),
    ("POST", "/v1/firm/summary/seen"),
    ("GET", "/v1/firm/worklist"),
    ("POST", "/v1/firm/clients/{administration_id}/snooze"),
    ("POST", "/v1/firm/clients/assign"),
    ("GET", "/v1/firm/staff"),
    ("GET", "/v1/firm/deadlines"),
}


async def test_each_administration_is_authorized_on_its_own() -> None:
    allowed_id, denied_id = uuid.uuid4(), uuid.uuid4()
    asked: list[AuthorizationRequest] = []
    primed: list[Sequence[uuid.UUID]] = []

    async def authorize(request: AuthorizationRequest) -> AuthorizationDecision:
        asked.append(request)
        assert isinstance(request.target, AdministrationScope)
        if request.target.administration_id == allowed_id:
            return AuthorizationDecision(allowed=True, reason="allowed")
        return AuthorizationDecision(allowed=False, reason="no_matching_grant")

    async def prime(ids: Sequence[uuid.UUID]) -> None:
        primed.append(ids)

    authorizer = PortfolioAuthorizer(authorize, prime=prime)
    user = uuid.uuid4()
    result = await authorizer.allowed(
        user_id=user,
        administration_ids=[allowed_id, denied_id, allowed_id],
        action="view",
        resource_type="report",
    )

    assert result == {allowed_id}
    assert len(asked) == 2  # duplicates asked once
    assert all((r.user_id, r.action, r.resource_type) == (user, "view", "report") for r in asked)
    assert primed == [[allowed_id, denied_id]]


async def test_an_empty_portfolio_asks_nothing() -> None:
    async def authorize(request: AuthorizationRequest) -> AuthorizationDecision:
        raise AssertionError("nothing to authorize")

    assert (
        await PortfolioAuthorizer(authorize).allowed(
            user_id=uuid.uuid4(), administration_ids=[], action="view", resource_type="report"
        )
        == set()
    )


class _CountingSession:
    """Just enough AsyncSession for SqlAuthorizationRepository.live_grants: counts executions
    and returns one administration-scoped grant."""

    def __init__(self, grant_scope: uuid.UUID) -> None:
        self.executions = 0
        self._scope = grant_scope

    async def execute(self, statement: Any, params: Any = None) -> list[Any]:
        self.executions += 1

        class _Row:
            assignment_id = uuid.uuid4()
            role_id = uuid.uuid4()
            role_name = "Accountant"
            scope_type = "administration"
            scope_id = self._scope
            resource_scope = "administration"
            conditions: dict[str, object] = {}
            granted_by_organization_id = None

        return [_Row()]


async def test_the_request_scoped_repository_reads_grants_once_per_permission() -> None:
    administration = uuid.uuid4()
    session = _CountingSession(administration)
    repository = RequestScopedAuthorizationRepository(session)  # type: ignore[arg-type]
    service = AuthorizationService(repository)
    user = uuid.uuid4()
    now = datetime.now(UTC)

    first: Sequence[Grant] = await repository.live_grants(
        user_id=user, action="view", resource_type="report", now=now
    )
    for _ in range(5):
        decision = await service.authorize(
            AuthorizationRequest(
                user_id=user,
                action="view",
                resource_type="report",
                target=AdministrationScope(administration),
            )
        )
        assert decision.allowed

    assert len(first) == 1
    assert session.executions == 1


def test_every_firm_route_declares_a_requirement() -> None:
    found = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", None) or set():
            if (method, path) in FIRM_ROUTES:
                assert declared_requirements(route), f"{method} {path}"
                found.add((method, path))
    assert found == FIRM_ROUTES


def test_the_portfolio_requirement_is_readable_without_running_it() -> None:
    requirement = getattr(PORTFOLIO_READ, PERMISSION_MARKER)
    assert (requirement.action, requirement.resource_type) == ("view", "report")
    assert requirement.scope.kind == "administration"
    assert requirement.scope.path_param is None

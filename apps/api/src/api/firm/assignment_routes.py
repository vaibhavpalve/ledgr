"""Who looks after which client, and "not this client until ...": FR-FRM-004b (partial),
FR-FRM-002 (ADR-109, migration 0078).

    POST /v1/firm/clients/{administration_id}/snooze  {"until": date|null, "reason": str|null}
    POST /v1/firm/clients/assign  {"administration_ids": [...], "user_id": uuid|null}
    GET  /v1/firm/staff           the caller's organization's users

Neither write touches the books or grants anything. An assignment is a label on the work queue,
not a role_assignment: who may open a client is still only what IAM-107's grants say.

--- Permissions ---

Snoozing is a note on ONE client's place in the queue, so it is checked like any other action on
that client: `view report` on the administration in the path - the permission the queue itself
is read with - the same "a view permission guards a write that changes only where someone is
looking" choice PUT /v1/switcher/{id} makes. It opts out of FR-FRM-000a's active-client guard
(`allows_cross_client`) because the firm home is not inside any client: refusing to snooze
client B while the session last had client A open would protect nothing - nothing here writes to
anybody's books.

Assigning work to people is §8.4's "assigning firm staff to clients" - the Firm Manager's job - so
it takes `manage user_role` at the caller's organization (Owner, Organization Admin, Firm
Manager). Each administration is then checked on its own: it must be visible to the firm (RLS:
an active engagement, or the business's own) and the assignee must hold a live grant on it, or
the assignment would put a client on someone's queue they cannot open. A failure is reported per
administration and never fails the rest.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, Field

from api.audit.log import AuditCategory
from api.authz.dependencies import (
    administration_from_path,
    organization_scope,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.firm.switcher import ClientSwitcher
from api.firm.worklist_access import (
    PORTFOLIO_ACTION,
    PORTFOLIO_READ,
    PORTFOLIO_RESOURCE,
    Portfolio,
    get_portfolio_switcher,
)
from api.firm.worklist_repository import SqlWorklistRepository
from api.firm.worklist_routes import get_worklist_repository
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

MAX_ASSIGN_BATCH = 500


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/firm/clients/{administration_id}/snooze",
        snooze_client,
        methods=["POST"],
        status_code=204,
        name="snooze_firm_client",
    )
    app.add_api_route(
        "/v1/firm/clients/assign", assign_clients, methods=["POST"], name="assign_firm_clients"
    )
    app.add_api_route("/v1/firm/staff", list_staff, methods=["GET"], name="list_firm_staff")


def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


class SnoozeBody(BaseModel):
    #: None clears the snooze.
    until: date | None = None
    reason: str | None = Field(default=None, max_length=500)


async def snooze_client(
    administration_id: uuid.UUID,
    body: SnoozeBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
    _: AuthorizationDecision = Depends(
        require_permission(
            PORTFOLIO_ACTION,
            PORTFOLIO_RESOURCE,
            scope=administration_from_path("administration_id"),
            allows_cross_client=True,
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> Response:
    user_id = _require_user(request, tenant)
    if body.until is not None and body.until <= date.today():
        raise problem(
            request,
            422,
            "errors.firm_snooze_until_not_future",
            reason="firm_snooze_until_not_future",
        )
    reason = body.reason.strip() if body.reason is not None and body.reason.strip() else None
    recorded = await repository.record_snooze(
        administration_id,
        until=body.until,
        reason=reason if body.until is not None else None,
        user_id=user_id,
    )
    if not recorded:
        # Authorized but not visible to this session is not reachable through the grant join;
        # answered as "not found" either way, never as which.
        raise problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        )
    return Response(status_code=204)


class AssignBody(BaseModel):
    administration_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ASSIGN_BATCH)
    #: None clears the assignment.
    user_id: uuid.UUID | None = None


async def assign_clients(
    body: AssignBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
    switcher: ClientSwitcher = Depends(get_portfolio_switcher),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "user_role",
            scope=organization_scope(),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    requested = list(dict.fromkeys(body.administration_ids))

    reachable_by_assignee: set[uuid.UUID] | None = None
    if body.user_id is not None:
        staff = {member.user_id for member in await repository.staff(tenant.organization_id)}
        if body.user_id not in staff:
            raise problem(
                request, 422, "errors.firm_assignee_not_staff", reason="firm_assignee_not_staff"
            )
        reachable_by_assignee = {
            entry.badge.administration_id for entry in await switcher.list(body.user_id)
        }

    visible = await repository.visible_active_administrations(requested)
    failed: list[dict[str, str]] = []
    ok: list[uuid.UUID] = []
    for administration_id in requested:
        if administration_id not in visible:
            failed.append(
                {"administration_id": str(administration_id), "reason": "administration_not_found"}
            )
        elif reachable_by_assignee is not None and administration_id not in reachable_by_assignee:
            failed.append(
                {"administration_id": str(administration_id), "reason": "assignee_has_no_access"}
            )
        else:
            ok.append(administration_id)

    assigned = await repository.record_assignments(
        ok, assigned_user_id=body.user_id, assigned_by_user_id=user_id
    )
    return {"assigned": assigned, "failed": failed}


async def list_staff(
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> list[dict[str, str]]:
    """The people the work queue can be filtered by and assigned to: users holding a live grant
    at the caller's organization, or one it made. Names come from the address (no name column).

    A portfolio route rather than an organization-scoped one: every member of the firm who works
    the queue needs the list to filter it, and firm staff hold no organization-scoped grant
    (IAM-107). It discloses colleagues' addresses to colleagues - the same organization."""
    return [
        {"user_id": str(member.user_id), "name": member.name, "email": member.email}
        for member in await repository.staff(portfolio.organization_id)
    ]

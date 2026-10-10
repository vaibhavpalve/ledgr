"""Saved views on the firm work queue: FR-FRM-000b, contract-wave2 decision 6 (ADR-115).

    GET  /v1/firm/views                   my active views, each with its live count
    POST /v1/firm/views                   {name, query}       -> 201 view
    POST /v1/firm/views/{view_id}/rename  {name}              -> view
    POST /v1/firm/views/{view_id}/archive                     -> 204

Registered via `register(app)`, like the rest of the firm home.

--- Per person ---

A view belongs to the person who saved it. RLS (0084) keeps it inside the organization; the
repository adds `user_id = <the verified token's user>` to every statement, so a colleague's view
id is answered exactly like an id that does not exist: 404.

--- Counts ---

Every count is `count_rows(rows, view query)` over ONE portfolio load per request
(`api.firm.worklist_routes.load_rows`) - not one worklist load per view. The rows are the same
the worklist page builds from the same authorized portfolio, and `count_rows` is the same
predicate `worklist_page` applies, so a view's count is what opening it shows.

--- Permissions ---

All four are portfolio routes (`require_portfolio_permission`, `view report`): the counts are
read across the caller's authorized clients. The writes change only the caller's own rows, are
declared with the portfolio permission so the authz and audit coverage checks see a requirement,
and are audited as configuration (IAM-090), as POST /v1/firm/summary/seen is.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.db import get_db_session
from api.firm.views_model import (
    MAX_ACTIVE_VIEWS,
    NAME_MAX_LENGTH,
    SavedViewQuery,
    clean_name,
    parse_stored_query,
)
from api.firm.views_repository import LimitReached, SavedView, SqlSavedViewRepository
from api.firm.worklist_access import PORTFOLIO_READ, Portfolio, require_portfolio_permission
from api.firm.worklist_model import WorklistRow, count_rows
from api.firm.worklist_repository import SqlWorklistRepository
from api.firm.worklist_routes import get_worklist_repository, load_rows
from api.i18n.http import problem

_VIEW_WRITE = require_portfolio_permission(audit=AuditCategory.CONFIGURATION)


def register(app: FastAPI) -> None:
    app.add_api_route("/v1/firm/views", list_views, methods=["GET"], name="list_firm_views")
    app.add_api_route(
        "/v1/firm/views",
        create_view,
        methods=["POST"],
        status_code=201,
        name="create_firm_view",
    )
    app.add_api_route(
        "/v1/firm/views/{view_id}/rename",
        rename_view,
        methods=["POST"],
        name="rename_firm_view",
    )
    app.add_api_route(
        "/v1/firm/views/{view_id}/archive",
        archive_view,
        methods=["POST"],
        status_code=204,
        name="archive_firm_view",
    )


async def get_saved_view_repository(
    session: AsyncSession = Depends(get_db_session),
) -> SqlSavedViewRepository:
    return SqlSavedViewRepository(session)


def view_json(view: SavedView, *, rows: list[WorklistRow], user_id: uuid.UUID) -> dict[str, object]:
    query = parse_stored_query(view.query)
    return {
        "id": str(view.id),
        "name": view.name,
        "query": view.query if query is None else query.stored(),
        "count": None
        if query is None
        else count_rows(rows, query.to_worklist_query(user_id=user_id)),
    }


def _name_or_422(request: Request, raw: str) -> str:
    name = clean_name(raw)
    if name is None:
        raise problem(
            request,
            422,
            "errors.firm_view_name_invalid",
            reason="firm_view_name_invalid",
            max_length=NAME_MAX_LENGTH,
        )
    return name


def _not_found(request: Request) -> Exception:
    return problem(request, 404, "errors.firm_view_not_found", reason="firm_view_not_found")


class CreateViewBody(BaseModel):
    name: str = Field(max_length=200)
    query: SavedViewQuery = Field(default_factory=SavedViewQuery)


class RenameViewBody(BaseModel):
    name: str = Field(max_length=200)


async def list_views(
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    views: SqlSavedViewRepository = Depends(get_saved_view_repository),
    worklist: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    active = await views.active(
        organization_id=portfolio.organization_id, user_id=portfolio.user_id
    )
    rows = await load_rows(portfolio, worklist, today=date.today()) if active else []
    return {"views": [view_json(v, rows=rows, user_id=portfolio.user_id) for v in active]}


async def create_view(
    body: CreateViewBody,
    request: Request,
    portfolio: Portfolio = Depends(_VIEW_WRITE),
    views: SqlSavedViewRepository = Depends(get_saved_view_repository),
    worklist: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    name = _name_or_422(request, body.name)
    try:
        view = await views.create(
            organization_id=portfolio.organization_id,
            user_id=portfolio.user_id,
            name=name,
            query=body.query.stored(),
            limit=MAX_ACTIVE_VIEWS,
        )
    except LimitReached as exc:
        raise problem(
            request,
            409,
            "errors.firm_view_limit_reached",
            reason="firm_view_limit_reached",
            max_views=MAX_ACTIVE_VIEWS,
        ) from exc
    rows = await load_rows(portfolio, worklist, today=date.today())
    return view_json(view, rows=rows, user_id=portfolio.user_id)


async def rename_view(
    view_id: uuid.UUID,
    body: RenameViewBody,
    request: Request,
    portfolio: Portfolio = Depends(_VIEW_WRITE),
    views: SqlSavedViewRepository = Depends(get_saved_view_repository),
    worklist: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    name = _name_or_422(request, body.name)
    view = await views.rename(
        view_id,
        organization_id=portfolio.organization_id,
        user_id=portfolio.user_id,
        name=name,
    )
    if view is None:
        raise _not_found(request)
    rows = await load_rows(portfolio, worklist, today=date.today())
    return view_json(view, rows=rows, user_id=portfolio.user_id)


async def archive_view(
    view_id: uuid.UUID,
    request: Request,
    portfolio: Portfolio = Depends(_VIEW_WRITE),
    views: SqlSavedViewRepository = Depends(get_saved_view_repository),
) -> Response:
    archived = await views.archive(
        view_id, organization_id=portfolio.organization_id, user_id=portfolio.user_id
    )
    if not archived:
        raise _not_found(request)
    return Response(status_code=204)

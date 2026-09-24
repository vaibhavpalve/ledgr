"""The person's own reminder preference (ADR-090).

    GET /v1/me/reminders   {"enabled": bool}
    PUT /v1/me/reminders   {"enabled": bool}

Authorization-exempt with its reason in api.authz.dependencies (the /v1/me/language case): it
reads and writes only the caller's own `users` row, identified from the verified token. `users`
carries no RLS (users are global, 0003), so every statement names its own predicate.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_db_session
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/me/reminders", get_my_reminders, methods=["GET"], name="get_my_reminders"
    )
    app.add_api_route(
        "/v1/me/reminders", set_my_reminders, methods=["PUT"], name="set_my_reminders"
    )


class ReminderChoice(BaseModel):
    enabled: bool


async def get_my_reminders(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    result = await session.execute(
        text("SELECT reminder_emails FROM users WHERE id = :user_id"),
        {"user_id": str(tenant.user_id)},
    )
    return {"enabled": bool(result.scalar_one())}


async def set_my_reminders(
    request: Request,
    choice: ReminderChoice,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    await session.execute(
        text("UPDATE users SET reminder_emails = :enabled, updated_at = now() WHERE id = :user_id"),
        {"enabled": choice.enabled, "user_id": str(tenant.user_id)},
    )
    return {"enabled": choice.enabled}

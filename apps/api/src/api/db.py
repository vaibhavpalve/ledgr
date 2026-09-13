"""Database session with tenant context propagated to Postgres.

Every session sets app.current_org_id via set_config(..., true) — SET LOCAL
semantics, scoped to the transaction and cleared automatically at commit or
rollback — before any other statement runs on it. That is what the RLS
policies in apps/api/migrations/0001_tenancy_core.sql key off, and using
`true` (is_local) rather than a session-level SET is what keeps a value from
one request leaking onto the next request's queries on a pooled connection.
See docs/decisions/ADR-003-rls-enforcement.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.config import settings
from api.tenancy import TenantContext, get_tenant_context

engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)
_session_factory = async_sessionmaker(engine, expire_on_commit=False)


def get_ops_engine() -> AsyncEngine:
    """A separate engine connected as ledgr_ops (BYPASSRLS, narrowly
    granted - see ADR-003 and migrations/0002_encryption_keys.sql), for the
    scheduled batch jobs in apps/api/scripts that legitimately need
    cross-tenant access (key rotation sweeps). Never used by request-serving
    code - that always goes through get_db_session above, as ledgr_app,
    scoped to one tenant per transaction.
    """
    if not settings.ops_database_url:
        raise RuntimeError(
            "OPS_DATABASE_URL is not set - required for ledgr_ops-connected "
            "batch jobs (key rotation sweeps). See .env.example."
        )
    return create_async_engine(settings.ops_database_url, pool_pre_ping=True)


async def get_db_session(
    tenant: TenantContext = Depends(get_tenant_context),
) -> AsyncIterator[AsyncSession]:
    """Yields a session whose entire transaction is scoped to the caller's
    tenant. Depending on get_tenant_context (rather than reading
    request.state directly) means this dependency cannot be satisfied on a
    route the tenant-context middleware didn't already clear — there is no
    way to obtain a session without a verified organization_id to bind it to.
    """
    async with _session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(tenant.organization_id)},
        )
        yield session


async def get_bootstrap_db_session() -> AsyncIterator[AsyncSession]:
    """For the one family of routes that runs before any tenant exists to
    scope a session to: signup, and the sign-in paths that authenticate a
    caller from a request body rather than a bearer token
    (api.auth.routes). No app.current_org_id is set — there is genuinely
    none yet — so any RLS-protected table this session queries sees
    nothing, by the same mechanism that protects every other tenant's data.
    The signup path's own writes (organization, role_assignment) go through
    either a SECURITY DEFINER bootstrap function or a same-transaction
    set_config immediately after the organization it scopes to is created
    — see api.auth.signup.SignupService.

    These routes are, deliberately, the ones listed in
    api.tenancy.EXEMPT_PATHS: there is no tenant context for
    TenantContextMiddleware to have verified, so get_db_session (which
    requires one) cannot be their dependency.
    """
    async with _session_factory() as session, session.begin():
        yield session

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

"""Reclaim space from expired idempotency records (NFR-032).

Run on a schedule. It is NOT what makes expiry correct - the claim itself
drops an expired row for the key it is claiming, so a stale key is reclaimed
the moment it is next used, whether or not this has run. This only stops the
table growing without bound.

Must connect as a role holding EXECUTE on app.purge_expired_idempotency_keys
- ledgr_ops. ledgr_app deliberately has none: purging is cross-tenant, and a
request handler that could delete other tenants' keys is a capability no
endpoint needs.

Usage:
    IDEMPOTENCY_PURGE_URL=postgresql+asyncpg://ledgr_ops:...@host/ledgr \\
    uv run python scripts/purge_idempotency_keys.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def purge(dsn: str) -> int:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT app.purge_expired_idempotency_keys() AS deleted")
            )
            deleted = int(result.scalar_one())
            await conn.commit()
    finally:
        await engine.dispose()
    return deleted


async def main() -> int:
    dsn = os.environ.get("IDEMPOTENCY_PURGE_URL")
    if not dsn:
        print(
            "IDEMPOTENCY_PURGE_URL is not set. It must point at a connection "
            "holding EXECUTE on app.purge_expired_idempotency_keys (ledgr_ops).",
            file=sys.stderr,
        )
        return 2

    deleted = await purge(dsn)
    # Structured, and the COUNT is the point: a purge that silently deletes
    # nothing for a month is indistinguishable from one that is working, and
    # the number is the difference.
    print(
        json.dumps(
            {
                "event": "idempotency_keys_purged",
                "deleted": deleted,
                "at": datetime.now(UTC).isoformat(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

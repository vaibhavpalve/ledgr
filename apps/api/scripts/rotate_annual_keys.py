"""Scheduled job for SEC-023's "automatic key rotation at least annually":
rotates every active administration's data encryption key.

Rotation is cheap and requires no downtime (see
docs/decisions/ADR-004-envelope-encryption.md): only the DEK's wrapper
changes, and only for future writes - no existing document is re-encrypted
or becomes briefly unavailable.

Connects as ledgr_ops (BYPASSRLS, read-only on administration, read/write
only on administration_encryption_key) - the cross-tenant role from
migrations/0002_encryption_keys.sql. Intended to run on a schedule (cron /
scheduled cloud job), not interactively.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import get_ops_engine


async def main() -> None:
    engine = get_ops_engine()
    kms = build_kms()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT id FROM administration WHERE status = 'active'")
        )
        administration_ids = [row.id for row in result]

    print(f"rotating keys for {len(administration_ids)} administrations", file=sys.stderr)

    rotated = 0
    for administration_id in administration_ids:
        async with session_factory() as session, session.begin():
            repository = SqlAdministrationKeyRepository(session)
            service = EnvelopeEncryptionService(kms, repository)
            await service.rotate_key(administration_id, reason="scheduled_annual")
        rotated += 1

    print(f"rotated {rotated} administration keys", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())

"""Emergency single-tenant rotation runbook (SEC-023). Use when one
administration's DEK is suspected compromised - e.g. a leaked wrapped-key
dump, or a credential that could call unwrap for that tenant specifically.
Rotates immediately, out of band from the annual schedule.

    uv run python scripts/emergency_rotate_key.py --administration-id <uuid>

Rotation alone does not retroactively protect documents an attacker already
decrypted with the old (compromised) DEK - it only prevents that DEK from
protecting anything NEW going forward. See
docs/decisions/ADR-004-envelope-encryption.md's emergency-rotation section
for what this does and does not guarantee, and what else a real compromise
requires (revocation instead of rotation if the tenant's data itself must be
cut off; incident response per SEC-060).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import get_ops_engine


async def main(administration_id: uuid.UUID) -> None:
    engine = get_ops_engine()
    kms = build_kms()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session, session.begin():
        repository = SqlAdministrationKeyRepository(session)
        service = EnvelopeEncryptionService(kms, repository)
        new_key = await service.rotate_key(administration_id, reason="emergency")

    print(
        f"emergency-rotated administration {administration_id} to key version "
        f"{new_key.key_version}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--administration-id", required=True, type=uuid.UUID)
    args = parser.parse_args()
    asyncio.run(main(args.administration_id))

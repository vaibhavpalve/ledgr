"""SEC-023's KEK-rotation re-wrap sweep. Run after the KMS/HSM's
key-encryption key rotates to a new version - scheduled (following the
KEK's own rotation policy) or emergency (see below) - to re-wrap every
administration's DEK still wrapped under the OLD version, so that version
can eventually be disabled without breaking anything.

    uv run python scripts/rewrap_after_kek_rotation.py --old-kek-key-id <id>

No document is re-encrypted and no DEK value changes here - only which KEK
version wraps it. This is what makes KEK rotation cheap even at scale: a
sweep over small wrapped-DEK rows, never over the (potentially enormous)
document/attachment blobs themselves.

Emergency use: if the KEK itself is suspected compromised (not a single
tenant's DEK - see emergency_rotate_key.py for that), the runbook is:
  1. Rotate the KEK in the KMS (creates a new current version; Azure Key
     Vault retains the old version rather than deleting it automatically).
  2. Run this script with --old-kek-key-id set to the compromised version,
     to re-wrap every tenant's DEK onto the new version.
  3. Confirm the script's reported count matches the number of active
     administrations, then explicitly DISABLE (not merely stop using) the
     old KEK version in the KMS. Only this last step actually cuts off
     whoever had access to the compromised version - steps 1-2 alone leave
     it live and unwrap-capable.
See docs/decisions/ADR-004-envelope-encryption.md for the full runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import get_ops_engine


async def main(old_kek_key_id: str) -> None:
    engine = get_ops_engine()
    kms = build_kms()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session, session.begin():
        repository = SqlAdministrationKeyRepository(session)
        service = EnvelopeEncryptionService(kms, repository)
        count = await service.rewrap_after_kek_rotation(old_kek_key_id=old_kek_key_id)

    print(f"re-wrapped {count} administration keys off KEK {old_kek_key_id}", file=sys.stderr)
    print(
        "next: confirm this count is complete, then disable the old KEK "
        "version in the KMS to actually cut off access to it.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-kek-key-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.old_kek_key_id))

"""Records each organization's audit chain head somewhere the database
cannot reach (IAM-092).

--- Why this exists ---

Migration 0019 stops every role this application creates from editing the
audit log: no UPDATE or DELETE privilege for anyone, triggers that reject
both unconditionally, and ownership by a NOLOGIN role so the triggers cannot
be dropped by the migration role. None of that stops a Postgres SUPERUSER,
and nothing inside the database can - they may drop a trigger, change an
owner, or rewrite a row.

Hash chaining narrows that: to alter entry N a superuser must also recompute
N+1 onward. But they CAN recompute it, and they can also delete entries from
the end of a chain, which leaves a shorter chain that verifies perfectly.
tests/integration/test_audit_log.py demonstrates both.

What defeats those is a copy of the head hash and sequence number held
outside the database. A rewritten chain will not match an anchor taken
before the rewrite; a truncated one will report a lower sequence number than
the anchor. Neither is detectable without it.

So: this is not an optimisation. Until anchors are being written somewhere
the database cannot reach, IAM-092's tamper-evidence covers everyone except
whoever owns the server.

--- Where anchors should go ---

Deliberately not decided here, because it is a deployment question with real
tradeoffs. What matters is that the destination is not writable by whoever
can write the database. Reasonable choices, strongest first:

  * Azure Blob under an immutability (WORM) policy, in a different
    subscription from the database, with a retention matching IAM-093's
    seven years. PRD §13 already chose Azure Blob with immutability policies
    for documents; this is the same control applied to a much smaller object.
  * A customer-held copy, which also serves IAM-094 - a tenant who keeps
    their own anchors can verify their log without trusting LEDGR at all.
  * An append-only log in a separate account or provider.

LoggingAnchorSink below is the only implementation today. A structured log
line is a real, durable record and is better than nothing, but it is written
by the same infrastructure it is meant to check, so it does NOT satisfy the
paragraph above on its own. Wiring a WORM sink is the remaining work.

    uv run python scripts/anchor_audit_chain.py
    uv run python scripts/anchor_audit_chain.py --organization <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from api.db import get_ops_engine  # noqa: E402

logger = logging.getLogger("api.audit.anchor")


@dataclass(frozen=True, slots=True)
class Anchor:
    organization_id: uuid.UUID
    sequence_number: int
    head_hash: str
    entries: int
    taken_at: datetime

    def as_json(self) -> str:
        return json.dumps(
            {
                "organization_id": str(self.organization_id),
                "sequence_number": self.sequence_number,
                "head_hash": self.head_hash,
                "entries": self.entries,
                "taken_at": self.taken_at.isoformat(),
            },
            sort_keys=True,
        )


class AnchorSink(Protocol):
    async def store(self, anchor: Anchor) -> None: ...


class LoggingAnchorSink:
    """Real, not a stub - a structured log line is durable and greppable.
    But see the module docstring: it is written by the same infrastructure it
    checks, so it is a placeholder for a WORM destination rather than a
    substitute for one.
    """

    async def store(self, anchor: Anchor) -> None:
        logger.info("audit_chain_anchor", extra={"anchor": anchor.as_json()})
        print(anchor.as_json())


async def collect_anchors(organization_id: uuid.UUID | None = None) -> list[Anchor]:
    """Reads every chain head as ledgr_ops.

    ledgr_ops holds BYPASSRLS, which is what lets one sweep cover every
    tenant, and holds SELECT-only on audit_log - it cannot write the log it
    is anchoring, which is the property that makes the anchor worth taking.
    """
    engine = get_ops_engine()
    taken_at = datetime.now(UTC)
    anchors: list[Anchor] = []

    try:
        async with engine.connect() as conn:
            if organization_id is not None:
                organizations = [organization_id]
            else:
                result = await conn.execute(text("SELECT DISTINCT organization_id FROM audit_log"))
                organizations = [row[0] for row in result]

            for org in organizations:
                row = (
                    await conn.execute(
                        text(
                            "SELECT sequence_number, head_hash, entries "
                            "FROM app.audit_chain_head(:org)"
                        ),
                        {"org": str(org)},
                    )
                ).one()
                if not row.head_hash:
                    continue
                anchors.append(
                    Anchor(
                        organization_id=org,
                        sequence_number=int(row.sequence_number),
                        head_hash=row.head_hash,
                        entries=int(row.entries),
                        taken_at=taken_at,
                    )
                )
    finally:
        await engine.dispose()

    return anchors


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--organization",
        type=uuid.UUID,
        default=None,
        help="anchor one organization instead of every one with entries",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    sink: AnchorSink = LoggingAnchorSink()

    anchors = await collect_anchors(args.organization)
    for anchor in anchors:
        await sink.store(anchor)

    print(f"anchored {len(anchors)} chain(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

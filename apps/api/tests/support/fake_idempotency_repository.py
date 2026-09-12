"""An in-memory IdempotencyRepository that reimplements 0022's claim logic.

Same contract as the other fakes here: it decides among the four outcomes the
way `app.claim_idempotency_key` does, in the same order, so the DB-free tests
exercise real behaviour rather than a stub.

The ORDER matters and is the one thing easiest to get subtly wrong: the
fingerprint is compared BEFORE the state. A key reused for a different request
is a client bug whether or not the first one has finished, and reporting it as
"still in flight" would send the caller into a retry loop that can never
succeed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from api.idempotency import (
    ClaimResult,
    Outcome,
    RequestIdentity,
    StoredResponse,
)


@dataclass
class _Record:
    fingerprint: str
    expires_at: datetime
    state: str = "in_progress"
    response: StoredResponse | None = None


def _key_of(identity: RequestIdentity) -> tuple[str, ...]:
    """Mirrors idempotency_key_unique. All five parts - see 0022 for what each
    one prevents.
    """
    return (
        str(identity.organization_id),
        str(identity.user_id),
        identity.method,
        identity.path_template,
        identity.key,
    )


@dataclass
class InMemoryIdempotencyRepository:
    records: dict[tuple[str, ...], _Record] = field(default_factory=dict)
    #: Set by tests to move time forward without sleeping.
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    async def claim(self, identity: RequestIdentity, *, expires_at: datetime) -> ClaimResult:
        slot = _key_of(identity)
        existing = self.records.get(slot)

        # An expired record is not a record. Reclaiming rather than replaying
        # is what makes expiry mean anything - and it is the behaviour that
        # carries the honest limit: a retry after the window re-executes.
        if existing is not None and existing.expires_at <= self.now:
            del self.records[slot]
            existing = None

        if existing is None:
            self.records[slot] = _Record(fingerprint=identity.fingerprint(), expires_at=expires_at)
            return ClaimResult(Outcome.CLAIMED)

        if existing.fingerprint != identity.fingerprint():
            return ClaimResult(Outcome.MISMATCH)

        if existing.state == "in_progress":
            return ClaimResult(Outcome.IN_FLIGHT)

        return ClaimResult(Outcome.REPLAY, existing.response)

    async def complete(self, identity: RequestIdentity, response: StoredResponse) -> None:
        record = self.records.get(_key_of(identity))
        if record is None or record.state != "in_progress":
            return
        record.state = "completed"
        record.response = response

    async def release(self, identity: RequestIdentity) -> None:
        slot = _key_of(identity)
        record = self.records.get(slot)
        if record is not None and record.state == "in_progress":
            del self.records[slot]

    async def purge_expired(self) -> int:
        expired = [slot for slot, record in self.records.items() if record.expires_at < self.now]
        for slot in expired:
            del self.records[slot]
        return len(expired)

"""In-memory AuditRepository double.

Reimplements the sealing and verification logic from migration 0019 rather
than stubbing it, because the property under test IS the chain: a fake that
returned a canned "intact" would let every tamper-evidence test pass without
any tamper-evidence existing.

The field ordering, the length-prefixing and the timestamp format below all
have to match 0019 exactly. `test_hash_agreement` in
tests/integration/test_audit_log.py compares a hash computed here against
one computed by Postgres on the same values, so a drift between the two is
caught rather than silently making the fake test something else.

There is deliberately no way to edit or remove an entry through this class
either - `tamper_*` methods exist only so the verification tests have
something broken to detect, and they say so in their names.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from api.audit.log import (
    AuditCategory,
    AuditEntry,
    AuditEvent,
    ChainBreak,
    ChainHead,
)

ZERO_HASH = "0" * 64


def _field(value: str | None) -> str:
    """app.audit_field: length-prefixed so a delimiter inside a value cannot
    make two different entries hash alike.
    """
    if value is None:
        return "-1:"
    return f"{len(value)}:{value}"


def _timestamp(value: datetime) -> str:
    """to_char(... 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') at UTC."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _optional(value: object | None) -> str | None:
    return None if value is None else str(value)


def seal_payload(
    *,
    previous_hash: str,
    sequence_number: int,
    organization_id: uuid.UUID,
    administration_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    actor_type: str,
    category: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    outcome: str,
    occurred_at: datetime,
    recorded_at: datetime,
    source_ip: str | None,
    user_agent: str | None,
    correlation_id: str | None,
    detail: Mapping[str, Any],
) -> str:
    """Field order matches audit_log_seal() in 0019, and must keep matching."""
    return "".join(
        [
            _field(previous_hash),
            _field(str(sequence_number)),
            _field(str(organization_id)),
            _field(_optional(administration_id)),
            _field(_optional(actor_user_id)),
            _field(actor_type),
            _field(category),
            _field(action),
            _field(resource_type),
            _field(_optional(resource_id)),
            _field(outcome),
            _field(_timestamp(occurred_at)),
            _field(_timestamp(recorded_at)),
            _field(source_ip),
            _field(user_agent),
            _field(correlation_id),
            _field(json.dumps(dict(detail), sort_keys=True) if detail else "{}"),
        ]
    )


def seal(**kwargs: Any) -> str:
    return hashlib.sha256(seal_payload(**kwargs).encode("utf-8")).hexdigest()


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        #: The limit the last search() call actually used, so a test can
        #: assert the page cap without materialising a thousand entries.
        self.last_search_limit: int | None = None

    # --- AuditRepository --------------------------------------------------

    async def append(self, event: AuditEvent, *, recorded_at: datetime) -> AuditEntry:
        chain = [e for e in self._entries if e.organization_id == event.organization_id]
        previous = chain[-1] if chain else None

        sequence_number = (previous.sequence_number if previous else 0) + 1
        previous_hash = previous.entry_hash if previous else ZERO_HASH
        occurred_at = event.occurred_at or recorded_at

        entry_hash = seal(
            previous_hash=previous_hash,
            sequence_number=sequence_number,
            organization_id=event.organization_id,
            administration_id=event.administration_id,
            actor_user_id=event.actor_user_id,
            actor_type=event.actor_type.value,
            category=event.category.value,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome.value,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            source_ip=event.source_ip,
            user_agent=event.user_agent,
            correlation_id=event.correlation_id,
            detail=event.detail,
        )

        entry = AuditEntry(
            id=uuid.uuid4(),
            sequence_number=sequence_number,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
            organization_id=event.organization_id,
            administration_id=event.administration_id,
            actor_user_id=event.actor_user_id,
            actor_type=event.actor_type,
            category=event.category,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            source_ip=event.source_ip,
            user_agent=event.user_agent,
            correlation_id=event.correlation_id,
            detail=dict(event.detail),
        )
        self._entries.append(entry)
        return entry

    async def verify_chain(self, organization_id: uuid.UUID) -> ChainBreak | None:
        """app.verify_audit_chain: recomputes each hash from the entry's own
        stored fields, so an edit that left the hashes alone is caught too.
        """
        expected_previous = ZERO_HASH
        expected_sequence = 1

        for entry in sorted(
            (e for e in self._entries if e.organization_id == organization_id),
            key=lambda e: e.sequence_number,
        ):
            if entry.sequence_number != expected_sequence:
                return ChainBreak(
                    entry.sequence_number,
                    entry.id,
                    f"sequence gap: expected {expected_sequence}",
                )
            if entry.previous_hash != expected_previous:
                return ChainBreak(
                    entry.sequence_number,
                    entry.id,
                    "previous_hash does not match the preceding entry",
                )

            recomputed = seal(
                previous_hash=entry.previous_hash,
                sequence_number=entry.sequence_number,
                organization_id=entry.organization_id,
                administration_id=entry.administration_id,
                actor_user_id=entry.actor_user_id,
                actor_type=entry.actor_type.value,
                category=entry.category.value,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                outcome=entry.outcome.value,
                occurred_at=entry.occurred_at,
                recorded_at=entry.recorded_at,
                source_ip=entry.source_ip,
                user_agent=entry.user_agent,
                correlation_id=entry.correlation_id,
                detail=entry.detail,
            )
            if recomputed != entry.entry_hash:
                return ChainBreak(
                    entry.sequence_number,
                    entry.id,
                    "entry contents do not match its hash",
                )

            expected_previous = entry.entry_hash
            expected_sequence = entry.sequence_number + 1

        return None

    async def chain_head(self, organization_id: uuid.UUID) -> ChainHead:
        chain = sorted(
            (e for e in self._entries if e.organization_id == organization_id),
            key=lambda e: e.sequence_number,
        )
        return ChainHead(
            organization_id=organization_id,
            sequence_number=chain[-1].sequence_number if chain else 0,
            head_hash=chain[-1].entry_hash if chain else "",
            entries=len(chain),
        )

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        categories: Sequence[AuditCategory] | None = None,
        actor_user_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEntry]:
        self.last_search_limit = limit
        matches = [
            e
            for e in self._entries
            if e.organization_id == organization_id
            and (not categories or e.category in categories)
            and (actor_user_id is None or e.actor_user_id == actor_user_id)
            and (since is None or e.occurred_at >= since)
            and (until is None or e.occurred_at <= until)
        ]
        matches.sort(key=lambda e: e.sequence_number, reverse=True)
        return matches[:limit]

    # --- tampering, for the verification tests only -----------------------
    #
    # No production path can reach these: 0019 grants no UPDATE or DELETE to
    # any role and rejects both in a trigger regardless. They exist so the
    # tamper-evidence tests have something to detect, which is the only way
    # to show that detection works.

    def tamper_with_field(self, sequence_number: int, **changes: Any) -> None:
        """Edits an entry's contents and leaves its hash untouched - what an
        attacker with a write path but no understanding of the chain does.
        """
        index = self._index_of(sequence_number)
        self._entries[index] = replace(self._entries[index], **changes)

    def tamper_and_reseal(self, sequence_number: int, **changes: Any) -> None:
        """Edits an entry AND recomputes its own hash - a more careful
        attacker, who then still breaks every following entry's
        previous_hash.
        """
        index = self._index_of(sequence_number)
        entry = replace(self._entries[index], **changes)
        self._entries[index] = replace(
            entry,
            entry_hash=seal(
                previous_hash=entry.previous_hash,
                sequence_number=entry.sequence_number,
                organization_id=entry.organization_id,
                administration_id=entry.administration_id,
                actor_user_id=entry.actor_user_id,
                actor_type=entry.actor_type.value,
                category=entry.category.value,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                outcome=entry.outcome.value,
                occurred_at=entry.occurred_at,
                recorded_at=entry.recorded_at,
                source_ip=entry.source_ip,
                user_agent=entry.user_agent,
                correlation_id=entry.correlation_id,
                detail=entry.detail,
            ),
        )

    def tamper_by_deleting(self, sequence_number: int) -> None:
        """Removes an entry outright, leaving a gap in the sequence."""
        del self._entries[self._index_of(sequence_number)]

    def _index_of(self, sequence_number: int) -> int:
        for index, entry in enumerate(self._entries):
            if entry.sequence_number == sequence_number:
                return index
        raise KeyError(sequence_number)

"""The firm's preview and request (ADR-114), on in-memory fakes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from api.audit.log import ActorType, AuditEvent
from api.chasing.model import BlockedReason
from api.chasing.repository import ChaseFacts, RecordedRequest
from api.chasing.service import (
    MAX_ADMINISTRATIONS,
    NOT_FOUND,
    ChaseService,
    TooManyAdministrations,
)

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
OWNER_ORG = uuid.uuid4()
FIRM_ORG = uuid.uuid4()
USER = uuid.uuid4()


class FakeStore:
    def __init__(
        self, counts: dict[uuid.UUID, int], facts: dict[uuid.UUID, ChaseFacts] | None = None
    ) -> None:
        self.counts = counts
        self.fact_rows = facts or {}
        self.requests: list[tuple[uuid.UUID, int]] = []
        self.asked: list[list[uuid.UUID]] = []

    async def missing_counts(self, administration_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        self.asked.append(list(administration_ids))
        return {a: self.counts[a] for a in administration_ids if a in self.counts}

    async def facts(self, administration_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ChaseFacts]:
        return {a: self.fact_rows[a] for a in administration_ids if a in self.fact_rows}

    async def record_request(
        self, administration_id: uuid.UUID, *, missing_count: int, user_id: uuid.UUID
    ) -> RecordedRequest | None:
        self.requests.append((administration_id, missing_count))
        return RecordedRequest(request_id=uuid.uuid4(), organization_id=OWNER_ORG)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


async def test_preview_reports_each_client_and_why_it_cannot_be_chased() -> None:
    ready, empty, recent, nobody, uncounted = _ids(5)
    store = FakeStore(
        {ready: 4, recent: 2, nobody: 1, uncounted: 7},
        {
            ready: ChaseFacts(recipient_count=2, last_chased_at=NOW - timedelta(days=8)),
            recent: ChaseFacts(recipient_count=1, last_chased_at=NOW - timedelta(hours=3)),
            nobody: ChaseFacts(recipient_count=0),
        },
    )
    portfolio = {a: f"Client {i}" for i, a in enumerate([ready, empty, recent, nobody, uncounted])}
    items = await ChaseService(store, FakeAudit()).preview(
        requested=[ready, empty, recent, nobody, uncounted], portfolio=portfolio, now=NOW
    )
    assert [(i.administration_id, i.missing_count, i.blocked_reason) for i in items] == [
        (ready, 4, None),
        (empty, 0, BlockedReason.NOTHING_MISSING),
        (recent, 2, BlockedReason.CHASED_RECENTLY),
        (nobody, 1, BlockedReason.NO_RECIPIENT),
        (uncounted, 7, None),
    ]
    assert items[0].display_name == "Client 0"
    assert items[0].last_chased_at == NOW - timedelta(days=8)
    assert items[4].recipient_count is None


async def test_ids_outside_the_portfolio_are_never_read() -> None:
    mine, theirs = _ids(2)
    store = FakeStore({mine: 1, theirs: 9})
    service = ChaseService(store, FakeAudit())
    items = await service.preview(requested=[theirs, mine], portfolio={mine: "M"}, now=NOW)
    assert [i.administration_id for i in items] == [mine]
    assert store.asked == [[mine]]

    outcome = await service.request(
        requested=[theirs, mine],
        portfolio={mine: "M"},
        user_id=USER,
        acting_organization_id=FIRM_ORG,
        now=NOW,
    )
    assert outcome.sent == 1
    assert [(s.administration_id, s.reason) for s in outcome.skipped] == [(theirs, NOT_FOUND)]
    assert [a for a, _ in store.requests] == [mine]


async def test_a_request_records_and_audits_only_the_unblocked() -> None:
    go, empty, recent = _ids(3)
    store = FakeStore(
        {go: 3, recent: 1},
        {recent: ChaseFacts(last_requested_at=NOW - timedelta(hours=1))},
    )
    audit = FakeAudit()
    outcome = await ChaseService(store, audit).request(
        requested=[go, empty, recent, go],
        portfolio={go: "Go", empty: "Empty", recent: "Recent"},
        user_id=USER,
        acting_organization_id=FIRM_ORG,
        now=NOW,
    )
    assert outcome.sent == 1
    assert {(s.administration_id, s.reason) for s in outcome.skipped} == {
        (empty, "nothing_missing"),
        (recent, "chased_recently"),
    }
    assert store.requests == [(go, 3)]
    [event] = audit.events
    assert event.organization_id == OWNER_ORG  # the client's own log (ADR-112)
    assert event.administration_id == go
    assert event.actor_type is ActorType.USER and event.actor_user_id == USER
    assert event.action == "request_receipt_chase"
    assert dict(event.detail) == {
        "missing_count": 3,
        "acting_organization_id": str(FIRM_ORG),
    }


async def test_one_call_takes_at_most_the_worklists_page() -> None:
    ids = _ids(MAX_ADMINISTRATIONS + 1)
    with pytest.raises(TooManyAdministrations):
        await ChaseService(FakeStore({}), FakeAudit()).preview(
            requested=ids, portfolio=dict.fromkeys(ids, "x"), now=NOW
        )

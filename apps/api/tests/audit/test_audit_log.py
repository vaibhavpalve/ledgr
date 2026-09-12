"""IAM-090 through IAM-093: the audit log.

The immutability half of IAM-092 cannot be tested here - it is enforced by
privileges and triggers, so proving it needs a real Postgres. Those tests are
in tests/integration/test_audit_log.py and are the ones that answer "what
stops a migration or support tooling editing this".

What IS testable without a database is the other half: tamper-EVIDENCE. Every
test below that breaks the chain does so by reaching into the fake's storage
directly, which is exactly what an attacker who got past the database
controls would have. The point is that they are still caught.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.audit.log import (
    ActorType,
    AuditCategory,
    AuditEvent,
    AuditLog,
    AuditOutcome,
)
from tests.support.fake_audit_repository import (
    ZERO_HASH,
    InMemoryAuditRepository,
    seal,
)

ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
ALICE = uuid.uuid4()
BOB = uuid.uuid4()


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _log() -> tuple[AuditLog, InMemoryAuditRepository, _FakeClock]:
    repository = InMemoryAuditRepository()
    clock = _FakeClock()
    return AuditLog(repository, clock=clock), repository, clock


def _event(**over: object) -> AuditEvent:
    defaults: dict[str, object] = {
        "organization_id": ORG,
        "category": AuditCategory.POSTING,
        "action": "post",
        "resource_type": "journal_entry",
        "outcome": AuditOutcome.SUCCESS,
        "actor_type": ActorType.USER,
        "actor_user_id": ALICE,
    }
    defaults.update(over)
    return AuditEvent(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# IAM-091: the field set
# ===========================================================================


async def test_an_entry_records_every_field_iam_091_names() -> None:
    log, _, clock = _log()
    resource = uuid.uuid4()
    administration = uuid.uuid4()

    entry = await log.record(
        _event(
            administration_id=administration,
            resource_id=resource,
            source_ip="203.0.113.7",
            user_agent="Mozilla/5.0",
            correlation_id="req-abc-123",
            detail={"amount": "1200.00"},
        )
    )

    assert entry.actor_user_id == ALICE  # actor
    assert entry.actor_type is ActorType.USER  # actor type
    assert entry.organization_id == ORG  # tenant
    assert entry.administration_id == administration
    assert entry.resource_type == "journal_entry"  # resource
    assert entry.resource_id == resource
    assert entry.action == "post"  # action
    assert entry.outcome is AuditOutcome.SUCCESS  # outcome
    assert entry.occurred_at.tzinfo is not None  # timestamp, UTC-aware
    assert entry.source_ip == "203.0.113.7"  # source IP
    assert entry.user_agent == "Mozilla/5.0"  # user agent
    assert entry.correlation_id == "req-abc-123"  # correlation ID
    assert entry.detail == {"amount": "1200.00"}
    assert entry.recorded_at == clock.now


async def test_occurred_at_and_recorded_at_are_separate() -> None:
    """A single timestamp cannot distinguish an event recorded late from one
    backdated on the way in, and a log whose job is being trusted should be
    able to tell those apart.
    """
    log, _, clock = _log()
    happened = clock.now - timedelta(hours=3)

    entry = await log.record(_event(occurred_at=happened))

    assert entry.occurred_at == happened
    assert entry.recorded_at == clock.now


def test_an_entry_attributed_to_a_user_must_name_one() -> None:
    """The shape that makes a log useless in review: it looks attributed and
    names nobody.
    """
    with pytest.raises(ValueError, match="requires an actor_user_id"):
        _event(actor_type=ActorType.USER, actor_user_id=None)


def test_a_system_actor_needs_no_user() -> None:
    event = _event(actor_type=ActorType.SYSTEM, actor_user_id=None)

    assert event.actor_user_id is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_entry_needs_an_action_and_a_resource_type(blank: str) -> None:
    with pytest.raises(ValueError, match="action"):
        _event(action=blank)
    with pytest.raises(ValueError, match="resource type"):
        _event(resource_type=blank)


# ===========================================================================
# IAM-090: the coverage list
# ===========================================================================


def test_every_category_iam_090_names_exists() -> None:
    """One member per clause of IAM-090's sentence. A category the
    requirement names but the enum lacks is a gap; one the enum has that the
    requirement does not name is scope creep.
    """
    assert {c.value for c in AuditCategory} == {
        "authentication",
        "permission_change",
        "financial_read",
        "export",
        "posting",
        "approval",
        "filing",
        "configuration",
        "support_access",
    }


def test_support_access_is_its_own_category() -> None:
    """IAM-090 names support access separately from ordinary activity, and a
    tenant reviewing their log needs to see which was which.
    """
    assert AuditCategory.SUPPORT_ACCESS in AuditCategory
    assert ActorType.SUPPORT in ActorType


def test_a_denied_action_is_not_the_same_as_a_failed_one() -> None:
    """A denied action is a security signal; a failed one is usually
    operational. Collapsing them would make the log worse at the thing it
    exists for.
    """
    assert AuditOutcome.DENIED is not AuditOutcome.FAILURE


# ===========================================================================
# IAM-092: tamper-evidence
# ===========================================================================


async def test_the_first_entry_chains_from_a_zero_hash() -> None:
    log, _, _ = _log()

    entry = await log.record(_event())

    assert entry.sequence_number == 1
    assert entry.previous_hash == ZERO_HASH


async def test_each_entry_chains_to_the_one_before_it() -> None:
    log, _, _ = _log()

    first = await log.record(_event())
    second = await log.record(_event(action="reverse"))

    assert second.sequence_number == 2
    assert second.previous_hash == first.entry_hash


async def test_an_untouched_chain_verifies() -> None:
    log, _, _ = _log()
    for _ in range(5):
        await log.record(_event())

    result = await log.verify(ORG)

    assert result.intact
    assert result.entries == 5


async def test_editing_an_entry_breaks_verification() -> None:
    """The basic attack: change what the log says happened."""
    log, repository, _ = _log()
    for _ in range(3):
        await log.record(_event())

    repository.tamper_with_field(2, outcome=AuditOutcome.DENIED)
    result = await log.verify(ORG)

    assert not result.intact
    assert result.break_found is not None
    assert result.break_found.sequence_number == 2
    assert "do not match its hash" in result.break_found.reason


async def test_editing_an_entry_and_resealing_it_still_breaks_the_chain() -> None:
    """A more careful attacker recomputes the edited entry's own hash. The
    entry then passes its own check and every entry after it fails, because
    their previous_hash still refers to the original.
    """
    log, repository, _ = _log()
    for _ in range(4):
        await log.record(_event())

    repository.tamper_and_reseal(2, action="something else entirely")
    result = await log.verify(ORG)

    assert not result.intact
    assert result.break_found is not None
    assert result.break_found.sequence_number == 3
    assert "previous_hash" in result.break_found.reason


async def test_removing_an_entry_breaks_verification() -> None:
    """Deletion is the attack that a hash-only check would miss - the
    remaining entries are all individually valid. The sequence gap is what
    catches it.
    """
    log, repository, _ = _log()
    for _ in range(4):
        await log.record(_event())

    repository.tamper_by_deleting(2)
    result = await log.verify(ORG)

    assert not result.intact
    assert result.break_found is not None
    assert "sequence gap" in result.break_found.reason


async def test_removing_the_most_recent_entry_is_not_detected_by_the_chain_alone() -> None:
    """An honest negative result, and the reason app.audit_chain_head exists.

    Truncating the END of a chain leaves a shorter but internally consistent
    chain. Only an external anchor recording a sequence number higher than
    what remains can catch it - which is exactly what
    scripts/anchor_audit_chain.py is for, and why the module docstring says
    an unanchored chain detects everyone except whoever owns the server.
    """
    log, repository, _ = _log()
    for _ in range(4):
        await log.record(_event())
    anchored = await log.head(ORG)

    repository.tamper_by_deleting(4)

    assert (await log.verify(ORG)).intact, "the chain itself cannot see this"
    # The anchor can.
    assert (await log.head(ORG)).sequence_number < anchored.sequence_number


async def test_a_detail_change_is_detected() -> None:
    """`detail` is in the hash, so event-specific context cannot be edited
    either - an amount or a filing reference is often the thing worth
    altering.
    """
    log, repository, _ = _log()
    await log.record(_event(detail={"amount": "1200.00"}))

    repository.tamper_with_field(1, detail={"amount": "12.00"})

    assert not (await log.verify(ORG)).intact


async def test_a_timestamp_change_is_detected() -> None:
    log, repository, _ = _log()
    await log.record(_event())

    repository.tamper_with_field(1, occurred_at=datetime(2020, 1, 1, tzinfo=UTC))

    assert not (await log.verify(ORG)).intact


def test_two_entries_cannot_collide_by_splitting_a_value_differently() -> None:
    """Why app.audit_field length-prefixes instead of concatenating.

    Without it, adjacent fields run together: an action of "post" with a
    resource type of "entry" would hash identically to an action of "poste"
    with a resource type of "ntry". Academic as an attack, but the entire
    value of this table rests on two different entries never hashing alike,
    so it is not a property to leave to chance.
    """
    common: dict[str, object] = {
        "previous_hash": ZERO_HASH,
        "sequence_number": 1,
        "organization_id": ORG,
        "administration_id": None,
        "actor_user_id": ALICE,
        "actor_type": "user",
        "category": "posting",
        "resource_id": None,
        "outcome": "success",
        "occurred_at": datetime(2026, 6, 1, tzinfo=UTC),
        "recorded_at": datetime(2026, 6, 1, tzinfo=UTC),
        "source_ip": None,
        "user_agent": None,
        "correlation_id": None,
        "detail": {},
    }

    split_one = seal(action="post", resource_type="entry", **common)  # type: ignore[arg-type]
    split_two = seal(action="poste", resource_type="ntry", **common)  # type: ignore[arg-type]

    assert split_one != split_two


def test_the_same_ambiguity_across_the_free_text_fields() -> None:
    """user_agent and correlation_id are adjacent, caller-supplied and
    unconstrained - the pair most likely to carry a value chosen to collide.
    """
    common: dict[str, object] = {
        "previous_hash": ZERO_HASH,
        "sequence_number": 1,
        "organization_id": ORG,
        "administration_id": None,
        "actor_user_id": ALICE,
        "actor_type": "user",
        "category": "posting",
        "action": "post",
        "resource_type": "journal_entry",
        "resource_id": None,
        "outcome": "success",
        "occurred_at": datetime(2026, 6, 1, tzinfo=UTC),
        "recorded_at": datetime(2026, 6, 1, tzinfo=UTC),
        "source_ip": None,
        "detail": {},
    }

    one = seal(user_agent="abc", correlation_id="def", **common)  # type: ignore[arg-type]
    two = seal(user_agent="ab", correlation_id="cdef", **common)  # type: ignore[arg-type]

    assert one != two


async def test_verification_reports_only_the_first_break() -> None:
    """Once a link is broken every entry after it fails too, so a list would
    be one real finding followed by noise.
    """
    log, repository, _ = _log()
    for _ in range(5):
        await log.record(_event())

    repository.tamper_with_field(2, action="edited")
    result = await log.verify(ORG)

    assert result.break_found is not None
    assert result.break_found.sequence_number == 2


# ===========================================================================
# Chains are per organization
# ===========================================================================


async def test_each_organization_has_its_own_chain() -> None:
    log, _, _ = _log()

    a = await log.record(_event(organization_id=ORG))
    b = await log.record(_event(organization_id=OTHER_ORG))

    assert a.sequence_number == 1
    assert b.sequence_number == 1
    assert b.previous_hash == ZERO_HASH


async def test_tampering_with_one_tenants_chain_leaves_the_others_intact() -> None:
    """A per-organization chain means a break is attributable to one tenant's
    log rather than invalidating every tenant's at once.
    """
    log, repository, _ = _log()
    await log.record(_event(organization_id=ORG))
    await log.record(_event(organization_id=OTHER_ORG))
    await log.record(_event(organization_id=ORG))

    repository.tamper_with_field(1, action="edited")

    assert not (await log.verify(ORG)).intact
    assert (await log.verify(OTHER_ORG)).intact


async def test_the_head_is_what_an_anchor_stores() -> None:
    log, _, _ = _log()
    for _ in range(3):
        last = await log.record(_event())

    head = await log.head(ORG)

    assert head.sequence_number == 3
    assert head.head_hash == last.entry_hash
    assert head.entries == 3


async def test_an_empty_chain_has_an_empty_head() -> None:
    log, _, _ = _log()

    head = await log.head(ORG)

    assert head.is_empty
    assert (await log.verify(ORG)).intact, "nothing to tamper with is not tampering"


# ===========================================================================
# IAM-094's read side
# ===========================================================================


async def test_search_filters_by_category_actor_and_time() -> None:
    log, _, clock = _log()
    await log.record(_event(category=AuditCategory.POSTING, actor_user_id=ALICE))
    clock.advance(days=1)
    await log.record(_event(category=AuditCategory.EXPORT, actor_user_id=BOB))
    clock.advance(days=1)
    await log.record(_event(category=AuditCategory.EXPORT, actor_user_id=ALICE))

    by_category = await log.search(organization_id=ORG, categories=[AuditCategory.EXPORT])
    by_actor = await log.search(organization_id=ORG, actor_user_id=BOB)

    assert len(by_category) == 2
    assert len(by_actor) == 1


async def test_search_never_crosses_a_tenant_boundary() -> None:
    log, _, _ = _log()
    await log.record(_event(organization_id=ORG))
    await log.record(_event(organization_id=OTHER_ORG))

    assert len(await log.search(organization_id=ORG)) == 1


async def test_search_returns_the_most_recent_first() -> None:
    log, _, _ = _log()
    for _ in range(3):
        await log.record(_event())

    entries = await log.search(organization_id=ORG)

    assert [e.sequence_number for e in entries] == [3, 2, 1]


async def test_search_caps_the_page_size() -> None:
    """An unbounded export of a seven-year log is a way to take the database
    down by asking politely.

    Asserts the limit that actually reached the repository, not the number of
    rows returned - with a handful of entries seeded, an uncapped query
    returns the same few and the cap would go untested.
    """
    log, repository, _ = _log()
    await log.record(_event())

    await log.search(organization_id=ORG, limit=10_000)

    assert repository.last_search_limit == 1000


async def test_a_smaller_requested_page_is_honoured() -> None:
    log, repository, _ = _log()

    await log.search(organization_id=ORG, limit=25)

    assert repository.last_search_limit == 25

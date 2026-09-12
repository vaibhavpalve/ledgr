"""The ledger's invariants, against the in-memory fake (PRD §6.2).

These run everywhere, on every commit, with no database. Their DB-gated twin
- tests/integration/test_ledger_invariants.py - runs the SAME case table
against real Postgres, which is where the guarantee actually lives. This file
proves the service and the fake agree with the requirement; that one proves
the database does.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from api.audit.log import AuditCategory, AuditLog
from api.ledger import (
    AlreadyReversed,
    ControlKind,
    EntryInput,
    LedgerError,
    LedgerService,
    LineInput,
    UnbalancedEntry,
)
from tests.ledger.cases import REJECTION_CASES, TEN, entry
from tests.ledger.fake_world import build_fake_world
from tests.ledger.world import World
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_ledger_repository import InMemoryLedgerRepository


async def _service() -> tuple[LedgerService, InMemoryLedgerRepository, World, AuditLog]:
    repo, world = await build_fake_world()
    audit = AuditLog(InMemoryAuditRepository())
    return LedgerService(repo, audit), repo, world, audit


# ===========================================================================
# The table: everything the ledger must refuse
# ===========================================================================


@pytest.mark.parametrize("case", REJECTION_CASES, ids=lambda c: c.name)
async def test_the_ledger_refuses(case) -> None:  # type: ignore[no-untyped-def]
    service, _, world, _ = await _service()

    with pytest.raises(LedgerError) as raised:
        await service.post(case.build(world))

    assert case.expect in str(raised.value), (
        f"{case.requirement}: the rejection did not mention {case.expect!r}. "
        f"Got: {raised.value}. A case that is refused for the wrong reason - a "
        f"bad id rejected as 'does not exist' rather than as the violation "
        f"under test - passes without testing anything."
    )


async def test_every_requirement_in_scope_has_at_least_one_rejection_case() -> None:
    """A coverage check on the table itself.

    Without it, deleting the last FR-GL-006 case would leave the suite green
    and the requirement untested - the failure mode the audit and isolation
    coverage checks were both built to catch elsewhere in this codebase.
    """
    covered = {case.requirement for case in REJECTION_CASES}
    assert covered >= {
        "FR-GL-001",
        "FR-GL-002",
        "FR-GL-005",
        "FR-GL-006",
        "FR-GL-007",
    }


# ===========================================================================
# FR-GL-001
# ===========================================================================


async def test_a_balanced_entry_posts() -> None:
    service, _, world, _ = await _service()

    posted = await service.post(entry(world))

    assert posted.entry_number == 1
    assert len(posted.lines) == 2
    assert sum(line.debit for line in posted.lines) == Decimal("10.00")
    assert sum(line.credit for line in posted.lines) == Decimal("10.00")


async def test_an_entry_with_no_lines_is_refused() -> None:
    """SUM() over no rows is NULL and NULL <> 0 is not TRUE, so a balance
    check written without a line count waves an empty entry straight through.
    Both the SQL trigger and the fake check emptiness separately; this is the
    test that would catch either one dropping it.
    """
    service, _, world, _ = await _service()

    with pytest.raises(UnbalancedEntry) as raised:
        await service.post(entry(world, lines=[]))

    assert "no lines" in str(raised.value)


async def test_amounts_stay_decimal_through_a_round_trip() -> None:
    """NFR-031. 4335.09 + 2964.61 is 7299.700000000001 in binary floating
    point; in Decimal it is exactly 7299.70. If a float ever entered the path,
    this is the pair that would show it.
    """
    service, _, world, _ = await _service()

    posted = await service.post(
        entry(
            world,
            lines=[
                LineInput(account_id=world.cash_account_id, debit=Decimal("4335.09")),
                LineInput(account_id=world.cash_account_id, debit=Decimal("2964.61")),
                LineInput(account_id=world.revenue_account_id, credit=Decimal("7299.70")),
            ],
        )
    )

    total = sum(line.debit for line in posted.lines)
    assert total == Decimal("7299.70")
    assert str(total) == "7299.70"


# ===========================================================================
# FR-GL-013: gapless numbering
# ===========================================================================


async def test_numbering_is_sequential_per_journal_and_year() -> None:
    service, repo, world, _ = await _service()

    numbers = [(await service.post(entry(world))).entry_number for _ in range(5)]

    assert numbers == [1, 2, 3, 4, 5]
    assert (
        await service.numbering_gaps(
            journal_id=world.journal_id, fiscal_year_id=world.fiscal_year_id
        )
        == []
    )


async def test_a_rejected_post_does_not_consume_a_number() -> None:
    """The whole reason FR-GL-013 uses an allocator table rather than a
    Postgres SEQUENCE. A sequence keeps its consumed value across a rollback
    and leaves a hole; an inspector reading the journal sees entry 1 followed
    by entry 3 and asks what was deleted.
    """
    service, _, world, _ = await _service()

    first = await service.post(entry(world))
    with pytest.raises(LedgerError):
        await service.post(
            entry(
                world,
                lines=[
                    LineInput(account_id=world.cash_account_id, debit=TEN),
                    LineInput(account_id=world.revenue_account_id, credit=Decimal("9.00")),
                ],
            )
        )
    second = await service.post(entry(world))

    assert (first.entry_number, second.entry_number) == (1, 2)
    assert (
        await service.numbering_gaps(
            journal_id=world.journal_id, fiscal_year_id=world.fiscal_year_id
        )
        == []
    )


async def test_each_journal_has_its_own_series() -> None:
    service, repo, world, _ = await _service()
    from api.ledger.model import JournalType

    other = await service.create_journal(
        administration_id=world.administration_id,
        code="VRK",
        name="Verkoop",
        journal_type=JournalType.SALES,
    )

    first = await service.post(entry(world))
    second = await service.post(entry(world, journal_id=other.id))

    assert (first.entry_number, second.entry_number) == (1, 1)


async def test_the_gap_report_finds_a_hole_when_one_exists() -> None:
    """The allocator makes gaps impossible, so the only way to test the report
    is to put a hole in by hand. Without this, `numbering_gaps` returning []
    unconditionally - a `return []` stub - would pass every other test here.
    """
    service, repo, world, _ = await _service()

    for _ in range(3):
        await service.post(entry(world))
    victim = next(e for e in repo.entries.values() if e.entry_number == 2)
    del repo.entries[victim.id]

    gaps = await service.numbering_gaps(
        journal_id=world.journal_id, fiscal_year_id=world.fiscal_year_id
    )

    assert gaps == [2]


# ===========================================================================
# FR-GL-003: immutability and reversal
# ===========================================================================


async def test_the_repository_offers_no_way_to_change_a_posting() -> None:
    """Asserted by construction, the same way tests/integration's audit suite
    asserts it: there is no update, delete, purge or upsert method to call.

    This is the application interface half of CMP-009's "impossible through
    any interface". The database half - withheld privileges and RAISE
    triggers - is in tests/integration/test_ledger_immutability.py.
    """
    from api.ledger.repository import SqlLedgerRepository

    forbidden = {"update", "delete", "purge", "upsert", "amend", "edit", "remove"}
    offered = {name for name in dir(SqlLedgerRepository) if not name.startswith("_")}

    assert not (offered & forbidden), (
        f"SqlLedgerRepository offers {offered & forbidden}. No role holds the "
        "privilege to execute any of those against a posting table, so a method "
        "for one would be a lie about what the ledger can do."
    )


async def test_a_reversal_mirrors_the_original() -> None:
    service, _, world, _ = await _service()

    original = await service.post(
        entry(
            world,
            lines=[
                LineInput(account_id=world.cash_account_id, debit=TEN),
                LineInput(account_id=world.revenue_account_id, credit=TEN),
            ],
        )
    )

    reversal = await service.reverse(
        entry_id=original.id,
        actor_user_id=world.actor_user_id,
        period_id=world.open_period_id,
        entry_date=world.entry_date,
        description="correction",
    )

    by_account = {line.account_id: (line.debit, line.credit) for line in reversal.lines}
    assert by_account[world.cash_account_id] == (Decimal("0.00"), TEN)
    assert by_account[world.revenue_account_id] == (TEN, Decimal("0.00"))
    assert reversal.reverses_entry_id == original.id


async def test_the_original_is_untouched_by_its_reversal() -> None:
    """FR-GL-003's actual claim. A "correction" that edited the original and
    also wrote a reversal would pass a test that only checked the reversal.
    """
    service, _, world, _ = await _service()

    original = await service.post(entry(world))
    before = (
        original.entry_number,
        original.description,
        tuple((line.account_id, line.debit, line.credit) for line in original.lines),
    )

    await service.reverse(
        entry_id=original.id,
        actor_user_id=world.actor_user_id,
        period_id=world.open_period_id,
        entry_date=world.entry_date,
        description="correction",
    )

    after = await service.entry(original.id)
    assert after is not None
    assert (
        after.entry_number,
        after.description,
        tuple((line.account_id, line.debit, line.credit) for line in after.lines),
    ) == before


async def test_an_entry_cannot_be_reversed_twice() -> None:
    service, _, world, _ = await _service()

    original = await service.post(entry(world))
    await service.reverse(
        entry_id=original.id,
        actor_user_id=world.actor_user_id,
        period_id=world.open_period_id,
        entry_date=world.entry_date,
        description="correction",
    )

    with pytest.raises(AlreadyReversed):
        await service.reverse(
            entry_id=original.id,
            actor_user_id=world.actor_user_id,
            period_id=world.open_period_id,
            entry_date=world.entry_date,
            description="correction again",
        )


async def test_a_reversal_that_does_not_mirror_is_refused() -> None:
    """The service builds the mirror itself, so this reaches past it and posts
    a hand-built "reversal" straight at the repository - the shape a second
    write path, or a caller with a database connection, would produce.
    """
    service, repo, world, _ = await _service()

    original = await service.post(entry(world))

    with pytest.raises(LedgerError) as raised:
        await repo.post(
            EntryInput(
                administration_id=world.administration_id,
                journal_id=world.journal_id,
                period_id=world.open_period_id,
                entry_date=world.entry_date,
                description="not really a reversal",
                lines=[
                    LineInput(account_id=world.cash_account_id, credit=Decimal("9.00")),
                    LineInput(account_id=world.revenue_account_id, debit=Decimal("9.00")),
                ],
                source_system="pytest",
                reverses_entry_id=original.id,
            )
        )

    assert "mirror" in str(raised.value)


async def test_a_reversal_may_land_in_a_different_period() -> None:
    """Not a workaround for the lock - the correct treatment. By the time an
    error is found the original's period is usually closed, and FR-GL-007
    forbids reopening it.
    """
    service, repo, world, _ = await _service()

    original = await service.post(entry(world))
    later = repo.add_period(
        repo.fiscal_years[world.fiscal_year_id],
        period_number=4,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
    )
    repo.periods[world.open_period_id].status = "locked"

    reversal = await service.reverse(
        entry_id=original.id,
        actor_user_id=world.actor_user_id,
        period_id=later.id,
        entry_date=date(2026, 4, 15),
        description="correction in a later period",
    )

    assert reversal.period_id == later.id


# ===========================================================================
# FR-GL-006: the sub-ledger reconciles by construction
# ===========================================================================


async def test_a_control_account_posts_through_its_subledger() -> None:
    service, _, world, _ = await _service()

    await service.post(
        entry(
            world,
            lines=[
                LineInput(
                    account_id=world.receivables_control_id,
                    debit=Decimal("121.00"),
                    subledger_party_id=world.customer_party_id,
                ),
                LineInput(account_id=world.revenue_account_id, credit=Decimal("121.00")),
            ],
        )
    )

    balances = await service.subledger_balance(
        administration_id=world.administration_id,
        control_kind=ControlKind.ACCOUNTS_RECEIVABLE,
    )
    assert [(row.party_name, row.balance) for row in balances] == [
        ("Jansen B.V.", Decimal("121.00"))
    ]


async def test_the_control_account_always_equals_its_subledger() -> None:
    """FR-GL-006's "reconciled continuously", which here is not a job that
    runs but a property of the schema: the sub-ledger IS the control account's
    own lines grouped by party, so there is no second number to drift.
    """
    service, _, world, _ = await _service()

    for amount in ("121.00", "60.50", "1000.00"):
        await service.post(
            entry(
                world,
                lines=[
                    LineInput(
                        account_id=world.receivables_control_id,
                        debit=Decimal(amount),
                        subledger_party_id=world.customer_party_id,
                    ),
                    LineInput(account_id=world.revenue_account_id, credit=Decimal(amount)),
                ],
            )
        )

    rows = await service.reconciliation(administration_id=world.administration_id)
    receivables = next(row for row in rows if row.control_kind is ControlKind.ACCOUNTS_RECEIVABLE)

    assert receivables.control_balance == Decimal("1181.50")
    assert receivables.subledger_balance == receivables.control_balance
    assert receivables.difference == 0
    assert receivables.reconciled


# ===========================================================================
# NFR-032: idempotency
# ===========================================================================


async def test_a_retry_with_the_same_key_does_not_double_post() -> None:
    service, _, world, _ = await _service()

    first = await service.post(entry(world, idempotency_key="req-1"))
    second = await service.post(entry(world, idempotency_key="req-1"))

    assert first.id == second.id
    assert first.entry_number == second.entry_number


async def test_a_retry_does_not_consume_a_number() -> None:
    """The check runs before the allocator, so a retried request leaves the
    series exactly where it was. If it ran after, retries would punch holes in
    a numbering series that FR-GL-013 requires to be gapless.
    """
    service, _, world, _ = await _service()

    await service.post(entry(world, idempotency_key="req-1"))
    await service.post(entry(world, idempotency_key="req-1"))
    third = await service.post(entry(world, idempotency_key="req-2"))

    assert third.entry_number == 2


# ===========================================================================
# IAM-090: postings are audited
# ===========================================================================


async def _postings(audit: AuditLog, repo: InMemoryLedgerRepository) -> list[object]:
    """Oldest first. AuditLog.search returns newest first, which is right for
    a UI and wrong for asserting the order things happened in.
    """
    found = await audit.search(
        organization_id=repo.organization_id, categories=[AuditCategory.POSTING]
    )
    return list(reversed(found))


async def test_posting_writes_an_audit_entry() -> None:
    """AuditCategory.POSTING has had no producer since 0019 shipped. This is
    it.
    """
    service, repo, world, audit = await _service()

    posted = await service.post(entry(world), actor_user_id=uuid.uuid4())

    recorded = await _postings(audit, repo)
    assert len(recorded) == 1
    assert recorded[0].resource_id == posted.id  # type: ignore[attr-defined]
    assert recorded[0].action == "post_journal_entry"  # type: ignore[attr-defined]
    # Strings, not floats: the detail is inside the audit hash chain, and a
    # float there would make an entry's hash depend on IEEE 754 rounding.
    assert recorded[0].detail["total_debit"] == "10.00"  # type: ignore[attr-defined]


async def test_reversing_writes_its_own_audit_entry() -> None:
    service, repo, world, audit = await _service()

    original = await service.post(entry(world))
    await service.reverse(
        entry_id=original.id,
        actor_user_id=world.actor_user_id,
        period_id=world.open_period_id,
        entry_date=world.entry_date,
        description="correction",
    )

    actions = [e.action for e in await _postings(audit, repo)]  # type: ignore[attr-defined]
    assert actions == ["post_journal_entry", "reverse_journal_entry"]


async def test_a_refused_posting_writes_no_audit_entry() -> None:
    """A log recording postings that never happened is worse than no log: an
    inspector reconciling the audit trail against the ledger would find
    entries with no counterpart and no way to tell which were refusals.
    """
    service, repo, world, audit = await _service()

    with pytest.raises(LedgerError):
        await service.post(entry(world, period_id=world.locked_period_id))

    assert await _postings(audit, repo) == []

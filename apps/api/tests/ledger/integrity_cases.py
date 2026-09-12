"""Every deviation the integrity job must catch, as data (NFR-033).

A check that has only ever been run against healthy books has not been tested.
It returns zero rows on a correct ledger and it would return zero rows if it
were `SELECT ... WHERE false`, and nothing in a green suite distinguishes the
two. So each case here BREAKS the ledger in a specific way and names the
deviation key that must come back.

Each case carries two corruptions of the same shape:

    corrupt_fake  mutates InMemoryLedgerRepository's rows directly
    corrupt_sql   statements run against Postgres with the append-only
                  triggers disarmed, which needs a superuser - see
                  tests/integration/test_ledger_integrity.py

tests/ledger/test_integrity.py runs the first, on every `make test-api`.
tests/integration/test_ledger_integrity.py runs the second, against a real
database. Both assert the same `expect` set, which is what keeps
tests/support/fake_integrity_repository.py and migration 0023 saying the same
thing about the same books.

--- Reaching these states at all ---

Every corruption below is refused by the ledger through its own API. That is
the point, and it is why they are written as raw mutations: the states this
job exists to detect are exactly the states no supported write path can
produce. They arrive from a superuser with triggers disabled, a restore of a
backup taken mid-transaction, a replication failover that lost the tail of a
series, or a future migration that drops a guard. 0019 makes the same argument
about the audit log, and
tests/integration/test_audit_tamper_evidence.py demonstrates the superuser
half of it in detail.

--- `expect` is a set, and it is a subset assertion ---

Corrupting one row often violates two requirements at once, and the job is
supposed to say so. Editing a posted amount unbalances both its entry
(FR-GL-001, per entry) and the fiscal year it sits in (FR-GL-001, aggregate);
deleting an entry header both orphans its lines and puts a hole in the
numbering series. Each case lists everything that MUST be reported. A case
that also surfaces something else is not a failure - a report that says more
about a broken ledger than the test predicted is not a regression - but a
missing key is.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from api.ledger.model import EntryInput, LineInput, PostedEntry
from tests.ledger.cases import entry
from tests.ledger.world import World
from tests.support.fake_ledger_repository import InMemoryLedgerRepository

TEN = Decimal("10.00")
INVOICE = Decimal("121.00")
PURCHASE = Decimal("60.50")


# ===========================================================================
# The scenario every case starts from
# ===========================================================================
# Three balanced entries in one journal and one fiscal year, numbered 1, 2, 3.
# Both the fake and the database post exactly these, through the ordinary
# write path, so every case begins from books that the job reports as intact -
# which the first test in each file asserts before any of these run.
#
# Entry 2 touches the receivables control account and entry 3 the payables
# one, because FR-GL-006's checks need a sub-ledger to disagree with. Entry 3
# is a shape rather than a plausible transaction: the fixture's chart of
# accounts has one asset and one revenue account to spare, so "cash up,
# payables up" is what a two-line entry against the AP control account can be
# built from here.


def scenario(world: World) -> list[EntryInput]:
    return [
        entry(
            world,
            description="contante verkoop",
            lines=[
                LineInput(account_id=world.cash_account_id, debit=TEN),
                LineInput(account_id=world.revenue_account_id, credit=TEN),
            ],
        ),
        entry(
            world,
            description="verkoopfactuur Jansen B.V.",
            lines=[
                LineInput(
                    account_id=world.receivables_control_id,
                    debit=INVOICE,
                    subledger_party_id=world.customer_party_id,
                ),
                LineInput(account_id=world.revenue_account_id, credit=INVOICE),
            ],
        ),
        entry(
            world,
            description="inkoop De Vries Groothandel",
            lines=[
                LineInput(account_id=world.cash_account_id, debit=PURCHASE),
                LineInput(
                    account_id=world.payables_control_id,
                    credit=PURCHASE,
                    subledger_party_id=world.supplier_party_id,
                ),
            ],
        ),
    ]


#: Entry numbers the scenario produces, named so the corruptions below read as
#: intentions rather than as magic numbers.
FIRST, MIDDLE, LAST = 1, 2, 3


@dataclass(frozen=True, slots=True)
class CorruptionCase:
    #: What was done to the ledger, phrased as the incident report would be.
    name: str
    #: The requirement the corrupted ledger now contradicts.
    requirement: str
    #: Deviation keys that must appear in the report. Subset assertion.
    expect: frozenset[str]
    #: None where the state cannot be represented in the in-memory ledger.
    corrupt_fake: Callable[[InMemoryLedgerRepository, World], None] | None
    #: Runs with journal_entry's and journal_line's triggers disarmed.
    corrupt_sql: tuple[str, ...]
    #: Required when corrupt_fake is None: why, in one sentence.
    fake_note: str = ""


# ---------------------------------------------------------------------------
# Helpers for the in-memory half
# ---------------------------------------------------------------------------


def _entry(repo: InMemoryLedgerRepository, world: World, number: int) -> PostedEntry:
    for candidate in repo.entries.values():
        if candidate.journal_id == world.journal_id and candidate.entry_number == number:
            return candidate
    raise AssertionError(f"the scenario did not post entry {number}")


def _put(repo: InMemoryLedgerRepository, posted: PostedEntry) -> None:
    repo.entries[posted.id] = posted


def _series_key(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    return (world.journal_id, world.fiscal_year_id)


# ===========================================================================
# FR-GL-001: debit/credit balance
# ===========================================================================


def _edit_a_posted_amount(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, FIRST)
    lines = tuple(
        replace(line, debit=line.debit + Decimal("1.00")) if line.debit > 0 else line
        for line in posted.lines
    )
    _put(repo, replace(posted, lines=lines))


def _remove_every_line(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, FIRST)
    _put(repo, replace(posted, lines=()))


def _remove_one_line(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, FIRST)
    _put(repo, replace(posted, lines=posted.lines[:1]))


# ===========================================================================
# FR-GL-013: numbering continuity
# ===========================================================================


def _delete_the_middle_entry(repo: InMemoryLedgerRepository, world: World) -> None:
    del repo.entries[_entry(repo, world, MIDDLE).id]


def _delete_the_last_entry(repo: InMemoryLedgerRepository, world: World) -> None:
    del repo.entries[_entry(repo, world, LAST).id]


def _rewind_the_allocator(repo: InMemoryLedgerRepository, world: World) -> None:
    repo.sequences[_series_key(world)] = MIDDLE


def _remove_the_allocator(repo: InMemoryLedgerRepository, world: World) -> None:
    del repo.sequences[_series_key(world)]


def _delete_the_whole_series(repo: InMemoryLedgerRepository, world: World) -> None:
    for number in (FIRST, MIDDLE, LAST):
        del repo.entries[_entry(repo, world, number).id]


# ===========================================================================
# FR-GL-006: sub-ledger to control account agreement
# ===========================================================================


def _strip_the_party(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, MIDDLE)
    lines = tuple(
        replace(line, subledger_party_id=None)
        if line.account_id == world.receivables_control_id
        else line
        for line in posted.lines
    )
    _put(repo, replace(posted, lines=lines))


def _attach_a_party_to_an_ordinary_line(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, FIRST)
    lines = tuple(
        replace(line, subledger_party_id=world.customer_party_id)
        if line.account_id == world.revenue_account_id
        else line
        for line in posted.lines
    )
    _put(repo, replace(posted, lines=lines))


def _swap_in_a_supplier(repo: InMemoryLedgerRepository, world: World) -> None:
    posted = _entry(repo, world, MIDDLE)
    lines = tuple(
        replace(line, subledger_party_id=world.supplier_party_id)
        if line.account_id == world.receivables_control_id
        else line
        for line in posted.lines
    )
    _put(repo, replace(posted, lines=lines))


def _swap_in_a_foreign_party(repo: InMemoryLedgerRepository, world: World) -> None:
    """The party belongs to the other administration.

    Seeded here rather than in the World because no healthy fixture needs it:
    a party in another administration has no legitimate use, which is the
    reason a line naming one is a finding.
    """
    from api.ledger.model import Party, PartyKind

    foreign = Party(
        id=uuid.uuid4(),
        administration_id=world.other_administration_id,
        party_kind=PartyKind.CUSTOMER,
        name="Buurman B.V.",
    )
    repo.parties[foreign.id] = foreign

    posted = _entry(repo, world, MIDDLE)
    lines = tuple(
        replace(line, subledger_party_id=foreign.id)
        if line.account_id == world.receivables_control_id
        else line
        for line in posted.lines
    )
    _put(repo, replace(posted, lines=lines))


# ===========================================================================
# The table
# ===========================================================================
#
# Every statement is parameterised by the ids the integration test supplies:
# :admin, :journal, :year, :receivables, :payables, :revenue, :customer,
# :supplier, :other_party. Each targets its row through the entry NUMBER
# rather than through an id captured at seed time, so a case reads as "the
# middle entry of the series" - which is what makes the expected numbering
# consequence obvious.

_ENTRY_BY_NUMBER = (
    "SELECT e.id FROM journal_entry e "
    " WHERE e.administration_id = :admin AND e.journal_id = :journal "
    "   AND e.fiscal_year_id = :year AND e.entry_number = {number}"
)

CORRUPTION_CASES: tuple[CorruptionCase, ...] = (
    # -- FR-GL-001 ---------------------------------------------------------
    CorruptionCase(
        name="a posted amount is edited",
        requirement="FR-GL-001",
        # Both, and the pair is the point: the entry no longer balances, and
        # neither does the year it belongs to. A job that reported only the
        # first would leave "do the books balance" unanswered.
        expect=frozenset({"entry_unbalanced", "administration_unbalanced"}),
        corrupt_fake=_edit_a_posted_amount,
        corrupt_sql=(
            "UPDATE journal_line SET debit = debit + 1.00 "
            " WHERE id = (SELECT l.id FROM journal_line l "
            f"              JOIN ({_ENTRY_BY_NUMBER.format(number=FIRST)}) e "
            "                ON e.id = l.journal_entry_id "
            "             WHERE l.debit > 0 ORDER BY l.line_number LIMIT 1)",
        ),
    ),
    CorruptionCase(
        name="every line of an entry is removed",
        requirement="FR-GL-001",
        # A header with nothing posted to it sums to zero on both sides, so
        # the arithmetic alone waves it through - the NULL trap
        # journal_entry_assert_balanced() guards in 0020, checked here from
        # the other side. The year still balances, so this is the one case
        # where the aggregate correctly stays quiet.
        expect=frozenset({"entry_without_lines"}),
        corrupt_fake=_remove_every_line,
        corrupt_sql=(
            "DELETE FROM journal_line WHERE journal_entry_id IN "
            f"({_ENTRY_BY_NUMBER.format(number=FIRST)})",
        ),
    ),
    CorruptionCase(
        name="one line of an entry is removed",
        requirement="FR-GL-001",
        expect=frozenset({"entry_with_one_line", "administration_unbalanced"}),
        corrupt_fake=_remove_one_line,
        corrupt_sql=(
            "DELETE FROM journal_line "
            " WHERE id = (SELECT l.id FROM journal_line l "
            f"              JOIN ({_ENTRY_BY_NUMBER.format(number=FIRST)}) e "
            "                ON e.id = l.journal_entry_id "
            "             ORDER BY l.line_number DESC LIMIT 1)",
        ),
    ),
    CorruptionCase(
        name="an entry header is removed and its lines are left behind",
        requirement="FR-GL-001",
        # Two checks catching two different consequences of one deletion: the
        # amounts are still in the books with nothing to explain them, and the
        # series now has a hole where entry 2 was.
        expect=frozenset({"orphan_line", "numbering_gap"}),
        corrupt_fake=None,
        fake_note=(
            "the in-memory ledger stores lines inside their PostedEntry, so a "
            "line without an entry is not a state it can hold"
        ),
        corrupt_sql=(
            f"DELETE FROM journal_entry WHERE id IN ({_ENTRY_BY_NUMBER.format(number=MIDDLE)})",
        ),
    ),
    # -- FR-GL-013 ---------------------------------------------------------
    CorruptionCase(
        name="an entry in the middle of a series is deleted with its lines",
        requirement="FR-GL-013",
        expect=frozenset({"numbering_gap"}),
        corrupt_fake=_delete_the_middle_entry,
        corrupt_sql=(
            "DELETE FROM journal_line WHERE journal_entry_id IN "
            f"({_ENTRY_BY_NUMBER.format(number=MIDDLE)})",
            f"DELETE FROM journal_entry WHERE id IN ({_ENTRY_BY_NUMBER.format(number=MIDDLE)})",
        ),
    ),
    CorruptionCase(
        name="the tail of a series is deleted with its lines",
        requirement="FR-GL-013",
        # The case the gap report structurally cannot catch: 1..2 is a
        # perfectly gapless series. Only the allocator remembers that a third
        # number was issued, which is why it is checked against max+1 - the
        # numbering series' equivalent of the audit chain's external anchor.
        expect=frozenset({"sequence_drift"}),
        corrupt_fake=_delete_the_last_entry,
        corrupt_sql=(
            "DELETE FROM journal_line WHERE journal_entry_id IN "
            f"({_ENTRY_BY_NUMBER.format(number=LAST)})",
            f"DELETE FROM journal_entry WHERE id IN ({_ENTRY_BY_NUMBER.format(number=LAST)})",
        ),
    ),
    CorruptionCase(
        name="the allocator is rewound",
        requirement="FR-GL-013",
        # Nothing is wrong with the entries yet. The next posting would
        # collide with number 2, and this is the check that says so before it
        # happens rather than after.
        expect=frozenset({"sequence_drift"}),
        corrupt_fake=_rewind_the_allocator,
        corrupt_sql=(
            "UPDATE journal_sequence SET next_number = 2 "
            " WHERE journal_id = :journal AND fiscal_year_id = :year",
        ),
    ),
    CorruptionCase(
        name="the allocator row is removed",
        requirement="FR-GL-013",
        expect=frozenset({"sequence_missing"}),
        corrupt_fake=_remove_the_allocator,
        corrupt_sql=(
            "DELETE FROM journal_sequence  WHERE journal_id = :journal AND fiscal_year_id = :year",
        ),
    ),
    CorruptionCase(
        name="every entry in a series is deleted",
        requirement="FR-GL-013",
        # With the whole series gone there is no series left to find a hole
        # in, and no unbalanced year. The allocator standing at 4 over an
        # empty journal is the only surviving evidence that three entries were
        # ever posted.
        expect=frozenset({"series_missing"}),
        corrupt_fake=_delete_the_whole_series,
        corrupt_sql=(
            "DELETE FROM journal_line WHERE journal_entry_id IN "
            " (SELECT id FROM journal_entry WHERE administration_id = :admin "
            "    AND journal_id = :journal AND fiscal_year_id = :year)",
            "DELETE FROM journal_entry WHERE administration_id = :admin "
            "  AND journal_id = :journal AND fiscal_year_id = :year",
        ),
    ),
    # -- FR-GL-006 ---------------------------------------------------------
    CorruptionCase(
        name="a control account line loses its party",
        requirement="FR-GL-006",
        # FR-GL-006's "reconciled continuously" holds because the sub-ledger
        # IS the control account's lines grouped by party. Strip the party and
        # the control account carries an amount owed by nobody, which is both
        # a direct posting and a reconciliation difference.
        expect=frozenset({"control_line_without_party", "control_subledger_difference"}),
        corrupt_fake=_strip_the_party,
        corrupt_sql=(
            "UPDATE journal_line SET subledger_party_id = NULL "
            " WHERE administration_id = :admin AND account_id = :receivables",
        ),
    ),
    CorruptionCase(
        name="an ordinary line gains a party",
        requirement="FR-GL-006",
        # The converse half of the biconditional, and the easier one to
        # overlook: a receivable recorded somewhere the control account will
        # never aggregate it.
        expect=frozenset({"party_line_off_control"}),
        corrupt_fake=_attach_a_party_to_an_ordinary_line,
        corrupt_sql=(
            "UPDATE journal_line SET subledger_party_id = :customer "
            " WHERE id = (SELECT l.id FROM journal_line l "
            "              WHERE l.administration_id = :admin "
            "                AND l.account_id = :revenue "
            "              ORDER BY l.id LIMIT 1)",
        ),
    ),
    CorruptionCase(
        name="a supplier is posted to the receivables control account",
        requirement="FR-GL-006",
        # The totals still reconcile - the amount is attributed to a real
        # party in this administration - and the sub-ledger is nonsense. Only
        # the party-kind check sees it.
        expect=frozenset({"party_kind_mismatch"}),
        corrupt_fake=_swap_in_a_supplier,
        corrupt_sql=(
            "UPDATE journal_line SET subledger_party_id = :supplier "
            " WHERE administration_id = :admin AND account_id = :receivables",
        ),
    ),
    CorruptionCase(
        name="a party from another administration is attributed",
        requirement="FR-GL-006",
        # CLAUDE.md rule 1 as it surfaces inside FR-GL-006. The sub-ledger
        # total is computed only from parties in this administration, so the
        # control account and its sub-ledger stop agreeing - which is how a
        # tenancy break shows up as an accounting one.
        expect=frozenset({"party_from_other_administration", "control_subledger_difference"}),
        corrupt_fake=_swap_in_a_foreign_party,
        corrupt_sql=(
            "UPDATE journal_line SET subledger_party_id = :other_party "
            " WHERE administration_id = :admin AND account_id = :receivables",
        ),
    ),
)


#: Every deviation key 0023 can emit. The suite asserts the case table covers
#: all of them, so a check added to the migration without a case that breaks
#: it fails the build rather than shipping untested.
ALL_DEVIATIONS = frozenset(
    {
        "entry_unbalanced",
        "entry_without_lines",
        "entry_with_one_line",
        "orphan_line",
        "administration_unbalanced",
        "control_line_without_party",
        "party_line_off_control",
        "party_kind_mismatch",
        "party_from_other_administration",
        "control_subledger_difference",
        "numbering_gap",
        "sequence_drift",
        "sequence_missing",
        "series_missing",
    }
)

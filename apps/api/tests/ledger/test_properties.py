"""NFR-042's property-based tests for the accounting engine.

    NFR-042  The accounting engine has property-based tests asserting
             invariants (balance, immutability, period integrity).

Five invariants, each stated as a property over generated inputs rather than
as an example:

    1. total debits equal total credits, always
    2. no committed posting can be mutated or deleted
    3. sub-ledger totals equal their control account
    4. numbering has no gaps
    5. a reversal exactly offsets its original

Two of those - immutability and gaplessness - are properties of a *sequence*
of operations, not of a single input. An entry is not immutable in isolation;
it is immutable across everything that happens afterwards, including the
operations nobody thought to write a test for. Those use Hypothesis's
`RuleBasedStateMachine`, which generates the interleavings and shrinks a
failure to the shortest sequence that still breaks the invariant. The other
three are `@given` over generated entries.

--- What this runs against ---

The in-memory repository, which reimplements migration 0020's invariants
rather than stubbing them (see tests/support/fake_ledger_repository.py). So a
failure here means the engine's logic is wrong. It does NOT mean the database
would have allowed it - the database enforces the same invariants
independently, with a deferred constraint trigger, withheld privileges and a
transaction-id seal, and tests/integration/test_ledger_invariants.py is what
proves those. Hypothesis cannot drive a real Postgres transaction per example
at a useful rate, and a property test that committed 100 transactions per
case would be measuring the database rather than the engine.

--- Determinism ---

`derandomize=True` on the profile: the same commit generates the same
examples, so a CI failure reproduces locally instead of vanishing on re-run.
Hypothesis still shrinks, and the `.hypothesis` example database still
records failures within a run.
"""

from __future__ import annotations

# The engine is async and Hypothesis's runner is not, so each example drives
# the service through a fresh event loop via asyncio.run(). Per-example rather
# than a shared loop, because a shared one would let state leak between
# examples - which is exactly what a property test must not have.
import asyncio
import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from api.audit.log import AuditLog
from api.ledger import (
    ControlKind,
    EntryInput,
    LedgerError,
    LedgerService,
    LineInput,
    PostedEntry,
)
from tests.ledger.fake_world import build_fake_world
from tests.ledger.world import World
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_ledger_repository import InMemoryLedgerRepository

#: Deliberately modest. Every example builds a fresh world and replays a
#: sequence of postings, so the cost per example is real; 100 balanced entries
#: with generated splits is far more coverage than the twelve examples a
#: hand-written test would have had.
SETTINGS = settings(
    max_examples=100,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ===========================================================================
# Strategies
# ===========================================================================


def amounts(min_units: int = 1, max_units: int = 10**9) -> st.SearchStrategy[Decimal]:
    """Positive amounts at the ledger's scale.

    Generated from an integer number of CENTS, not from floats. `st.decimals()`
    with `places=2` would also work, but building from integers makes it
    impossible for the generator itself to introduce the imprecision NFR-031
    exists to keep out - a test whose inputs are already wrong proves nothing
    about the code that stores them.

    The upper bound keeps sums inside numeric(19,2) even when a hundred
    entries accumulate on one account.
    """
    return st.integers(min_value=min_units, max_value=max_units).map(
        lambda units: Decimal(units) / Decimal(100)
    )


@st.composite
def split(draw: st.DrawFn, total: Decimal, max_parts: int = 4) -> list[Decimal]:
    """Split an amount into 1..max_parts positive parts summing exactly to it.

    Splitting rather than drawing independent amounts is what produces entries
    where the debit and credit sides have DIFFERENT line counts. A balance
    check that compared lines pairwise, or zipped the two sides, passes on
    two-line entries and fails here.
    """
    units = int(total * 100)
    parts = draw(st.integers(min_value=1, max_value=min(max_parts, max(1, units))))
    cuts = sorted(
        draw(
            st.lists(
                st.integers(min_value=1, max_value=units - 1),
                min_size=parts - 1,
                max_size=parts - 1,
                unique=True,
            )
        )
    )
    boundaries = [0, *cuts, units]
    return [
        Decimal(boundaries[i + 1] - boundaries[i]) / Decimal(100)
        for i in range(len(boundaries) - 1)
    ]


@st.composite
def balanced_lines(draw: st.DrawFn, world: World) -> list[LineInput]:
    """A balanced entry over ordinary (non-control) accounts.

    Balanced by construction: one total, split independently across the debit
    and credit sides. That is the shape the engine has to accept - the
    unbalanced cases are generated by perturbing this.
    """
    accounts = [world.cash_account_id, world.revenue_account_id]
    total = draw(amounts(min_units=2, max_units=10**8))

    lines = [
        LineInput(account_id=draw(st.sampled_from(accounts)), debit=part)
        for part in draw(split(total))
    ]
    lines += [
        LineInput(account_id=draw(st.sampled_from(accounts)), credit=part)
        for part in draw(split(total))
    ]
    return lines


@st.composite
def subledger_lines(draw: st.DrawFn, world: World) -> list[LineInput]:
    """An entry touching a control account, which FR-GL-006 requires to name a
    party. Either side of the AR/AP pair, so the property is checked for both.
    """
    receivable = draw(st.booleans())
    amount = draw(amounts(max_units=10**8))
    if receivable:
        return [
            LineInput(
                account_id=world.receivables_control_id,
                debit=amount,
                subledger_party_id=world.customer_party_id,
            ),
            LineInput(account_id=world.revenue_account_id, credit=amount),
        ]
    return [
        LineInput(account_id=world.cash_account_id, debit=amount),
        LineInput(
            account_id=world.payables_control_id,
            credit=amount,
            subledger_party_id=world.supplier_party_id,
        ),
    ]


def entry_over(world: World, lines: list[LineInput], **overrides: object) -> EntryInput:
    fields: dict[str, object] = {
        "administration_id": world.administration_id,
        "journal_id": world.journal_id,
        "period_id": world.open_period_id,
        "entry_date": world.entry_date,
        "description": "generated",
        "lines": lines,
        "source_system": "pytest",
        "posted_by_user_id": world.actor_user_id,
    }
    fields.update(overrides)
    return EntryInput(**fields)  # type: ignore[arg-type]


async def _engine() -> tuple[LedgerService, InMemoryLedgerRepository, World]:
    repo, world = await build_fake_world()
    return LedgerService(repo, AuditLog(InMemoryAuditRepository())), repo, world


def _snapshot(entry: PostedEntry) -> tuple[object, ...]:
    """Everything about an entry that must never change.

    A tuple rather than the dataclass, so a failure prints the differing field
    - and so a mutable field added to PostedEntry later is a deliberate
    decision here rather than something that silently escapes the check.
    """
    return (
        entry.id,
        entry.entry_number,
        entry.entry_date,
        entry.description,
        entry.period_id,
        entry.journal_id,
        entry.fiscal_year_id,
        entry.reverses_entry_id,
        tuple(
            (
                line.line_number,
                line.account_id,
                line.debit,
                line.credit,
                line.subledger_party_id,
            )
            for line in entry.lines
        ),
    )


# ===========================================================================
# 1. Total debits equal total credits, always (FR-GL-001)
# ===========================================================================


@SETTINGS
@given(data=st.data())
def test_every_balanced_entry_is_accepted_and_stays_balanced(data: st.DataObject) -> None:
    async def run() -> None:
        service, _, world = await _engine()
        lines = data.draw(balanced_lines(world))

        posted = await service.post(entry_over(world, lines))

        debit = sum((line.debit for line in posted.lines), Decimal("0.00"))
        credit = sum((line.credit for line in posted.lines), Decimal("0.00"))
        assert debit == credit
        # The stored lines are the submitted ones, not a re-derivation: an
        # engine that balanced by ADJUSTING a line would satisfy the check
        # above and be catastrophically wrong.
        assert sorted((line.debit, line.credit) for line in posted.lines) == sorted(
            (line.debit, line.credit) for line in lines
        )

    asyncio.run(run())


@SETTINGS
@given(data=st.data(), drift_units=st.integers(min_value=1, max_value=10**6))
def test_no_unbalanced_entry_is_ever_accepted(data: st.DataObject, drift_units: int) -> None:
    """Perturbs one line of an otherwise valid entry.

    The drift is drawn down to a single cent, which is the case that matters:
    a balance check written with any tolerance at all - a float comparison, an
    `abs(a - b) < 0.01` - passes a large perturbation and fails this one.
    """

    async def run() -> None:
        service, _, world = await _engine()
        lines = data.draw(balanced_lines(world))
        index = data.draw(st.integers(min_value=0, max_value=len(lines) - 1))
        drift = Decimal(drift_units) / Decimal(100)

        victim = lines[index]
        lines[index] = LineInput(
            account_id=victim.account_id,
            debit=victim.debit + drift if victim.debit > 0 else Decimal("0.00"),
            credit=victim.credit + drift if victim.credit > 0 else Decimal("0.00"),
        )

        with pytest.raises(LedgerError) as raised:
            await service.post(entry_over(world, lines))
        assert "unbalanced" in str(raised.value)

    asyncio.run(run())


@SETTINGS
@given(data=st.data(), count=st.integers(min_value=1, max_value=8))
def test_the_trial_balance_always_nets_to_zero(data: st.DataObject, count: int) -> None:
    """The system-wide form of FR-GL-001, and the assertion an accountant
    actually makes.

    Implied by every entry balancing, which is the point: it fails if entries
    are balanced on the way in but stored or aggregated wrongly.
    """

    async def run() -> None:
        service, _, world = await _engine()
        for _ in range(count):
            await service.post(entry_over(world, data.draw(balanced_lines(world))))

        rows = await service.trial_balance(
            administration_id=world.administration_id,
            fiscal_year_id=world.fiscal_year_id,
        )
        assert sum((row.balance for row in rows), Decimal("0.00")) == Decimal("0.00")

    asyncio.run(run())


# ===========================================================================
# 3. Sub-ledger totals equal their control account (FR-GL-006)
# ===========================================================================


@SETTINGS
@given(data=st.data(), count=st.integers(min_value=1, max_value=10))
def test_every_control_account_equals_the_sum_of_its_parties(
    data: st.DataObject, count: int
) -> None:
    """Structurally true - the sub-ledger IS the control account's own lines
    grouped by party - so this is a test that the structure is what the code
    actually does, over arbitrary traffic mixing AR, AP and ordinary entries.
    """

    async def run() -> None:
        service, _, world = await _engine()

        for _ in range(count):
            lines = data.draw(st.one_of(subledger_lines(world), balanced_lines(world)))
            await service.post(entry_over(world, lines))

        for kind in ControlKind:
            parties = await service.subledger_balance(
                administration_id=world.administration_id, control_kind=kind
            )
            row = next(
                r
                for r in await service.reconciliation(administration_id=world.administration_id)
                if r.control_kind is kind
            )
            assert row.control_balance == sum((p.balance for p in parties), Decimal("0.00"))
            # The unattributed bucket cannot receive anything: a line on a
            # control account with no party is refused. A non-zero difference
            # means that guard is gone.
            assert row.difference == Decimal("0.00")
            assert row.reconciled

    asyncio.run(run())


# ===========================================================================
# 5. A reversal exactly offsets its original (FR-GL-003)
# ===========================================================================


@SETTINGS
@given(data=st.data())
def test_an_entry_and_its_reversal_net_to_zero_on_every_account(
    data: st.DataObject,
) -> None:
    """Not "a reversal exists" but "the pair leaves every account exactly
    where it started".

    A reversal that mirrored only some lines, dropped a sub-ledger party, or
    rounded one amount would pass a shape check and fail this.
    """

    async def run() -> None:
        service, _, world = await _engine()
        lines = data.draw(st.one_of(balanced_lines(world), subledger_lines(world)))

        original = await service.post(entry_over(world, lines))
        reversal = await service.reverse(
            entry_id=original.id,
            actor_user_id=world.actor_user_id,
            period_id=world.open_period_id,
            entry_date=world.entry_date,
            description="generated correction",
        )

        net: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
        for line in [*original.lines, *reversal.lines]:
            key = (line.account_id, line.subledger_party_id)
            net[key] = net.get(key, Decimal("0.00")) + line.debit - line.credit

        assert all(value == Decimal("0.00") for value in net.values()), net
        # Grouped by (account, party), not just account: a reversal that
        # offset the right total against the WRONG customer would net to zero
        # per account and leave both parties' balances wrong.
        assert set(net) == {(line.account_id, line.subledger_party_id) for line in original.lines}

    asyncio.run(run())


@SETTINGS
@given(data=st.data())
def test_a_reversal_leaves_the_trial_balance_where_it_started(
    data: st.DataObject,
) -> None:
    """The observable consequence of the property above, which is what a user
    checks: post, reverse, and every account is back to its opening balance.
    """

    async def run() -> None:
        service, _, world = await _engine()

        async def balances() -> dict[uuid.UUID, Decimal]:
            rows = await service.trial_balance(
                administration_id=world.administration_id,
                fiscal_year_id=world.fiscal_year_id,
            )
            return {row.account_id: row.balance for row in rows}

        before = await balances()
        original = await service.post(entry_over(world, data.draw(balanced_lines(world))))
        await service.reverse(
            entry_id=original.id,
            actor_user_id=world.actor_user_id,
            period_id=world.open_period_id,
            entry_date=world.entry_date,
            description="generated correction",
        )

        assert await balances() == before

    asyncio.run(run())


# ===========================================================================
# Period integrity (FR-GL-007)
# ===========================================================================
# Not on the list of five, but NFR-042 names it directly - "balance,
# immutability, period integrity" - so dropping it would leave the
# requirement's own wording uncovered.


@SETTINGS
@given(
    data=st.data(),
    targets=st.lists(st.sampled_from(["open", "locked", "vat_filed"]), min_size=1, max_size=8),
)
def test_no_entry_ever_lands_in_a_closed_period(data: st.DataObject, targets: list[str]) -> None:
    """Posting is attempted into all three period states in generated orders.

    The assertion is on where entries LANDED, not on which calls raised: a
    refusal that happened after the row was written would satisfy
    `pytest.raises` and fail this.
    """

    async def run() -> None:
        service, repo, world = await _engine()
        # The expected REASON, not just "it raised". Asserting only that a
        # LedgerError came back let a mutation removing the period-status
        # guard survive: the entry date still falls outside the locked
        # period's range, so a different check refused it and the test could
        # not tell the difference. Same discipline as tests/ledger/cases.py.
        by_name = {
            "open": (world.open_period_id, None),
            "locked": (world.locked_period_id, "cannot be posted to (FR-GL-007)"),
            "vat_filed": (world.vat_filed_period_id, "hard-locked"),
        }

        for name in targets:
            period_id, expected = by_name[name]
            lines = data.draw(balanced_lines(world))
            candidate = entry_over(world, lines, period_id=period_id)
            if expected is None:
                await service.post(candidate)
            else:
                with pytest.raises(LedgerError) as raised:
                    await service.post(candidate)
                assert expected in str(raised.value), (
                    f"posting into a {name} period was refused, but for the wrong "
                    f"reason: {raised.value}"
                )

        landed = {posted.period_id for posted in repo.entries.values()}
        assert not (landed & {world.locked_period_id, world.vat_filed_period_id}), (
            "an entry reached a closed period (FR-GL-007)"
        )

    asyncio.run(run())


# ===========================================================================
# 2 and 4: immutability and gapless numbering, as a stateful machine
# ===========================================================================


class LedgerMachine(RuleBasedStateMachine):
    """Immutability and gaplessness are properties of a SEQUENCE of operations.

    An entry is not immutable in isolation - it is immutable across everything
    that happens afterwards, including orderings nobody would think to write
    down. A numbering series is not gapless because one post succeeded; it is
    gapless across arbitrary interleavings of successes and failures, which is
    the only place a numbering scheme ever goes wrong.

    So this generates the sequence. Hypothesis picks the rules, and shrinks a
    failure to the shortest sequence that still breaks an invariant - which is
    the part a hand-written loop cannot do.

    Every `@invariant` runs after EVERY rule, so a violation is caught at the
    step that caused it rather than at the end of the run.
    """

    def __init__(self) -> None:
        super().__init__()
        self.service: LedgerService
        self.repo: InMemoryLedgerRepository
        self.world: World
        #: id -> the snapshot taken when it was posted. Nothing removes from
        #: this dict; that is the immutability check's whole point.
        self.snapshots: dict[uuid.UUID, tuple[object, ...]] = {}
        self.issued: list[int] = []
        #: period id -> its debit total at the moment it was locked. Period
        #: integrity (FR-GL-007): once locked, that number can never move.
        self.frozen: dict[uuid.UUID, Decimal] = {}
        #: Postings go here. Advances when a period is locked.
        self.period_id: uuid.UUID
        self.next_period_number = 10

    @initialize()
    def setup(self) -> None:
        self.service, self.repo, self.world = asyncio.run(_engine())
        self.period_id = self.world.open_period_id

    def _period_total(self, period_id: uuid.UUID) -> Decimal:
        return sum(
            (
                line.debit
                for posted in self.repo.entries.values()
                if posted.period_id == period_id
                for line in posted.lines
            ),
            Decimal("0.00"),
        )

    # -- rules ------------------------------------------------------------

    @rule(units=st.integers(min_value=2, max_value=10**8))
    def post(self, units: int) -> None:
        amount = Decimal(units) / Decimal(100)
        posted = asyncio.run(
            self.service.post(
                entry_over(
                    self.world,
                    [
                        LineInput(account_id=self.world.cash_account_id, debit=amount),
                        LineInput(account_id=self.world.revenue_account_id, credit=amount),
                    ],
                    period_id=self.period_id,
                )
            )
        )
        self.snapshots[posted.id] = _snapshot(posted)
        self.issued.append(posted.entry_number)

    @rule(units=st.integers(min_value=2, max_value=10**8))
    def post_to_the_subledger(self, units: int) -> None:
        amount = Decimal(units) / Decimal(100)
        posted = asyncio.run(
            self.service.post(
                entry_over(
                    self.world,
                    [
                        LineInput(
                            account_id=self.world.receivables_control_id,
                            debit=amount,
                            subledger_party_id=self.world.customer_party_id,
                        ),
                        LineInput(account_id=self.world.revenue_account_id, credit=amount),
                    ],
                    period_id=self.period_id,
                )
            )
        )
        self.snapshots[posted.id] = _snapshot(posted)
        self.issued.append(posted.entry_number)

    @precondition(lambda self: bool(self.snapshots))
    @rule(data=st.data())
    def reverse(self, data: st.DataObject) -> None:
        reversible = [
            entry_id
            for entry_id in self.snapshots
            if asyncio.run(self.service.reversal_of(entry_id)) is None
            and self.repo.entries[entry_id].reverses_entry_id is None
        ]
        if not reversible:
            return
        target = data.draw(st.sampled_from(sorted(reversible, key=str)))
        reversal = asyncio.run(
            self.service.reverse(
                entry_id=target,
                actor_user_id=self.world.actor_user_id,
                # The CURRENT open period, not the original's - which may by
                # now be locked. That is the correct accounting treatment, and
                # it is what makes locked_periods_never_change a real test
                # rather than one nothing ever pushes against.
                period_id=self.period_id,
                entry_date=self.world.entry_date,
                description="generated correction",
            )
        )
        self.snapshots[reversal.id] = _snapshot(reversal)
        self.issued.append(reversal.entry_number)

    @rule()
    def lock_the_current_period(self) -> None:
        """FR-GL-007, mid-sequence.

        Records what the period contains, closes it, and opens a new one for
        the traffic that follows - including reversals of entries that are now
        inside the locked period, which is the case the invariant exists for.
        A correction posted into a later period must not disturb the closed
        one, or a VAT return already filed from it stops matching the ledger.
        """
        self.frozen[self.period_id] = self._period_total(self.period_id)
        self.repo.periods[self.period_id].status = "locked"

        self.next_period_number += 1
        later = self.repo.add_period(
            self.repo.fiscal_years[self.world.fiscal_year_id],
            period_number=self.next_period_number,
            start_date=self.world.period_start,
            end_date=self.world.period_end,
        )
        self.period_id = later.id

    @rule(
        reason=st.sampled_from(
            ["locked_period", "vat_filed_period", "unbalanced", "control_no_party"]
        )
    )
    def attempt_a_refused_post(self, reason: str) -> None:
        """Failures are a rule, not an afterthought.

        A numbering scheme is only ever wrong on the failure path, and an
        immutability bug is most likely in the rollback of a partial write. A
        state machine that only ever succeeds tests neither.
        """
        amount = Decimal("10.00")
        cash, revenue = self.world.cash_account_id, self.world.revenue_account_id
        # Defaults to the CURRENT open period, not world.open_period_id, which
        # lock_the_current_period may already have closed - otherwise the
        # unbalanced and control-account cases would start being refused for
        # period reasons and would stop testing what they name.
        overrides: dict[str, object] = {"period_id": self.period_id}
        lines = [
            LineInput(account_id=cash, debit=amount),
            LineInput(account_id=revenue, credit=amount),
        ]

        if reason == "locked_period":
            overrides["period_id"] = self.world.locked_period_id
            expected = "cannot be posted to (FR-GL-007)"
        elif reason == "vat_filed_period":
            overrides["period_id"] = self.world.vat_filed_period_id
            expected = "hard-locked"
        elif reason == "unbalanced":
            lines = [
                LineInput(account_id=cash, debit=amount),
                LineInput(account_id=revenue, credit=amount + Decimal("0.01")),
            ]
            expected = "unbalanced"
        else:
            lines = [
                LineInput(account_id=self.world.receivables_control_id, debit=amount),
                LineInput(account_id=revenue, credit=amount),
            ]
            expected = "cannot be posted to directly"

        with pytest.raises(LedgerError) as raised:
            asyncio.run(self.service.post(entry_over(self.world, lines, **overrides)))
        # The reason, not just the refusal. A rule that accepted any
        # LedgerError would pass when the guard it names had been removed and
        # some other check happened to refuse the same entry.
        assert expected in str(raised.value), (
            f"{reason} was refused for the wrong reason: {raised.value}"
        )

    # -- invariants, checked after every rule ------------------------------

    @invariant()
    def no_committed_posting_is_ever_mutated_or_deleted(self) -> None:
        """Invariant 2. Every entry ever posted still exists, and is identical
        to the moment it was posted.
        """
        for entry_id, expected in self.snapshots.items():
            current = asyncio.run(self.service.entry(entry_id))
            assert current is not None, f"entry {entry_id} was deleted (FR-GL-003)"
            assert _snapshot(current) == expected, (
                f"entry {entry_id} changed after it was committed (FR-GL-003). "
                "Corrections are made by reversing entry, never by mutation."
            )

    @invariant()
    def numbering_has_no_gaps(self) -> None:
        """Invariant 4. The series is 1..N with nothing missing, and the gap
        report agrees.

        Both halves matter: the first would pass if the allocator were correct
        and the report broken, the second if the report were hard-coded to
        return nothing.
        """
        assert sorted(self.issued) == list(range(1, len(self.issued) + 1)), (
            f"the numbering series has a gap: {sorted(self.issued)} (FR-GL-013)"
        )
        assert (
            asyncio.run(
                self.service.numbering_gaps(
                    journal_id=self.world.journal_id,
                    fiscal_year_id=self.world.fiscal_year_id,
                )
            )
            == []
        )

    @invariant()
    def locked_periods_never_change(self) -> None:
        """Period integrity (FR-GL-007), the sense a filed VAT return depends
        on: once a period is closed, nothing that happens afterwards may alter
        what it contains.

        The machine keeps posting and reversing after each lock, so this is
        checked against real subsequent traffic rather than against an idle
        ledger.
        """
        for period_id, expected in self.frozen.items():
            assert self._period_total(period_id) == expected, (
                f"period {period_id} changed after it was locked: "
                f"{self._period_total(period_id)} != {expected} (FR-GL-007)"
            )

    @invariant()
    def the_ledger_always_balances(self) -> None:
        """Invariant 1, held across the whole sequence rather than per entry."""
        rows = asyncio.run(
            self.service.trial_balance(
                administration_id=self.world.administration_id,
                fiscal_year_id=self.world.fiscal_year_id,
            )
        )
        assert sum((row.balance for row in rows), Decimal("0.00")) == Decimal("0.00")

    @invariant()
    def the_subledger_always_equals_its_control_account(self) -> None:
        """Invariant 3, likewise - checked after every operation, including
        the refused ones.
        """
        for row in asyncio.run(
            self.service.reconciliation(administration_id=self.world.administration_id)
        ):
            assert row.difference == Decimal("0.00"), (
                f"{row.control_kind.value} diverged from its sub-ledger by "
                f"{row.difference} (FR-GL-006)"
            )

    @invariant()
    def no_amount_is_ever_a_float(self) -> None:
        """NFR-031. Checked on TYPE, not value: a float that entered the path
        would very likely still compare equal to what was expected.
        """
        for posted in self.repo.entries.values():
            for line in posted.lines:
                assert isinstance(line.debit, Decimal)
                assert isinstance(line.credit, Decimal)
                assert not isinstance(line.debit, float)
                assert not isinstance(line.credit, float)


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(
    max_examples=50,
    stateful_step_count=30,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)

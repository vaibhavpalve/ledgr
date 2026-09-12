"""The integrity job (NFR-033) against the in-memory ledger.

Runs tests/ledger/integrity_cases.py - the same corruption table
tests/integration/test_ledger_integrity.py runs against a real Postgres - so
that every deviation the job claims to detect is proved to be detectable
without a database, on every `make test-api`.

Two things this file is for:

  1. **The checks actually check.** A check that has only ever seen healthy
     books is indistinguishable from `WHERE false`. Each case here breaks the
     ledger and asserts the named deviation comes back.

  2. **Keeping the fake honest.** tests/support/fake_integrity_repository.py
     reimplements migration 0023's queries in Python. Sharing the case table
     with the DB-backed file means a deviation one of them reports and the
     other does not fails there.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.ledger.integrity import (
    IntegrityCheck,
    IntegrityReport,
    IntegrityScope,
    IntegrityViolation,
    LedgerIntegrityJob,
    LoggingIntegrityAlerter,
)
from tests.ledger.fake_world import build_fake_world
from tests.ledger.integrity_cases import (
    ALL_DEVIATIONS,
    CORRUPTION_CASES,
    FIRST,
    CorruptionCase,
    scenario,
)
from tests.ledger.world import World
from tests.support.fake_integrity_alerter import FakeIntegrityAlerter
from tests.support.fake_integrity_repository import InMemoryIntegrityRepository
from tests.support.fake_ledger_repository import InMemoryLedgerRepository

SETTINGS = settings(
    max_examples=50,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)


async def seeded() -> tuple[InMemoryLedgerRepository, World]:
    """A world with the scenario posted through the ordinary write path.

    Through `post()`, not by poking rows in: the books every case starts from
    have to be books the ledger itself would produce, or "the job reports them
    intact" proves nothing about a real ledger.
    """
    repo, world = await build_fake_world()
    for candidate in scenario(world):
        await repo.post(candidate)
    return repo, world


def job(
    repo: InMemoryLedgerRepository, alerter: FakeIntegrityAlerter | None = None
) -> LedgerIntegrityJob:
    return LedgerIntegrityJob(InMemoryIntegrityRepository(repo), alerter)


# ===========================================================================
# The baseline: healthy books
# ===========================================================================


async def test_a_healthy_ledger_reports_intact() -> None:
    repo, _ = await seeded()

    report = await job(repo).run()

    assert report.intact, f"a correctly posted ledger reported: {report.deviations}"
    assert report.deviations == ()
    assert not report.examined_nothing


async def test_a_healthy_ledger_raises_nothing() -> None:
    """The oracle's own shape: verify() is what a test calls after a scenario,
    and on healthy books it returns rather than raising.
    """
    repo, _ = await seeded()

    report = await job(repo).verify()

    assert report.intact


async def test_the_scope_says_what_was_examined() -> None:
    """A clean report has to carry evidence that it looked at something, or
    "no deviations" is unfalsifiable.
    """
    repo, world = await seeded()

    report = await job(repo).run()

    assert report.scope.administrations >= 1
    assert report.scope.entries == 3
    assert report.scope.lines == 6
    assert report.scope.journals >= 1
    assert report.scope.accounts >= 1
    assert report.administration_id is None

    scoped = await job(repo).run(administration_id=world.administration_id)
    assert scoped.scope.entries == 3
    assert scoped.administration_id == world.administration_id


async def test_an_administration_with_no_ledger_at_all_is_not_a_pass() -> None:
    """The failure mode this job would otherwise have.

    Every check reports by returning a row, so a run that can see nothing
    returns nothing and looks exactly like a perfect ledger. An empty ledger is
    `intact` and NOT a verification, and `raise_for_deviations` refuses to
    treat it as one unless asked.
    """
    empty = InMemoryLedgerRepository()

    report = await job(empty).run()

    assert report.intact, "there is nothing wrong, because there is nothing"
    assert report.examined_nothing

    with pytest.raises(IntegrityViolation):
        report.raise_for_deviations()

    # ... unless an empty scope is the expected state, which a caller has to
    # say deliberately.
    report.raise_for_deviations(require_scope=False)


async def test_a_scope_that_matches_no_administration_examines_nothing() -> None:
    """The same trap reached the other way: a real ledger, and a filter that
    selects none of it. Without the scope counts this run reports a clean pass
    over an administration that does not exist.
    """
    repo, _ = await seeded()

    report = await job(repo).run(administration_id=uuid.uuid4())

    assert report.intact
    assert report.examined_nothing
    with pytest.raises(IntegrityViolation):
        report.raise_for_deviations()


# ===========================================================================
# The corruption table
# ===========================================================================


@pytest.mark.parametrize(
    "case",
    [c for c in CORRUPTION_CASES if c.corrupt_fake is not None],
    ids=lambda c: c.name,
)
async def test_the_job_detects(case: CorruptionCase) -> None:
    repo, world = await seeded()
    alerter = FakeIntegrityAlerter()
    runner = job(repo, alerter)

    assert (await runner.run()).intact, "the scenario must start from clean books"

    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)
    report = await runner.run()

    found = {deviation.deviation for deviation in report.deviations}
    assert case.expect <= found, (
        f"{case.name} ({case.requirement}): expected {sorted(case.expect)}, got {sorted(found)}"
    )
    # NFR-033's "alerting on any deviation", for every one of these.
    assert alerter.called
    assert alerter.only.deviations == report.deviations


@pytest.mark.parametrize(
    "case",
    [c for c in CORRUPTION_CASES if c.corrupt_fake is not None],
    ids=lambda c: c.name,
)
async def test_a_detected_deviation_names_the_requirement_and_a_subject(
    case: CorruptionCase,
) -> None:
    """NFR-045: an alert that cannot be acted on is deleted, not tolerated.
    Acting on one of these starts with knowing which row and which rule, so
    both are asserted for every case rather than trusted.
    """
    repo, world = await seeded()
    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)

    report = await job(repo).run()

    for deviation in report.deviations:
        assert deviation.requirement.startswith("FR-GL-")
        assert deviation.subject_type
        assert deviation.subject_id is not None
        assert deviation.summary.strip()
        assert deviation.administration_id is not None
        assert deviation.check in set(IntegrityCheck)


def test_every_deviation_the_job_can_report_has_a_case() -> None:
    """A check added to 0023 without a corruption that triggers it is a check
    nobody has watched fail. This is what makes that a build failure instead
    of a quiet gap - the same role tests/test_isolation_coverage.py plays for
    IAM-005.
    """
    covered: set[str] = set()
    for case in CORRUPTION_CASES:
        covered |= case.expect

    assert covered == ALL_DEVIATIONS, (
        f"deviations with no case: {sorted(ALL_DEVIATIONS - covered)}; "
        f"cases expecting a deviation the job cannot emit: "
        f"{sorted(covered - ALL_DEVIATIONS)}"
    )


def test_a_case_the_fake_cannot_reach_explains_itself() -> None:
    """Some states the database can hold and an in-memory model cannot. That
    is allowed; leaving it unexplained is not, because a `corrupt_fake=None`
    with no reason is indistinguishable from one somebody forgot to write.
    """
    for case in CORRUPTION_CASES:
        if case.corrupt_fake is None:
            assert case.fake_note, f"{case.name} has no in-memory corruption and no note saying why"
            assert case.corrupt_sql, f"{case.name} is covered by neither implementation"


# ===========================================================================
# Alerting (NFR-033, NFR-045)
# ===========================================================================


async def test_a_clean_run_does_not_alert() -> None:
    """An integrity job that pages every healthy night is one whose pages are
    muted by the second week, and a muted page is the same as no check.
    """
    repo, _ = await seeded()
    alerter = FakeIntegrityAlerter()

    await job(repo, alerter).run()

    assert not alerter.called


async def test_one_run_produces_one_alert_however_many_deviations() -> None:
    """Grouping is the requirement, not a nicety: a series with a hundred
    holes is one incident.
    """
    repo, world = await seeded()
    alerter = FakeIntegrityAlerter()

    # Two independent breakages, in two different checks.
    posted = next(e for e in repo.entries.values() if e.entry_number == FIRST)
    repo.entries[posted.id] = replace(posted, lines=posted.lines[:1])
    del repo.sequences[(world.journal_id, world.fiscal_year_id)]

    report = await job(repo, alerter).run()

    assert len(report.deviations) >= 2
    assert len(alerter.alerts) == 1
    assert alerter.only.counts()["balance"] >= 1
    assert alerter.only.counts()["numbering"] >= 1


async def test_the_default_alerter_is_a_real_one() -> None:
    """NFR-033 makes alerting part of the requirement, so a caller that passes
    no alerter gets the weakest real channel rather than silence.
    """
    repo, _ = await seeded()

    assert isinstance(
        LedgerIntegrityJob(InMemoryIntegrityRepository(repo))._alerter,
        LoggingIntegrityAlerter,
    )


async def test_verify_raises_and_carries_the_whole_report() -> None:
    """The first question on seeing this exception is always "which entry",
    so the report travels with it rather than a message.
    """
    repo, world = await seeded()
    case = next(c for c in CORRUPTION_CASES if "posted amount" in c.name)
    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)

    with pytest.raises(IntegrityViolation) as raised:
        await job(repo).verify()

    assert raised.value.report.deviations
    assert "deviation" in str(raised.value)


# ===========================================================================
# The report as an artefact
# ===========================================================================


async def test_the_report_is_json_serialisable_with_no_floats() -> None:
    """NFR-031 reaches the end of the path too. A float in the report would
    round the very difference the report exists to state, and would do it
    after every other layer had kept the number exact.
    """
    repo, world = await seeded()
    case = next(c for c in CORRUPTION_CASES if "posted amount" in c.name)
    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)

    report = await job(repo).run()
    encoded = json.dumps(report.as_dict())
    decoded = json.loads(encoded)

    assert decoded["intact"] is False
    assert decoded["counts"]["balance"] >= 1
    unbalanced = next(d for d in decoded["deviations"] if d["deviation"] == "entry_unbalanced")
    assert unbalanced["detail"]["difference"] == "1.00"
    assert isinstance(unbalanced["detail"]["total_debit"], str)

    def no_floats(value: object, path: str = "") -> None:
        if isinstance(value, float):
            raise AssertionError(f"float in the report at {path}")
        if isinstance(value, dict):
            for key, item in value.items():
                no_floats(item, f"{path}.{key}")
        if isinstance(value, list):
            for index, item in enumerate(value):
                no_floats(item, f"{path}[{index}]")

    no_floats(decoded)


async def test_the_counts_name_every_check_including_the_quiet_ones() -> None:
    """ "numbering: 0" is evidence the numbering check ran. An omitted key is
    evidence of nothing, and a reader cannot tell the difference.
    """
    repo, world = await seeded()
    case = next(c for c in CORRUPTION_CASES if "posted amount" in c.name)
    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)

    report = await job(repo).run()

    assert set(report.counts()) == {check.value for check in IntegrityCheck}
    assert report.counts()["control_account"] == 0
    assert set(report.by_check()) == set(IntegrityCheck)


async def test_two_runs_over_unchanged_books_produce_the_same_report() -> None:
    """Deterministic ordering, so that diffing last night's report against
    tonight's is a meaningful operation rather than a shuffle.
    """
    repo, world = await seeded()
    case = next(c for c in CORRUPTION_CASES if "control account line" in c.name)
    assert case.corrupt_fake is not None
    case.corrupt_fake(repo, world)
    runner = job(repo)

    first = await runner.run()
    second = await runner.run()

    assert [d.as_dict() for d in first.deviations] == [d.as_dict() for d in second.deviations]


def test_the_summary_distinguishes_intact_from_unexamined() -> None:
    """The two states a caller must never conflate, in the one line that ends
    up in a log or on a terminal.
    """
    empty = IntegrityReport(
        checked_at=datetime.now(UTC),
        scope=IntegrityScope(0, 0, 0, 0, 0),
    )
    populated = IntegrityReport(
        checked_at=datetime.now(UTC),
        scope=IntegrityScope(1, 2, 5, 3, 6),
    )

    assert "nothing examined" in empty.summary()
    assert "intact" in populated.summary()
    assert empty.intact and populated.intact


# ===========================================================================
# NFR-042: the property, not the example
# ===========================================================================


@given(
    cents=st.integers(min_value=1, max_value=10**8),
    which=st.integers(min_value=1, max_value=3),
    on_debit=st.booleans(),
)
@SETTINGS
def test_no_perturbation_of_a_posted_amount_goes_unnoticed(
    cents: int, which: int, on_debit: bool
) -> None:
    """The balance check, stated as a property rather than as three examples.

    For any entry in the scenario, either side, any non-zero amount: changing
    a committed line must produce a finding. A check that happened to pass on
    the hand-picked amounts in the case table and missed, say, a change that
    keeps the totals' last digit would survive the examples and fail here.
    """
    delta = Decimal(cents) / Decimal(100)

    async def run() -> None:
        repo, world = await seeded()
        runner = job(repo)
        assert (await runner.run()).intact

        posted = next(
            e
            for e in repo.entries.values()
            if e.journal_id == world.journal_id and e.entry_number == which
        )
        index = next(
            (i for i, line in enumerate(posted.lines) if (line.debit > 0) is on_debit),
            0,
        )
        line = posted.lines[index]
        edited = replace(
            line,
            debit=line.debit + delta if line.debit > 0 else line.debit,
            credit=line.credit + delta if line.credit > 0 else line.credit,
        )
        lines = list(posted.lines)
        lines[index] = edited
        repo.entries[posted.id] = replace(posted, lines=tuple(lines))

        report = await runner.run()
        found = {d.deviation for d in report.deviations}
        assert "entry_unbalanced" in found, (
            f"a {delta} change to entry {which} went unnoticed: {found}"
        )
        assert "administration_unbalanced" in found

    asyncio.run(run())

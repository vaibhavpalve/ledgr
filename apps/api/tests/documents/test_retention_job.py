"""api.documents.retention_job - PRIV-030, without a database.

`documents.expired()` (migration 0045) is what actually finds the rows and
counts the blocking links; what is tested here is the JOB's behaviour given
what that function returns - the loop, the split between deleted and
exception, and the report. tests/integration/test_document_retention_sweep.py
runs the equivalent against real Postgres, including a genuine foreign-key
block.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from api.documents.retention_job import (
    DocumentRetentionSweepJob,
    ExpiredDocument,
    RetentionSweepReport,
)

pytestmark = pytest.mark.anyio

ORG = uuid.uuid4()
ADMIN = uuid.uuid4()


def _candidate(
    *, blocking_link_count: int = 0, retention_until: date = date(2024, 1, 1)
) -> ExpiredDocument:
    return ExpiredDocument(
        id=uuid.uuid4(),
        organization_id=ORG,
        administration_id=ADMIN,
        retention_basis="standard",
        retention_until=retention_until,
        status="active",
        blocking_link_count=blocking_link_count,
    )


@dataclass
class FakeRetentionRepository:
    candidates: list[ExpiredDocument] = field(default_factory=list)
    deleted: list[uuid.UUID] = field(default_factory=list)

    async def expired(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[ExpiredDocument]:
        if administration_id is None:
            return list(self.candidates)
        return [c for c in self.candidates if c.administration_id == administration_id]

    async def delete(self, *, document_id: uuid.UUID) -> None:
        self.deleted.append(document_id)


@dataclass
class RecordingAlerter:
    reports: list[RetentionSweepReport] = field(default_factory=list)

    async def exceptions_found(self, report: RetentionSweepReport) -> None:
        self.reports.append(report)


async def test_nothing_past_retention_is_a_clean_empty_report() -> None:
    repo = FakeRetentionRepository()
    job = DocumentRetentionSweepJob(repo)  # type: ignore[arg-type]

    report = await job.run()

    assert report.examined == 0
    assert report.deleted == ()
    assert report.exceptions == ()
    assert report.clean


async def test_an_unlinked_expired_document_is_deleted() -> None:
    candidate = _candidate(blocking_link_count=0)
    repo = FakeRetentionRepository(candidates=[candidate])
    job = DocumentRetentionSweepJob(repo)  # type: ignore[arg-type]

    report = await job.run()

    assert report.deleted == (candidate.id,)
    assert report.exceptions == ()
    assert candidate.id in repo.deleted


async def test_a_linked_expired_document_is_an_exception_not_a_deletion() -> None:
    """FR-DOC-003: a document_posting_link row is never removed, so a
    document it still names can never actually be deleted. PRIV-030's
    exception report exists for exactly this case.
    """
    candidate = _candidate(blocking_link_count=2, retention_until=date(2020, 6, 30))
    repo = FakeRetentionRepository(candidates=[candidate])
    job = DocumentRetentionSweepJob(repo)  # type: ignore[arg-type]

    report = await job.run()

    assert report.deleted == ()
    assert len(report.exceptions) == 1
    exception = report.exceptions[0]
    assert exception.document_id == candidate.id
    assert exception.retention_until == date(2020, 6, 30)
    assert "2" in exception.reason
    assert "document_posting_link" in exception.reason
    assert candidate.id not in repo.deleted


async def test_exceptions_trigger_exactly_one_alert_for_the_whole_run() -> None:
    """NFR-045: a batch of exceptions is one incident, not one page each."""
    candidates = [_candidate(blocking_link_count=1) for _ in range(5)]
    repo = FakeRetentionRepository(candidates=candidates)
    alerter = RecordingAlerter()
    job = DocumentRetentionSweepJob(repo, alerter)  # type: ignore[arg-type]

    await job.run()

    assert len(alerter.reports) == 1
    assert len(alerter.reports[0].exceptions) == 5


async def test_a_clean_run_does_not_alert() -> None:
    repo = FakeRetentionRepository(candidates=[_candidate(blocking_link_count=0)])
    alerter = RecordingAlerter()
    job = DocumentRetentionSweepJob(repo, alerter)  # type: ignore[arg-type]

    await job.run()

    assert alerter.reports == []


async def test_a_mixed_run_reports_both_and_deletes_only_what_it_can() -> None:
    deletable = _candidate(blocking_link_count=0)
    blocked = _candidate(blocking_link_count=1)
    repo = FakeRetentionRepository(candidates=[deletable, blocked])
    job = DocumentRetentionSweepJob(repo)  # type: ignore[arg-type]

    report = await job.run()

    assert report.examined == 2
    assert report.deleted == (deletable.id,)
    assert [e.document_id for e in report.exceptions] == [blocked.id]
    assert not report.clean


async def test_administration_id_narrows_the_sweep() -> None:
    other_admin = uuid.uuid4()
    mine = _candidate()
    theirs = ExpiredDocument(
        id=uuid.uuid4(),
        organization_id=ORG,
        administration_id=other_admin,
        retention_basis="standard",
        retention_until=date(2024, 1, 1),
        status="active",
        blocking_link_count=0,
    )
    repo = FakeRetentionRepository(candidates=[mine, theirs])
    job = DocumentRetentionSweepJob(repo)  # type: ignore[arg-type]

    report = await job.run(administration_id=ADMIN)

    assert report.examined == 1
    assert report.deleted == (mine.id,)


def test_summary_names_nothing_examined_distinctly_from_a_clean_sweep() -> None:
    """Not the ledger integrity job's ambiguity (0 findings could mean
    "clean" or "examined nothing") - here an empty sweep really is the
    ordinary state, and the summary says so rather than reusing the
    "no exceptions" wording.
    """
    empty = RetentionSweepReport(checked_at=datetime.now(UTC), examined=0)
    clean = RetentionSweepReport(
        checked_at=datetime.now(UTC), examined=3, deleted=(uuid.uuid4(),) * 3
    )

    assert "nothing past retention" in empty.summary()
    assert "no exceptions" in clean.summary()

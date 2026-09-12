"""The ledger integrity job: NFR-033 (PRD §12.4).

    NFR-033  A nightly integrity job verifies debit/credit balance, sub-ledger
             to control account agreement, and numbering continuity, alerting
             on any deviation.

The three checks are FR-GL-001, FR-GL-006 and FR-GL-013 respectively, and all
three are already enforced at write time by migration 0020 - as a deferred
constraint trigger, as a BEFORE INSERT trigger, and as an allocator row behind
a UNIQUE index. So this job should find nothing, on every tenant, forever.

--- Then why run it ---

Because those guards bind every writer, and they bind them only at write time.
A superuser who runs `ALTER TABLE journal_line DISABLE TRIGGER ALL` is not a
writer they can stop - 0019 says the same about the audit log, and
tests/integration/test_audit_tamper_evidence.py demonstrates it. Neither is a
restore from a backup taken mid-transaction, a replication failover that lost
the tail of a series, a data migration between databases, or a future
migration that drops a trigger. In each of those the books are wrong and
nothing has raised.

0020 already states the principle for one of the three checks, in the comment
on `ledger.numbering_gaps()`: a report that can only ever say "no gaps" is the
check ON the allocator. This is that argument applied to all three, swept
across every tenant, on a schedule.

--- Where the checking happens ---

In SQL, in migration 0023, not here. Four reasons, in order of weight:

  1. The comparison has to be made against the ROWS. Re-deriving a balance in
     Python from rows Python has already read tests the reader, not the books.
  2. `ledgr_app` and `ledgr_ops` hold SELECT on the posting tables and nothing
     else, so a check written as SQL needs no privilege this system does not
     already grant.
  3. The functions are SECURITY INVOKER, so tenant scoping is RLS's - the same
     first line of defence CLAUDE.md rule 1 requires of every other query path,
     rather than a WHERE clause this module would have to remember.
  4. A single statement sees a single snapshot. Three round trips would not.

This module turns those rows into a report, decides what an alert is, and is
the thing tests and the CLI call.

--- Alerting, and what it deliberately is not ---

`IntegrityAlerter` receives the whole report, once, only when it is not clean.
Not one call per deviation: a series with a thousand holes is one incident,
and NFR-045 says alerts that cannot be acted on are deleted rather than
tolerated. Each `Deviation` carries `deviation` - a stable key like
`entry_unbalanced` or `sequence_drift` - which is what a runbook entry is
written against, plus the requirement it contradicts and the id of the exact
row to look at.

No audit entry is written, and that is a constraint rather than an oversight.
The nightly sweep runs as `ledgr_ops`, which holds SELECT on `audit_log` and
no INSERT (0019: "support tooling reads and never writes ... a role that could
write the log recording its own access is not an auditor of itself"). A job
that had to write an audit entry would need a privilege that undoes that
property, to record something no tenant did. The report goes to the alerting
path instead, and the ledger it describes is unchanged by having been read.

--- The empty sweep ---

Every check reports by RETURNING A ROW, so "no findings" means both "clean"
and "looked at nothing", and a caller counting findings cannot tell them
apart. A run as `ledgr_app` with no tenant context set sees zero rows through
RLS and would otherwise report a perfect ledger for a database it could not
read. `IntegrityScope` travels with every report for that reason, and
`examined_nothing` is what a caller checks before believing a pass -
scripts/verify_ledger_integrity.py exits 3 on it rather than 0.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from api.ledger.model import LedgerError

logger = logging.getLogger("api.ledger.integrity")

#: How many individual deviations a single alert carries. The counts in the
#: alert are always complete; this bounds only the sample, because an alert
#: nobody can read is an alert nobody acts on (NFR-045).
ALERT_SAMPLE_SIZE = 20


class IntegrityCheck(enum.Enum):
    """NFR-033's three checks, and the requirement each one verifies."""

    #: FR-GL-001. Debits equal credits - per entry, and in aggregate.
    BALANCE = "balance"
    #: FR-GL-006. The sub-ledger and its control account are the same rows.
    CONTROL_ACCOUNT = "control_account"
    #: FR-GL-013 / CMP-009. Every series is 1..n with nothing missing.
    NUMBERING = "numbering"


class IntegrityViolation(LedgerError):
    """Raised by `IntegrityReport.raise_for_deviations()`.

    A LedgerError like every other refusal this context makes, so a caller
    that already catches LedgerError does not silently miss this one. It
    carries the whole report rather than a message, because the first question
    on seeing it is always "which entry".
    """

    def __init__(self, report: IntegrityReport) -> None:
        self.report = report
        super().__init__(report.summary())


@dataclass(frozen=True, slots=True)
class Deviation:
    """One thing the ledger says that it should not be able to say.

    `deviation` is the stable key (`entry_unbalanced`, `orphan_line`,
    `control_line_without_party`, `sequence_drift`, ...). It is what a runbook
    entry is keyed on and what an alert should be grouped by; `summary` is for
    a human reading the page, and `detail` carries the numbers.

    Every monetary value inside `detail` is a STRING, and arrives as one from
    the database. NFR-031 prohibits floating point in the calculation path, and
    a report is the end of that path: a float here would round the very
    difference being reported.
    """

    check: IntegrityCheck
    #: e.g. "FR-GL-001". The requirement the ledger is contradicting.
    requirement: str
    #: The runbook key.
    deviation: str
    organization_id: uuid.UUID | None
    administration_id: uuid.UUID | None
    #: "journal_entry", "journal_line", "ledger_account", "ledger_journal",
    #: "fiscal_year" - what `subject_id` identifies.
    subject_type: str
    subject_id: uuid.UUID | None
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check.value,
            "requirement": self.requirement,
            "deviation": self.deviation,
            "organization_id": _text(self.organization_id),
            "administration_id": _text(self.administration_id),
            "subject_type": self.subject_type,
            "subject_id": _text(self.subject_id),
            "summary": self.summary,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class IntegrityScope:
    """What the run could see.

    Not telemetry. `administrations == 0` is the difference between "the
    ledger is intact" and "this process cannot read the ledger", which no
    count of findings can express - see the module docstring.
    """

    administrations: int
    journals: int
    accounts: int
    entries: int
    lines: int

    @property
    def examined_nothing(self) -> bool:
        """True when the run saw no administration at all.

        Deliberately keyed on administrations rather than on entries: a real
        administration with no postings yet is legitimately clean and must not
        be reported as unverifiable, while a run that could not see a single
        administration has verified nothing regardless of what it found.
        """
        return self.administrations == 0

    def as_dict(self) -> dict[str, int]:
        return {
            "administrations": self.administrations,
            "journals": self.journals,
            "accounts": self.accounts,
            "entries": self.entries,
            "lines": self.lines,
        }


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    checked_at: datetime
    scope: IntegrityScope
    deviations: tuple[Deviation, ...] = ()
    #: What was asked for; None for a sweep across everything visible.
    administration_id: uuid.UUID | None = None

    @property
    def intact(self) -> bool:
        """No deviation was found.

        Note what this does NOT assert: that anything was examined. A caller
        treating this as a pass must check `examined_nothing` too, which is
        why `raise_for_deviations()` takes `require_scope`.
        """
        return not self.deviations

    @property
    def examined_nothing(self) -> bool:
        return self.scope.examined_nothing

    def by_check(self) -> dict[IntegrityCheck, tuple[Deviation, ...]]:
        """Grouped in NFR-033's order, with empty checks present.

        The empty entries are the useful part: a report that lists `numbering:
        0` is evidence the numbering check ran, where a report that simply
        omits it is evidence of nothing.
        """
        return {
            check: tuple(d for d in self.deviations if d.check is check) for check in IntegrityCheck
        }

    def counts(self) -> dict[str, int]:
        return {check.value: len(found) for check, found in self.by_check().items()}

    def summary(self) -> str:
        if self.examined_nothing:
            return (
                "ledger integrity: nothing examined - no administration was visible "
                "to this connection, so the absence of findings is not a pass"
            )
        if self.intact:
            return (
                f"ledger integrity: intact across {self.scope.administrations} "
                f"administration(s), {self.scope.entries} entries, "
                f"{self.scope.lines} lines"
            )
        counts = ", ".join(f"{name} {count}" for name, count in self.counts().items())
        return (
            f"ledger integrity: {len(self.deviations)} deviation(s) ({counts}) across "
            f"{self.scope.administrations} administration(s)"
        )

    def raise_for_deviations(self, *, require_scope: bool = True) -> None:
        """Turn the report into an assertion. The test-oracle entry point.

        `require_scope` defaults to True so that the easy call is the safe one:
        a test that seeded a ledger and then verified nothing because its
        connection carried no tenant context fails, rather than passing on an
        empty result set. Pass False only where an empty scope is the expected
        state.
        """
        if require_scope and self.examined_nothing:
            raise IntegrityViolation(self)
        if self.deviations:
            raise IntegrityViolation(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "administration_id": _text(self.administration_id),
            "intact": self.intact,
            "examined_nothing": self.examined_nothing,
            "scope": self.scope.as_dict(),
            "counts": self.counts(),
            "deviations": [d.as_dict() for d in self.deviations],
        }


class IntegrityRepository(Protocol):
    """The two reads migration 0023 exposes.

    Both are SECURITY INVOKER, so what they return is exactly what the calling
    connection may see: one tenant for `ledgr_app`, every tenant for
    `ledgr_ops` (BYPASSRLS). Neither takes an organization argument, because
    tenant scoping is not this layer's to decide (CLAUDE.md rule 1).
    """

    async def scope(self, *, administration_id: uuid.UUID | None = None) -> IntegrityScope: ...

    async def findings(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[Deviation]: ...


class IntegrityAlerter(Protocol):
    """NFR-033's "alerting on any deviation".

    Called once per run, and only when there is something to say.
    """

    async def deviations_detected(self, report: IntegrityReport) -> None: ...


class LoggingIntegrityAlerter:
    """Structured logs, which is a real alerting channel and not a stub:
    NFR-040 already requires structured logging with correlation ids, and an
    `event=ledger_integrity_deviation` line is something a log-based alert
    rule fires on today.

    What it is not is a pager. Routing these to one - and writing the runbook
    NFR-045 requires for each `deviation` key - is deployment work that this
    module deliberately does not decide, in the same way
    scripts/anchor_audit_chain.py declines to choose where anchors are stored.
    The seam is `IntegrityAlerter`; swapping the implementation is the whole
    change.
    """

    def __init__(self, sample_size: int = ALERT_SAMPLE_SIZE) -> None:
        self._sample_size = sample_size

    async def deviations_detected(self, report: IntegrityReport) -> None:
        # One summary line carrying the complete counts, so an alert rule can
        # fire on it without parsing the sample below.
        logger.critical(
            "ledger_integrity_deviation",
            extra={
                "event": "ledger_integrity_deviation",
                "requirement": "NFR-033",
                "checked_at": report.checked_at.isoformat(),
                "administration_id": _text(report.administration_id),
                "deviations": len(report.deviations),
                "counts": report.counts(),
                "scope": report.scope.as_dict(),
                "summary": report.summary(),
            },
        )
        for deviation in report.deviations[: self._sample_size]:
            logger.critical(
                "ledger_integrity_deviation_detail",
                extra={
                    "event": "ledger_integrity_deviation_detail",
                    **deviation.as_dict(),
                },
            )
        if len(report.deviations) > self._sample_size:
            logger.critical(
                "ledger_integrity_deviation_truncated",
                extra={
                    "event": "ledger_integrity_deviation_truncated",
                    "shown": self._sample_size,
                    "total": len(report.deviations),
                },
            )


class LedgerIntegrityJob:
    """NFR-033, runnable on a schedule or on demand.

    Nightly:   scripts/verify_ledger_integrity.py, as `ledgr_ops`, no
               administration argument - one sweep over every tenant.
    On demand: the same script with `--administration`, or this class directly
               from a test, where it is an oracle: post whatever the scenario
               posts, then assert the books still hold.

    Stateless and idempotent. It writes nothing, holds no privilege to write
    anything, and running it twice produces the same report.
    """

    def __init__(
        self,
        repository: IntegrityRepository,
        alerter: IntegrityAlerter | None = None,
    ) -> None:
        self._repository = repository
        # Defaulting to the logging alerter rather than to None: NFR-033 makes
        # alerting part of the requirement, so a caller that forgets one should
        # get the weakest real channel, not silence.
        self._alerter: IntegrityAlerter = alerter or LoggingIntegrityAlerter()

    async def run(self, *, administration_id: uuid.UUID | None = None) -> IntegrityReport:
        """Run all three checks and alert if anything deviates.

        Scope is read first so that a report always carries it, including the
        empty-scope case that must not be mistaken for a pass.

        The findings are internally consistent - all three checks run in one
        statement, therefore one snapshot. The scope is a second statement, so
        under READ COMMITTED a posting committing between the two could leave
        the counts a few rows behind the findings. That is deliberate rather
        than overlooked: the counts answer "did this examine anything", which
        no concurrent posting can change, and pinning both to one snapshot
        would mean holding a REPEATABLE READ transaction open across the whole
        sweep for a guarantee nothing here needs.
        """
        checked_at = datetime.now(UTC)
        scope = await self._repository.scope(administration_id=administration_id)
        deviations = await self._repository.findings(administration_id=administration_id)

        report = IntegrityReport(
            checked_at=checked_at,
            scope=scope,
            deviations=tuple(deviations),
            administration_id=administration_id,
        )

        if not report.intact:
            await self._alerter.deviations_detected(report)
        return report

    async def verify(
        self, *, administration_id: uuid.UUID | None = None, require_scope: bool = True
    ) -> IntegrityReport:
        """`run()`, but raises `IntegrityViolation` instead of returning a
        report describing a broken ledger. The form a test oracle wants.
        """
        report = await self.run(administration_id=administration_id)
        report.raise_for_deviations(require_scope=require_scope)
        return report


def _text(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None

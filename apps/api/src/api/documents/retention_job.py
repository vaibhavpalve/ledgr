"""The document retention sweep: PRIV-030 (PRD 10.4). See
docs/decisions/ADR-051-document-retention-sweep.md.

    PRIV-030  Retention is enforced by automated jobs, not manual process,
              with an exception report for records that failed to expire.

--- What this job does not decide ---

Whether a document MAY be removed. `document_deletion_guard` (migration 0031)
already permits an unconditional DELETE once `retention_until < current_date`,
whatever the row's `status` - a restricted document (PRIV-023) is swept
exactly like an active one, no approval needed for either. This job supplies
the SELECTION (`documents.expired()`, migration 0045) and the loop; the rule
that makes deletion safe lives in the database, the same division
`api.ledger.integrity` keeps with 0023's checks.

--- The exception report, and why it is decided rather than caught ---

A document that was ever linked to a posting can never actually be deleted:
`document_posting_link` is immutable and never removed (FR-DOC-003), and its
foreign key to `document` has no ON DELETE action, so the link blocks the
DELETE for as long as it exists - which is forever. `documents.expired()`
counts those links in advance, so this job can name the reason in FR-DOC-003's
own terms rather than surface a caught IntegrityError, which would say only
that something referenced the row. PRIV-030's "exception report for records
that failed to expire" is exactly this: not a crash, and not a silent skip -
a named reason a person can act on.

--- Alerting ---

Once per run, with the whole report, and only when there is an exception -
the same shape `api.ledger.integrity.IntegrityAlerter` takes and for the same
reason (NFR-045): a page for every clean night is muted by the second week.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

logger = logging.getLogger("api.documents.retention_job")


@dataclass(frozen=True, slots=True)
class ExpiredDocument:
    """One row `documents.expired()` returned - a candidate for removal."""

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    retention_basis: str
    retention_until: date
    status: str
    #: `document_posting_link` rows (live or detached) that name this
    #: document. Non-zero means the DELETE would be refused by the database,
    #: not attempted and caught - see the module docstring.
    blocking_link_count: int


@dataclass(frozen=True, slots=True)
class RetentionException:
    """PRIV-030's "record that failed to expire", with the reason a person
    reading the report can act on.
    """

    document_id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    retention_until: date
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": str(self.document_id),
            "organization_id": str(self.organization_id),
            "administration_id": str(self.administration_id),
            "retention_until": self.retention_until.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RetentionSweepReport:
    checked_at: datetime
    #: How many documents `documents.expired()` returned, whatever happened
    #: to them next. Zero means nothing was due - not that nothing was
    #: checked; unlike `api.ledger.integrity`'s sweep this one has no
    #: cross-tenant "examined nothing" ambiguity to guard against, because an
    #: empty result is the ordinary, expected state of a healthy archive.
    examined: int
    deleted: tuple[uuid.UUID, ...] = ()
    exceptions: tuple[RetentionException, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.exceptions

    def summary(self) -> str:
        if self.examined == 0:
            return "document retention sweep: nothing past retention"
        if self.clean:
            return (
                f"document retention sweep: {len(self.deleted)} document(s) removed, no exceptions"
            )
        return (
            f"document retention sweep: {len(self.deleted)} document(s) removed, "
            f"{len(self.exceptions)} exception(s) - retention expired but the "
            "document could not be removed"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "examined": self.examined,
            "deleted": [str(document_id) for document_id in self.deleted],
            "exceptions": [exc.as_dict() for exc in self.exceptions],
        }


class DocumentRetentionRepository(Protocol):
    """`documents.expired()` and the one statement that removes a row.

    Both run on the caller's own connection, so tenant scope is RLS's:
    `ledgr_app` sees one administration, `ledgr_ops` (BYPASSRLS) sees every
    one and needs no per-tenant loop - the same division `api.ledger.
    integrity.IntegrityRepository` documents for its own two reads.
    """

    async def expired(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[ExpiredDocument]: ...

    async def delete(self, *, document_id: uuid.UUID) -> None: ...


class RetentionSweepAlerter(Protocol):
    async def exceptions_found(self, report: RetentionSweepReport) -> None: ...


class LoggingRetentionSweepAlerter:
    """Structured logs - a real alerting channel, not a stub. See
    `api.ledger.integrity.LoggingIntegrityAlerter` for the same argument.
    """

    async def exceptions_found(self, report: RetentionSweepReport) -> None:
        logger.warning(
            "document_retention_exception",
            extra={
                "event": "document_retention_exception",
                "requirement": "PRIV-030",
                "checked_at": report.checked_at.isoformat(),
                "examined": report.examined,
                "deleted": len(report.deleted),
                "exceptions": len(report.exceptions),
                "summary": report.summary(),
            },
        )
        for exc in report.exceptions:
            logger.warning(
                "document_retention_exception_detail",
                extra={"event": "document_retention_exception_detail", **exc.as_dict()},
            )


class DocumentRetentionSweepJob:
    """PRIV-030, runnable nightly (`scripts/enforce_document_retention.py`) or
    on demand. Stateless: running it twice against the same state produces
    the same report, and a document already removed simply is not a
    candidate the second time.
    """

    def __init__(
        self,
        repository: DocumentRetentionRepository,
        alerter: RetentionSweepAlerter | None = None,
    ) -> None:
        self._repository = repository
        self._alerter: RetentionSweepAlerter = alerter or LoggingRetentionSweepAlerter()

    async def run(self, *, administration_id: uuid.UUID | None = None) -> RetentionSweepReport:
        checked_at = datetime.now(UTC)
        candidates = await self._repository.expired(administration_id=administration_id)

        deleted: list[uuid.UUID] = []
        exceptions: list[RetentionException] = []

        for candidate in candidates:
            if candidate.blocking_link_count > 0:
                exceptions.append(
                    RetentionException(
                        document_id=candidate.id,
                        organization_id=candidate.organization_id,
                        administration_id=candidate.administration_id,
                        retention_until=candidate.retention_until,
                        reason=(
                            f"retention expired on {candidate.retention_until.isoformat()} but "
                            f"{candidate.blocking_link_count} document_posting_link row(s) "
                            "still name this document (FR-DOC-003); those links are never "
                            "deleted, so this document cannot be removed while they exist"
                        ),
                    )
                )
                continue
            await self._repository.delete(document_id=candidate.id)
            deleted.append(candidate.id)

        report = RetentionSweepReport(
            checked_at=checked_at,
            examined=len(candidates),
            deleted=tuple(deleted),
            exceptions=tuple(exceptions),
        )
        if not report.clean:
            await self._alerter.exceptions_found(report)
        return report

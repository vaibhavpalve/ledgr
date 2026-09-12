"""IAM-109: what the client can see about who is in their books.

    "The client sees, at any time and without asking, which firm users hold
     access to their administration, with what role, since when, and when
     each last accessed it."

Four columns, and three of them already existed. role_assignment has carried
WHO (user_id), WITH WHAT ROLE (role_id) and SINCE WHEN (created_at) since
0009; migration 0016 added the provenance that says which of those rows are
the FIRM's rather than the client's own. Only "when each last accessed it"
needed somewhere to live, which is administration_access (0017).

--- "at any time and without asking" is a requirement about power, not UX ---

The phrase rules out two designs that would otherwise be reasonable. It rules
out a report the firm generates and sends, because that makes the client's
visibility depend on the firm's cooperation - the party the visibility exists
to check. And it rules out anything the firm can switch off, which is why
this reads live rows through the client's own tenant context rather than
through a setting.

The RLS policy on administration_access is where that actually holds: the
administration's owning organization can read every access record on it. No
firm involvement, nothing to request, nothing to enable.

--- Recording an access is throttled, and that is a stated tradeoff ---

Writing a row on every request would put a write on the read path of every
page a firm user opens. AdministrationAccessRecorder collapses repeated
accesses within a window into one update, so "last accessed" is accurate to
within that window rather than to the millisecond. IAM-109 asks the client to
be able to see when someone was last in their books; a few minutes of
granularity answers that, and an exact-to-the-request answer would cost a
write per request forever.

The window is a lower bound on staleness, never an upper one on truth: the
recorded timestamp is always a real access, only possibly an earlier one than
the most recent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

# Repeated accesses inside this window collapse into the row already
# written. Five minutes is short enough that "when were they last in my
# books" is answered usefully and long enough that a firm user clicking
# through a client's ledger does not generate a write per click.
ACCESS_RECORD_WINDOW = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FirmAccessEntry:
    """One row of the client-facing register. Named for what the client
    reads, not for the tables it comes from.
    """

    user_id: uuid.UUID
    email: str
    firm_organization_id: uuid.UUID
    firm_name: str
    role_name: str
    granted_at: datetime
    granted_by_user_id: uuid.UUID
    expires_at: datetime | None = None
    last_accessed_at: datetime | None = None
    first_accessed_at: datetime | None = None
    access_count: int = 0

    @property
    def has_ever_accessed(self) -> bool:
        """Distinguishes "holds access and has never used it" from "holds
        access and was here this morning". Both matter to a client reviewing
        who can see their books, and a null timestamp rendered as a blank
        cell would blur them - so the register states it as a fact rather
        than leaving the reader to infer it from a missing value.
        """
        return self.last_accessed_at is not None


class FirmAccessRegisterRepository(Protocol):
    async def list_firm_access(
        self, *, administration_id: uuid.UUID, now: datetime
    ) -> Sequence[FirmAccessEntry]: ...

    async def record_access(
        self,
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
        window: timedelta,
    ) -> bool:
        """Upserts the access row, collapsing repeats inside `window`.
        Returns whether a write actually happened, which is only interesting
        to tests and metrics - a caller has nothing to do differently either
        way.
        """
        ...


class FirmAccessRegister:
    """The client-facing answer to "who from my accountant can see this, and
    when were they last here".
    """

    def __init__(
        self,
        repository: FirmAccessRegisterRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def list_firm_access(self, administration_id: uuid.UUID) -> Sequence[FirmAccessEntry]:
        """Every LIVE firm grant on this administration.

        Expired and revoked grants are excluded: the question IAM-109 asks is
        "who holds access", present tense, and a client scanning this list to
        decide whether to revoke an engagement must not have to work out which
        rows are still in force. The history of who once held access is a
        different question, and one the append-only role_assignment table can
        still answer.

        Ordered by last access, most recent first, so the people who have
        actually been in the books appear before the ones who merely could be.
        """
        return await self._repository.list_firm_access(
            administration_id=administration_id, now=self._clock()
        )

    async def record_access(self, *, user_id: uuid.UUID, administration_id: uuid.UUID) -> bool:
        """Called when a user actually reads something in an administration.

        Records for CLIENT users too, not only firm staff. The register shows
        the client the firm's activity, but the same row is what a firm needs
        for its own review, and filtering at write time would mean deciding
        who counts as firm staff on the hot path - a decision the read side
        already makes from provenance, correctly and once.

        Not wired into a middleware: there is no HTTP surface that reads an
        administration's contents yet. api.authz.dependencies.require_permission
        is where a call belongs once there is - it already resolves the
        administration id and the user, and runs on exactly the requests that
        constitute an access.
        """
        return await self._repository.record_access(
            user_id=user_id,
            administration_id=administration_id,
            at=self._clock(),
            window=ACCESS_RECORD_WINDOW,
        )

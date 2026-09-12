"""The audit log: IAM-090 through IAM-093 (PRD §8.10).

    IAM-090  append-only, covering authentication, permission grants and
             revocations, data reads of financial records, exports, postings,
             approvals, filings, configuration changes and support access
    IAM-091  actor, actor type, tenant, resource, action, outcome, timestamp
             (UTC), source IP, user agent, correlation ID
    IAM-092  immutable and tamper-evident. No role, including Owner or LEDGR
             staff, can edit or delete entries
    IAM-093  retained 7 years, matching fiscal record retention

--- What this module does NOT do ---

It does not enforce immutability. That is entirely in migration 0019: no
UPDATE or DELETE privilege for any role, triggers that reject both
unconditionally, ownership by a NOLOGIN role so the triggers cannot be
dropped by the migration role, and hash chaining so tampering by anyone who
CAN drop them is still detectable. An application-level guarantee would be
worth nothing here - the threat model for an audit log explicitly includes
someone with a database connection.

Nor does it compute the hash. The sealing trigger derives `entry_hash`,
`previous_hash`, `sequence_number` and `recorded_at` from the row's own
values and overwrites anything supplied. An application that could name its
own hash could forge a consistent chain, so it is not given the chance.

What this module does is give callers one typed way to append, with the
IAM-091 field set as required arguments rather than a dict nobody validates.

--- Why appending cannot be optional ---

An audit log that a caller may forget is not an audit log. The pattern used
everywhere else in this codebase applies: `record()` is called from the
places that already run on every relevant request - api.authz.dependencies
for authorization outcomes, and the services that perform the actions
IAM-090 names. Where a category has no caller yet because the feature does
not exist (postings, filings), the category is still declared, and
tests/audit/test_coverage.py fails if a category IAM-090 names disappears
from the enum.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


class AuditCategory(enum.Enum):
    """IAM-090's coverage list, one member per clause of the sentence.

    Closed, and mirrored by a CHECK constraint in 0019. A category the
    requirement names but the code never emits is a gap; one the code emits
    that the requirement does not name is scope creep. Keeping the set here
    and in the database means neither can happen quietly.
    """

    AUTHENTICATION = "authentication"
    PERMISSION_CHANGE = "permission_change"
    FINANCIAL_READ = "financial_read"
    EXPORT = "export"
    POSTING = "posting"
    APPROVAL = "approval"
    FILING = "filing"
    CONFIGURATION = "configuration"
    SUPPORT_ACCESS = "support_access"


class ActorType(enum.Enum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    #: Scheduled jobs and internal processes - api/scripts, key rotation.
    SYSTEM = "system"
    #: LEDGR staff acting on a tenant's data. Its own type because IAM-090
    #: names support access separately from ordinary user activity, and a
    #: tenant reviewing their log needs to see which was which.
    SUPPORT = "support"


class AuditOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    #: Refused by authorization. Distinct from FAILURE, which is an attempt
    #: that was permitted and did not work: a denied action is a security
    #: signal and a failed one is usually an operational signal.
    DENIED = "denied"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """IAM-091's field set. The required arguments are required because a log
    entry missing its actor or its outcome cannot answer the question the log
    exists to answer, and a default would let it be omitted silently.

    `occurred_at` defaults to now but is caller-supplied, because a service
    recording something that happened a moment ago should say when it
    happened. `recorded_at` is not here at all: the database sets it from the
    server clock, and having both is what lets a backdated entry be told from
    a late-recorded one.
    """

    organization_id: uuid.UUID
    category: AuditCategory
    action: str
    resource_type: str
    outcome: AuditOutcome
    actor_type: ActorType

    actor_user_id: uuid.UUID | None = None
    administration_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    occurred_at: datetime | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    correlation_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("an audit entry needs an action")
        if not self.resource_type.strip():
            raise ValueError("an audit entry needs a resource type")
        # A user-attributed entry with no user is the shape that makes a log
        # useless in review: it looks attributed and names nobody.
        if self.actor_type is ActorType.USER and self.actor_user_id is None:
            raise ValueError("an actor_type of 'user' requires an actor_user_id")


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """A sealed entry, as read back. Carries the chain fields the database
    assigned so a caller can verify or anchor without a second query.
    """

    id: uuid.UUID
    sequence_number: int
    previous_hash: str
    entry_hash: str
    organization_id: uuid.UUID
    category: AuditCategory
    action: str
    resource_type: str
    outcome: AuditOutcome
    actor_type: ActorType
    recorded_at: datetime
    occurred_at: datetime
    actor_user_id: uuid.UUID | None = None
    administration_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    correlation_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChainBreak:
    sequence_number: int
    entry_id: uuid.UUID
    reason: str


@dataclass(frozen=True, slots=True)
class ChainHead:
    """What an external anchor stores (IAM-092).

    Anchoring this outside the database is what extends tamper-evidence to
    cover a superuser, who can otherwise rewrite an entry and recompute every
    hash after it. See scripts/anchor_audit_chain.py.
    """

    organization_id: uuid.UUID
    sequence_number: int
    head_hash: str
    entries: int

    @property
    def is_empty(self) -> bool:
        return self.entries == 0


@dataclass(frozen=True, slots=True)
class VerificationResult:
    organization_id: uuid.UUID
    entries: int
    head_hash: str | None
    break_found: ChainBreak | None = None

    @property
    def intact(self) -> bool:
        return self.break_found is None


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent, *, recorded_at: datetime) -> AuditEntry: ...

    async def verify_chain(self, organization_id: uuid.UUID) -> ChainBreak | None: ...

    async def chain_head(self, organization_id: uuid.UUID) -> ChainHead: ...

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        categories: Sequence[AuditCategory] | None = None,
        actor_user_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEntry]: ...


class AuditLog:
    """The one way to append. Thin on purpose - the guarantees live in the
    schema, and a service that did clever things here would only be adding
    somewhere for them to go wrong.
    """

    def __init__(
        self,
        repository: AuditRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def record(self, event: AuditEvent) -> AuditEntry:
        return await self._repository.append(event, recorded_at=self._clock())

    async def verify(self, organization_id: uuid.UUID) -> VerificationResult:
        """IAM-092's tamper-evidence, as an answer a caller can act on.

        Reports the FIRST break rather than every inconsistency: once a link
        is broken every entry after it fails too, so a list would be one real
        finding followed by noise.
        """
        head = await self._repository.chain_head(organization_id)
        return VerificationResult(
            organization_id=organization_id,
            entries=head.entries,
            head_hash=head.head_hash or None,
            break_found=await self._repository.verify_chain(organization_id),
        )

    async def head(self, organization_id: uuid.UUID) -> ChainHead:
        return await self._repository.chain_head(organization_id)

    async def search(
        self,
        *,
        organization_id: uuid.UUID,
        categories: Sequence[AuditCategory] | None = None,
        actor_user_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEntry]:
        """IAM-094's read side. The requirement (customers search and export
        without contacting support) also needs an HTTP surface, which is not
        built; this is the query it will call.
        """
        return await self._repository.search(
            organization_id=organization_id,
            categories=categories,
            actor_user_id=actor_user_id,
            since=since,
            until=until,
            limit=min(limit, 1000),
        )

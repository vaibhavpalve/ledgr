"""Idempotency keys on every mutating endpoint: NFR-032.

    NFR-032  Idempotency keys on all mutating API endpoints so retries cannot
             double-post.

--- Why this is middleware and not a dependency ---

The other "cannot forget" guarantees in this codebase are declared per route:
`require_permission(...)` names a permission, `audit=...` names a category,
and a coverage check fails the build when a route declares neither. That
shape is right when the route has to say something only it knows.

Idempotency has nothing route-specific to say. Every mutating endpoint wants
the same behaviour, so declaring it per route would be pure ceremony - and a
new endpoint would be unprotected until someone remembered the ceremony.
Applying it in middleware to every POST/PUT/PATCH/DELETE inverts that: a new
endpoint is covered the moment it is registered, and *opting out* is the
thing that takes a deliberate act.

tests/test_idempotency_coverage.py then guards the opt-out list rather than
the opt-in one.

--- The four outcomes ---

    CLAIMED     first time this key has been seen; run the handler
    REPLAY      a completed request with a matching fingerprint; serve its
                stored response without running the handler
    IN_FLIGHT   a duplicate is executing right now; 409, retry shortly
    MISMATCH    the key was reused for a DIFFERENT request; 422

MISMATCH is the one worth being strict about. Serving the first response for
a different request would be silently wrong in the most expensive possible
way: the caller believes their second, different posting succeeded. Refusing
tells them their client has a bug. This follows
draft-ietf-httpapi-idempotency-key-header, which specifies 422 for exactly
this case.
"""

from __future__ import annotations

import enum
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: How long a key is honoured. Long enough for any realistic client retry -
#: a mobile app resuming after a tunnel, a worker retrying a queue item, an
#: overnight batch re-run - and short enough that the table stays small and a
#: key can eventually be reused.
#:
#: The honest consequence: a retry AFTER this window re-executes at the HTTP
#: layer. For postings that is still caught by journal_entry.idempotency_key
#: (0020), which never expires. For anything else it is not caught, which is
#: why the window is generous rather than tight.
DEFAULT_TTL = timedelta(hours=24)

#: RFC-style guidance is that the key is opaque to the server. It still has to
#: fit in a column and not be a denial-of-service vector, so it is bounded.
MAX_KEY_LENGTH = 255

HEADER = "Idempotency-Key"
#: Set on a served-from-store response so a client can tell a replay from a
#: fresh execution. Purely informational - the body and status are identical
#: either way, which is the point.
REPLAY_HEADER = "Idempotent-Replay"

#: The only response headers carried into a replay. An allow-list, not a
#: deny-list: replaying Set-Cookie or an Authorization echo from the first
#: caller's response would hand one request's credentials to another.
REPLAYABLE_HEADERS = frozenset({"content-type", "location", "x-correlation-id"})

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class Outcome(enum.Enum):
    CLAIMED = "claimed"
    REPLAY = "replay"
    IN_FLIGHT = "in_flight"
    MISMATCH = "mismatch"


class IdempotencyError(Exception):
    pass


class MissingKey(IdempotencyError):
    """NFR-032 says keys are required, so a mutating request without one is
    refused rather than executed unprotected.

    Refusing is the whole point: accepting it would mean the endpoint is
    idempotent only for clients that opted in, which is the same as not being
    idempotent.
    """


class InvalidKey(IdempotencyError):
    pass


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: Outcome
    response: StoredResponse | None = None

    @property
    def should_run_handler(self) -> bool:
        return self.outcome is Outcome.CLAIMED


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Everything that decides whether two requests are "the same request".

    The path TEMPLATE is part of the key; the CONCRETE path is part of the
    fingerprint. So reusing a key against a different administration is
    reported as a mismatch - a client bug the caller should hear about -
    rather than silently becoming a separate key that executes twice.
    """

    organization_id: uuid.UUID
    user_id: uuid.UUID
    method: str
    path_template: str
    key: str

    concrete_path: str
    query_string: str
    body: bytes

    def fingerprint(self) -> str:
        """sha256 over the parts of the request that must not differ between
        a request and its retry.

        Length-prefixed, for the reason 0019's audit hash is: without it,
        adjacent fields run together and two different requests can produce
        the same payload - a path of `/a/b` with query `c` hashing the same as
        `/a` with query `b/c`. Academic as an attack; the whole value of this
        column is that two different requests never collide.
        """
        parts = [
            self.method,
            self.concrete_path,
            self.query_string,
            self.body.decode("utf-8", errors="replace"),
        ]
        payload = "".join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_key(raw: str | None) -> str:
    if raw is None:
        raise MissingKey(
            f"this endpoint requires an {HEADER} header (NFR-032). Send a unique "
            "value per logical operation and reuse it verbatim when retrying."
        )
    key = raw.strip()
    if not key:
        raise InvalidKey(f"{HEADER} must not be blank")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidKey(f"{HEADER} must be at most {MAX_KEY_LENGTH} characters, got {len(key)}")
    return key


class IdempotencyRepository(Protocol):
    async def claim(self, identity: RequestIdentity, *, expires_at: datetime) -> ClaimResult: ...

    async def complete(self, identity: RequestIdentity, response: StoredResponse) -> None: ...

    async def release(self, identity: RequestIdentity) -> None: ...

    # No purge_expired here on purpose. Purging is cross-tenant, runs as
    # ledgr_ops, and belongs to scripts/purge_idempotency_keys.py - a request
    # handler that could delete other tenants' keys is a capability no
    # endpoint needs. Expiry itself is honoured by the claim (0022), so
    # correctness does not depend on that job running.


class IdempotencyStore:
    """Thin over the repository, for the same reason AuditLog is thin over
    its own: the guarantees are in the schema. Claiming is one statement, so
    the check and the claim cannot interleave - see
    app.claim_idempotency_key.
    """

    def __init__(
        self,
        repository: IdempotencyRepository,
        *,
        ttl: timedelta = DEFAULT_TTL,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._ttl = ttl
        self._clock = clock

    async def claim(self, identity: RequestIdentity) -> ClaimResult:
        return await self._repository.claim(identity, expires_at=self._clock() + self._ttl)

    async def complete(self, identity: RequestIdentity, response: StoredResponse) -> None:
        await self._repository.complete(identity, response)

    async def release(self, identity: RequestIdentity) -> None:
        """Called when the request failed in a way a retry could fix.

        Storing a 5xx and replaying it would make a transient failure
        permanent for the life of the key: the caller retries correctly and
        receives the same 500 forever.
        """
        await self._repository.release(identity)


def is_retryable_failure(status_code: int) -> bool:
    """Whether the outcome should RELEASE the key rather than be stored.

    5xx only. A 4xx is a deterministic answer to this exact request - a
    validation error, a permission denial, a conflict - and replaying it is
    correct: the retry would compute the same answer, and serving the stored
    one saves the round trip and keeps the response identical.

    A 5xx is not deterministic. The request's transaction has rolled back, so
    nothing was committed that a retry could duplicate.
    """
    return status_code >= 500

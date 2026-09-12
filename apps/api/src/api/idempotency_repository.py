"""SQLAlchemy-backed IdempotencyRepository over migration 0022.

Claiming is a single `app.claim_idempotency_key(...)` call, not a
SELECT-then-INSERT. Two concurrent first attempts would both see nothing and
both insert; one would then fail on the unique constraint at commit, after
doing the work. One statement with `ON CONFLICT DO NOTHING` means the loser
gets no row back, re-reads, and reports `in_flight` - before the handler runs.

The claim also commits in its OWN transaction, separate from the request's.
A claim that lived inside the request transaction would be invisible to a
concurrent duplicate until the first request finished, which is precisely the
window it exists to close.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.idempotency import (
    ClaimResult,
    Outcome,
    RequestIdentity,
    StoredResponse,
)

_CLAIM = """
    SELECT outcome, response_status, response_body, response_headers
      FROM app.claim_idempotency_key(
        :organization_id, :user_id, :method, :path_template, :key,
        :fingerprint, :expires_at)
"""

_COMPLETE = """
    SELECT app.complete_idempotency_key(
      :organization_id, :user_id, :method, :path_template, :key,
      :status, :body, cast(:headers as jsonb))
"""

_RELEASE = """
    SELECT app.release_idempotency_key(
      :organization_id, :user_id, :method, :path_template, :key)
"""


def _identity_params(identity: RequestIdentity) -> dict[str, object]:
    return {
        "organization_id": str(identity.organization_id),
        "user_id": str(identity.user_id),
        "method": identity.method,
        "path_template": identity.path_template,
        "key": identity.key,
    }


class SqlIdempotencyRepository:
    """Takes an ENGINE, not a session.

    Every other repository here takes the request's session, which is right
    when the work belongs to the request's transaction. This one must not:
    the claim has to be visible to other requests before the handler runs, and
    the completion has to survive a rollback of the request that produced it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _tenant_session(self, identity: RequestIdentity):  # type: ignore[no-untyped-def]
        session = self._sessions()
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(identity.organization_id)},
        )
        return session

    async def claim(self, identity: RequestIdentity, *, expires_at: datetime) -> ClaimResult:
        session = await self._tenant_session(identity)
        async with session:
            result = await session.execute(
                text(_CLAIM),
                {
                    **_identity_params(identity),
                    "fingerprint": identity.fingerprint(),
                    "expires_at": expires_at,
                },
            )
            row = result.one()
            await session.commit()

        outcome = Outcome(row.outcome)
        if outcome is not Outcome.REPLAY:
            return ClaimResult(outcome)

        raw_headers = row.response_headers
        headers = (json.loads(raw_headers) if isinstance(raw_headers, str) else raw_headers) or {}
        return ClaimResult(
            outcome,
            StoredResponse(
                status_code=int(row.response_status),
                body=bytes(row.response_body or b""),
                headers={str(k): str(v) for k, v in headers.items()},
            ),
        )

    async def complete(self, identity: RequestIdentity, response: StoredResponse) -> None:
        session = await self._tenant_session(identity)
        async with session:
            await session.execute(
                text(_COMPLETE),
                {
                    **_identity_params(identity),
                    "status": response.status_code,
                    "body": response.body,
                    "headers": json.dumps(response.headers, sort_keys=True),
                },
            )
            await session.commit()

    async def release(self, identity: RequestIdentity) -> None:
        session = await self._tenant_session(identity)
        async with session:
            await session.execute(text(_RELEASE), _identity_params(identity))
            await session.commit()

    # There is deliberately no purge_expired() here. The purge is cross-tenant
    # and runs as ledgr_ops; this class is built over the request-path engine
    # (ledgr_app), which holds no EXECUTE on that function. Putting it here
    # would be a method that always fails, or an argument for granting a
    # capability no endpoint needs. It lives in
    # scripts/purge_idempotency_keys.py instead.
    #
    # Expiry does not depend on that job: app.claim_idempotency_key drops an
    # expired row for the key it is claiming, so a stale key is reclaimed the
    # moment it is next used. The job only reclaims space.

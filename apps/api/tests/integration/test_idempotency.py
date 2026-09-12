"""NFR-032 against a real Postgres: the storage, the collision, the expiry.

tests/test_idempotency_middleware.py covers the behaviour against an
in-memory store. This file covers what only a database can show:

  * the claim is ATOMIC - two concurrent first attempts do not both execute
  * the unique constraint is what makes that true, not the application
  * a replay cannot cross a tenant boundary (RLS)
  * expiry actually removes rows, and the purge is cross-tenant

The atomicity test is the one that matters. Every other guarantee here is a
consequence of a single `INSERT ... ON CONFLICT DO NOTHING`, and a
SELECT-then-INSERT written by someone who did not know that would pass every
in-memory test in the suite and double-post under load.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from api.config import settings
from api.db import engine as app_engine
from api.idempotency import Outcome, RequestIdentity, StoredResponse
from api.idempotency_repository import SqlIdempotencyRepository
from tests.support.seed import SeededTenants


def _identity(
    tenants: SeededTenants,
    *,
    key: str = "req-1",
    body: bytes = b'{"amount":"10.00"}',
    user_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    path_template: str = "/v1/entries",
) -> RequestIdentity:
    return RequestIdentity(
        organization_id=organization_id or tenants.org_a,
        user_id=user_id or tenants.owner_a,
        method="POST",
        path_template=path_template,
        key=key,
        concrete_path="/v1/entries",
        query_string="",
        body=body,
    )


def _expiry(hours: int = 24) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


# ===========================================================================
# The claim
# ===========================================================================


async def test_a_first_claim_succeeds_and_a_second_replays(
    two_organizations: SeededTenants,
) -> None:
    repository = SqlIdempotencyRepository(app_engine)
    identity = _identity(two_organizations)

    first = await repository.claim(identity, expires_at=_expiry())
    assert first.outcome is Outcome.CLAIMED

    # Before completion the duplicate is IN_FLIGHT - there is no stored
    # response to serve yet.
    assert (await repository.claim(identity, expires_at=_expiry())).outcome is (Outcome.IN_FLIGHT)

    await repository.complete(
        identity,
        StoredResponse(
            status_code=201,
            body=b'{"id":"abc"}',
            headers={"content-type": "application/json"},
        ),
    )

    replay = await repository.claim(identity, expires_at=_expiry())
    assert replay.outcome is Outcome.REPLAY
    assert replay.response is not None
    assert replay.response.status_code == 201
    assert replay.response.body == b'{"id":"abc"}'
    assert replay.response.headers == {"content-type": "application/json"}


async def test_two_concurrent_claims_produce_exactly_one_winner(
    two_organizations: SeededTenants,
) -> None:
    """The test this file exists for.

    A SELECT-then-INSERT would let both callers see nothing and both proceed;
    one would fail on the unique constraint at COMMIT, after the work was
    done. `INSERT ... ON CONFLICT DO NOTHING` means the loser gets no row
    back, re-reads, and reports in_flight - before the handler runs.

    Twenty concurrent attempts rather than two, because a race that resolves
    correctly by luck at two often does not at twenty.
    """
    repository = SqlIdempotencyRepository(app_engine)
    identity = _identity(two_organizations, key=f"race-{uuid.uuid4()}")

    results = await asyncio.gather(
        *(repository.claim(identity, expires_at=_expiry()) for _ in range(20))
    )
    outcomes = [result.outcome for result in results]

    assert outcomes.count(Outcome.CLAIMED) == 1, (
        f"expected exactly one winner, got {outcomes.count(Outcome.CLAIMED)}: "
        f"{outcomes}. More than one means the handler would have run more than "
        "once for a single key (NFR-032)."
    )
    assert set(outcomes) <= {Outcome.CLAIMED, Outcome.IN_FLIGHT}


async def test_a_different_body_under_the_same_key_is_a_mismatch(
    two_organizations: SeededTenants,
) -> None:
    repository = SqlIdempotencyRepository(app_engine)
    key = f"mismatch-{uuid.uuid4()}"

    await repository.claim(_identity(two_organizations, key=key), expires_at=_expiry())
    result = await repository.claim(
        _identity(two_organizations, key=key, body=b'{"amount":"99.00"}'),
        expires_at=_expiry(),
    )

    assert result.outcome is Outcome.MISMATCH


async def test_the_mismatch_is_reported_before_the_in_flight_state(
    two_organizations: SeededTenants,
) -> None:
    """Fingerprint before state, in SQL as in Python.

    Reporting a reused key as "still in flight" would send the caller into a
    retry loop that can never succeed: the fingerprint will never match.
    """
    repository = SqlIdempotencyRepository(app_engine)
    key = f"order-{uuid.uuid4()}"

    # Left in_progress deliberately - never completed.
    await repository.claim(_identity(two_organizations, key=key), expires_at=_expiry())

    result = await repository.claim(
        _identity(two_organizations, key=key, body=b"different"),
        expires_at=_expiry(),
    )

    assert result.outcome is Outcome.MISMATCH


# ===========================================================================
# Scoping
# ===========================================================================


async def test_the_same_key_for_a_different_user_is_a_different_key(
    two_organizations: SeededTenants,
) -> None:
    """The stored body is whatever the first caller was allowed to see, so a
    key shared across users would be a cross-user leak wearing an idempotency
    key. user_id is in the unique constraint for exactly this.
    """
    repository = SqlIdempotencyRepository(app_engine)
    key = f"shared-{uuid.uuid4()}"
    other_user = await _seed_user(two_organizations.org_a)

    first = await repository.claim(_identity(two_organizations, key=key), expires_at=_expiry())
    second = await repository.claim(
        _identity(two_organizations, key=key, user_id=other_user),
        expires_at=_expiry(),
    )

    assert first.outcome is Outcome.CLAIMED
    assert second.outcome is Outcome.CLAIMED


async def test_the_same_key_on_a_different_endpoint_is_a_different_key(
    two_organizations: SeededTenants,
) -> None:
    repository = SqlIdempotencyRepository(app_engine)
    key = f"endpoint-{uuid.uuid4()}"

    first = await repository.claim(_identity(two_organizations, key=key), expires_at=_expiry())
    second = await repository.claim(
        _identity(two_organizations, key=key, path_template="/v1/invoices"),
        expires_at=_expiry(),
    )

    assert first.outcome is Outcome.CLAIMED
    assert second.outcome is Outcome.CLAIMED


async def test_one_tenant_cannot_see_anothers_idempotency_records(
    two_organizations: SeededTenants,
) -> None:
    """IAM-005. A stored response body is one tenant's data; RLS is what stops
    a replay serving it to another.
    """
    repository = SqlIdempotencyRepository(app_engine)
    await repository.claim(
        _identity(two_organizations, key=f"tenant-{uuid.uuid4()}"),
        expires_at=_expiry(),
    )

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        visible = (await conn.execute(text("SELECT count(*) FROM idempotency_key"))).scalar_one()

    assert visible == 0


# ===========================================================================
# Failure and expiry
# ===========================================================================


async def test_releasing_lets_a_retry_claim_again(
    two_organizations: SeededTenants,
) -> None:
    repository = SqlIdempotencyRepository(app_engine)
    identity = _identity(two_organizations, key=f"release-{uuid.uuid4()}")

    await repository.claim(identity, expires_at=_expiry())
    await repository.release(identity)

    assert (await repository.claim(identity, expires_at=_expiry())).outcome is (Outcome.CLAIMED)


async def test_releasing_a_completed_key_does_nothing(
    two_organizations: SeededTenants,
) -> None:
    """release() is for a request that FAILED. A completed key must survive
    it, or a stray release would let a successful request be executed twice.
    """
    repository = SqlIdempotencyRepository(app_engine)
    identity = _identity(two_organizations, key=f"completed-{uuid.uuid4()}")

    await repository.claim(identity, expires_at=_expiry())
    await repository.complete(identity, StoredResponse(status_code=200, body=b"{}", headers={}))
    await repository.release(identity)

    assert (await repository.claim(identity, expires_at=_expiry())).outcome is (Outcome.REPLAY)


async def test_an_expired_key_can_be_claimed_again(
    two_organizations: SeededTenants,
) -> None:
    """The honest limit, against the real thing: after the window a retry
    re-executes. Postings are still covered by journal_entry.idempotency_key
    (0020), which never expires; nothing else is.

    Note what this does NOT do: it never calls the purge job. Expiry is
    honoured by the claim itself, which drops an expired row for the key it is
    claiming. If it were not, expiry would mean nothing until a cron happened
    to run - correctness depending on a schedule.
    """
    repository = SqlIdempotencyRepository(app_engine)
    identity = _identity(two_organizations, key=f"expired-{uuid.uuid4()}")

    await repository.claim(identity, expires_at=datetime.now(UTC) - timedelta(hours=1))
    await repository.complete(identity, StoredResponse(status_code=200, body=b"{}", headers={}))

    assert (await repository.claim(identity, expires_at=_expiry())).outcome is (Outcome.CLAIMED)


async def test_the_purge_leaves_unexpired_rows_alone(
    two_organizations: SeededTenants,
) -> None:
    """The purge reclaims space; it must not remove a live key.

    Run as ledgr_ops, which is the role the scheduled job uses: the function
    is SECURITY DEFINER over a cross-tenant DELETE, and ledgr_app deliberately
    holds no EXECUTE on it.
    """
    repository = SqlIdempotencyRepository(app_engine)
    live = _identity(two_organizations, key=f"live-{uuid.uuid4()}")
    stale = _identity(two_organizations, key=f"stale-{uuid.uuid4()}")

    await repository.claim(live, expires_at=_expiry())
    await repository.complete(live, StoredResponse(status_code=200, body=b"{}", headers={}))
    await repository.claim(stale, expires_at=datetime.now(UTC) - timedelta(hours=1))

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET ROLE ledgr_ops"))
            deleted = (
                await conn.execute(text("SELECT app.purge_expired_idempotency_keys() AS deleted"))
            ).scalar_one()
            await conn.commit()
    finally:
        await engine.dispose()

    assert deleted >= 1, "the purge removed nothing, including the expired row"
    assert (await repository.claim(live, expires_at=_expiry())).outcome is (Outcome.REPLAY), (
        "the purge removed a live key"
    )


# ===========================================================================
# Schema guards
# ===========================================================================


async def test_a_completed_row_must_carry_a_response(
    two_organizations: SeededTenants,
) -> None:
    """A bug that marked a row completed without storing the body would
    surface as a retry receiving an empty 200 - the worst possible failure
    here, because it looks like success.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(
                text(
                    "INSERT INTO idempotency_key (organization_id, user_id, method, "
                    " path_template, idempotency_key, request_fingerprint, state, "
                    " expires_at) "
                    "VALUES (:org, :user, 'POST', '/v1/x', :key, 'fp', 'completed', "
                    "        now() + interval '1 hour')"
                ),
                {
                    "org": str(two_organizations.org_a),
                    "user": str(two_organizations.owner_a),
                    "key": f"bad-{uuid.uuid4()}",
                },
            )
            await conn.commit()
        await conn.rollback()

    assert "completed_has_a_response" in str(raised.value)


async def test_a_blank_key_is_rejected_by_the_database(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)):
            await conn.execute(
                text(
                    "INSERT INTO idempotency_key (organization_id, user_id, method, "
                    " path_template, idempotency_key, request_fingerprint, expires_at) "
                    "VALUES (:org, :user, 'POST', '/v1/x', '   ', 'fp', "
                    "        now() + interval '1 hour')"
                ),
                {
                    "org": str(two_organizations.org_a),
                    "user": str(two_organizations.owner_a),
                },
            )
            await conn.commit()
        await conn.rollback()


async def test_the_application_role_cannot_purge_other_tenants_keys(
    two_organizations: SeededTenants,
) -> None:
    """Purging is cross-tenant and belongs to the maintenance job, not to a
    request handler. ledgr_app holds no EXECUTE on the purge function.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        with pytest.raises((DBAPIError, SQLAlchemyError)) as raised:
            await conn.execute(text("SELECT app.purge_expired_idempotency_keys()"))
            await conn.commit()
        await conn.rollback()

    assert "permission denied" in str(raised.value).lower()


async def _seed_user(organization_id: uuid.UUID) -> uuid.UUID:
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO users (email) VALUES (:email) RETURNING id"),
            {"email": f"idem+{uuid.uuid4().hex[:10]}@example.com"},
        )
        return result.scalar_one()  # type: ignore[no-any-return]

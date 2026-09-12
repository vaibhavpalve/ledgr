"""NFR-032: a retried request cannot double-post.

    NFR-032  Idempotency keys on all mutating API endpoints so retries cannot
             double-post.

Driven through a probe app rather than api.main, for the reason
tests/test_ledger_posting_eligibility.py gives: the behaviour under test is
middleware, not any particular endpoint, and a probe route lets the handler
count its own executions - which is what "cannot double-post" actually means.

The handler here increments a counter and returns it. Every assertion below
is ultimately about that counter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.idempotency import (
    HEADER,
    REPLAY_HEADER,
    IdempotencyStore,
    RequestIdentity,
    validate_key,
)
from api.idempotency_middleware import IdempotencyMiddleware
from api.tenancy import TenantContext
from tests.support.fake_idempotency_repository import InMemoryIdempotencyRepository

ORG = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
OTHER_ORG = uuid.uuid4()


class _Tenant:
    """Stands in for TenantContextMiddleware, which is not under test here."""

    def __init__(self, app, *, organization_id=ORG, user_id=USER):  # type: ignore[no-untyped-def]
        self.app = app
        self.organization_id = organization_id
        self.user_id = user_id

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["tenant_context"] = TenantContext(
                organization_id=self.organization_id, user_id=self.user_id
            )
        await self.app(scope, receive, send)


def _probe(
    repository: InMemoryIdempotencyRepository | None = None,
    *,
    organization_id: uuid.UUID = ORG,
    user_id: uuid.UUID = USER,
    ttl: timedelta = timedelta(hours=24),
) -> tuple[TestClient, dict[str, int], InMemoryIdempotencyRepository]:
    repository = repository or InMemoryIdempotencyRepository()
    calls = {"count": 0}

    app = FastAPI()

    @app.post("/v1/things")
    def create_thing() -> dict[str, int]:
        calls["count"] += 1
        return {"execution": calls["count"]}

    @app.put("/v1/things/{thing_id}")
    def replace_thing(thing_id: uuid.UUID) -> dict[str, int]:
        calls["count"] += 1
        return {"execution": calls["count"]}

    @app.post("/v1/boom")
    def boom() -> dict[str, int]:
        calls["count"] += 1
        raise RuntimeError("handler exploded")

    @app.post("/v1/unavailable")
    def unavailable() -> dict[str, int]:
        calls["count"] += 1
        raise HTTPException(status_code=503, detail="try again")

    @app.post("/v1/refused")
    def refused() -> dict[str, int]:
        calls["count"] += 1
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/v1/things")
    def list_things() -> dict[str, int]:
        calls["count"] += 1
        return {"execution": calls["count"]}

    app.add_middleware(
        IdempotencyMiddleware,
        # ONE clock. The store computes expires_at and the repository decides
        # whether a record has expired; if those read different clocks, a test
        # that moves time forward moves only half the system and the result
        # means nothing.
        store=IdempotencyStore(repository, ttl=ttl, clock=lambda: repository.now),
    )
    app.add_middleware(_Tenant, organization_id=organization_id, user_id=user_id)
    return TestClient(app, raise_server_exceptions=False), calls, repository


# ===========================================================================
# The requirement itself
# ===========================================================================


def test_a_retried_request_does_not_execute_twice() -> None:
    client, calls, _ = _probe()
    headers = {HEADER: "req-1"}

    first = client.post("/v1/things", json={"amount": "10.00"}, headers=headers)
    second = client.post("/v1/things", json={"amount": "10.00"}, headers=headers)

    assert calls["count"] == 1, "the handler ran twice - NFR-032 is not satisfied"
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"execution": 1}


def test_the_replay_is_byte_identical_and_flagged() -> None:
    """The caller gets the same answer, so a retry is indistinguishable from
    the original as far as their code is concerned. The header is
    informational, for someone reading a trace.
    """
    client, _, _ = _probe()
    headers = {HEADER: "req-1"}

    first = client.post("/v1/things", json={"a": 1}, headers=headers)
    second = client.post("/v1/things", json={"a": 1}, headers=headers)

    assert first.content == second.content
    assert REPLAY_HEADER not in first.headers
    assert second.headers[REPLAY_HEADER] == "true"


def test_different_keys_execute_separately() -> None:
    """The negative space. Without this, a middleware that replayed everything
    - or executed nothing - would pass every test above.
    """
    client, calls, _ = _probe()

    client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})
    client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-2"})

    assert calls["count"] == 2


def test_a_get_is_untouched() -> None:
    """Only mutating methods. A GET needs no key and must not be replayed:
    reads are expected to reflect current state.
    """
    client, calls, _ = _probe()

    first = client.get("/v1/things")
    second = client.get("/v1/things")

    assert first.status_code == second.status_code == 200
    assert calls["count"] == 2


# ===========================================================================
# Collision behaviour
# ===========================================================================


def test_a_missing_key_is_refused() -> None:
    """NFR-032 says keys are required. Accepting a request without one would
    make the endpoint idempotent only for clients that opted in, which is the
    same as not being idempotent.
    """
    client, calls, _ = _probe()

    response = client.post("/v1/things", json={"a": 1})

    assert response.status_code == 400
    assert HEADER in response.json()["detail"]
    assert calls["count"] == 0, "the handler ran despite the request being refused"


@pytest.mark.parametrize("key", ["", "   ", "x" * 256])
def test_a_malformed_key_is_refused(key: str) -> None:
    client, calls, _ = _probe()

    response = client.post("/v1/things", json={"a": 1}, headers={HEADER: key})

    assert response.status_code == 400
    assert calls["count"] == 0


def test_the_same_key_with_a_different_body_is_refused() -> None:
    """The collision that matters.

    Serving the first response would be silently wrong in the most expensive
    way available: the caller believes their second, different posting
    succeeded. 422 per draft-ietf-httpapi-idempotency-key-header.
    """
    client, calls, _ = _probe()
    headers = {HEADER: "req-1"}

    client.post("/v1/things", json={"amount": "10.00"}, headers=headers)
    second = client.post("/v1/things", json={"amount": "99.00"}, headers=headers)

    assert second.status_code == 422
    assert calls["count"] == 1
    assert "different request" in second.json()["detail"]


def test_the_same_key_on_a_different_path_is_a_different_key() -> None:
    """The path TEMPLATE is part of the key, so "req-1" on two endpoints are
    two keys rather than a collision.
    """
    client, calls, _ = _probe()

    client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})
    client.put(f"/v1/things/{uuid.uuid4()}", json={"a": 1}, headers={HEADER: "req-1"})

    assert calls["count"] == 2


def test_the_same_key_against_a_different_id_is_a_mismatch() -> None:
    """The concrete path is part of the FINGERPRINT, not the key.

    So reusing a key against a different administration is reported to the
    caller as a bug rather than quietly becoming a separate key that executes
    twice - which is the failure that would silently double-post to the wrong
    client.
    """
    client, calls, _ = _probe()
    headers = {HEADER: "req-1"}

    client.put(f"/v1/things/{uuid.uuid4()}", json={"a": 1}, headers=headers)
    second = client.put(f"/v1/things/{uuid.uuid4()}", json={"a": 1}, headers=headers)

    assert second.status_code == 422
    assert calls["count"] == 1


def test_a_key_in_flight_is_reported_as_a_conflict() -> None:
    """A concurrent duplicate, simulated by leaving the first claim in
    in_progress. 409 and a retry-shortly message, not a replay: there is no
    stored response to serve yet.
    """
    repository = InMemoryIdempotencyRepository()
    client, calls, _ = _probe(repository)

    identity = RequestIdentity(
        organization_id=ORG,
        user_id=USER,
        method="POST",
        path_template="/v1/things",
        key="req-1",
        concrete_path="/v1/things",
        query_string="",
        body=b'{"a":1}',
    )
    import asyncio

    asyncio.run(repository.claim(identity, expires_at=datetime.now(UTC) + timedelta(hours=1)))

    response = client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})

    assert response.status_code == 409
    assert calls["count"] == 0


def test_a_mismatch_is_reported_even_while_the_first_is_in_flight() -> None:
    """Fingerprint before state, deliberately.

    Reporting a reused key as "still in flight" would send the caller into a
    retry loop that can never succeed - the fingerprint will never match.
    """
    repository = InMemoryIdempotencyRepository()
    client, _, _ = _probe(repository)

    identity = RequestIdentity(
        organization_id=ORG,
        user_id=USER,
        method="POST",
        path_template="/v1/things",
        key="req-1",
        concrete_path="/v1/things",
        query_string="",
        body=b'{"a":1}',
    )
    import asyncio

    asyncio.run(repository.claim(identity, expires_at=datetime.now(UTC) + timedelta(hours=1)))

    response = client.post("/v1/things", json={"totally": "different"}, headers={HEADER: "req-1"})

    assert response.status_code == 422


# ===========================================================================
# Scoping: whose key is it
# ===========================================================================


def test_one_users_key_does_not_replay_for_another() -> None:
    """The stored body is whatever the FIRST caller was allowed to see, so a
    key shared across users would be a cross-user data leak wearing an
    idempotency key.
    """
    repository = InMemoryIdempotencyRepository()
    first_client, calls, _ = _probe(repository, user_id=USER)
    second_client, _, _ = _probe(repository, user_id=OTHER_USER)

    first_client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})
    second = second_client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})

    assert second.status_code == 200
    assert REPLAY_HEADER not in second.headers
    assert calls["count"] == 1, "each client has its own counter"


def test_one_tenants_key_does_not_replay_for_another() -> None:
    repository = InMemoryIdempotencyRepository()
    first_client, _, _ = _probe(repository, organization_id=ORG)
    second_client, _, _ = _probe(repository, organization_id=OTHER_ORG)

    first_client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})
    second = second_client.post("/v1/things", json={"a": 1}, headers={HEADER: "req-1"})

    assert REPLAY_HEADER not in second.headers


# ===========================================================================
# Failure handling
# ===========================================================================


def test_a_5xx_releases_the_key_so_a_retry_is_a_real_retry() -> None:
    """Storing a 5xx and replaying it would make a transient failure
    permanent for the life of the key: the caller retries correctly and gets
    the same 503 back forever.
    """
    client, calls, repository = _probe()
    headers = {HEADER: "req-1"}

    first = client.post("/v1/unavailable", json={"a": 1}, headers=headers)
    assert first.status_code == 503
    assert repository.records == {}, "a retryable failure must not hold the key"

    second = client.post("/v1/unavailable", json={"a": 1}, headers=headers)

    assert second.status_code == 503
    assert calls["count"] == 2, "the retry must actually re-execute"


def test_an_unhandled_exception_releases_the_key() -> None:
    client, calls, repository = _probe()
    headers = {HEADER: "req-1"}

    client.post("/v1/boom", json={"a": 1}, headers=headers)

    assert repository.records == {}
    client.post("/v1/boom", json={"a": 1}, headers=headers)
    assert calls["count"] == 2


def test_releasing_does_not_drop_a_completed_key() -> None:
    """release() is for a request that FAILED.

    A release that also dropped completed rows would let a SUCCESSFUL request
    be executed twice - the exact thing NFR-032 forbids - and every other test
    here would still pass, because none of them releases after completing.
    """
    repository = InMemoryIdempotencyRepository()
    client, calls, _ = _probe(repository)
    headers = {HEADER: "req-1"}

    client.post("/v1/things", json={"a": 1}, headers=headers)

    identity = RequestIdentity(
        organization_id=ORG,
        user_id=USER,
        method="POST",
        path_template="/v1/things",
        key="req-1",
        concrete_path="/v1/things",
        query_string="",
        body=b'{"a":1}',
    )
    import asyncio

    asyncio.run(repository.release(identity))

    second = client.post("/v1/things", json={"a": 1}, headers=headers)

    assert calls["count"] == 1, "a completed key was released and re-executed"
    assert second.headers[REPLAY_HEADER] == "true"


def test_a_4xx_is_stored_and_replayed() -> None:
    """A denial is a deterministic answer to this exact request. Replaying it
    is correct - a retry would compute the same one - and it keeps a client
    that retries blindly from hammering an endpoint that will keep saying no.
    """
    client, calls, _ = _probe()
    headers = {HEADER: "req-1"}

    first = client.post("/v1/refused", json={"a": 1}, headers=headers)
    second = client.post("/v1/refused", json={"a": 1}, headers=headers)

    assert first.status_code == second.status_code == 403
    assert second.headers[REPLAY_HEADER] == "true"
    assert calls["count"] == 1


# ===========================================================================
# Expiry
# ===========================================================================


def test_an_expired_key_is_reclaimed_and_re_executes() -> None:
    """The honest limit, asserted rather than described.

    After the window a retry DOES execute again. For postings that is still
    caught by journal_entry.idempotency_key (0020), which never expires; for
    anything else it is not caught at all, which is why the window is
    generous.
    """
    repository = InMemoryIdempotencyRepository()
    client, calls, _ = _probe(repository, ttl=timedelta(hours=24))
    headers = {HEADER: "req-1"}

    client.post("/v1/things", json={"a": 1}, headers=headers)
    assert calls["count"] == 1

    repository.now = datetime.now(UTC) + timedelta(hours=25)
    second = client.post("/v1/things", json={"a": 1}, headers=headers)

    assert calls["count"] == 2
    assert REPLAY_HEADER not in second.headers
    assert second.json() == {"execution": 2}


def test_a_key_within_the_window_still_replays() -> None:
    """The other side of expiry. Without this, a TTL of zero would pass the
    test above and disable idempotency entirely.
    """
    repository = InMemoryIdempotencyRepository()
    client, calls, _ = _probe(repository, ttl=timedelta(hours=24))
    headers = {HEADER: "req-1"}

    client.post("/v1/things", json={"a": 1}, headers=headers)
    repository.now = datetime.now(UTC) + timedelta(hours=23)
    second = client.post("/v1/things", json={"a": 1}, headers=headers)

    assert calls["count"] == 1
    assert second.headers[REPLAY_HEADER] == "true"


def test_purging_removes_only_expired_rows() -> None:
    repository = InMemoryIdempotencyRepository()
    client, _, _ = _probe(repository, ttl=timedelta(hours=1))

    client.post("/v1/things", json={"a": 1}, headers={HEADER: "old"})
    repository.now = datetime.now(UTC) + timedelta(hours=2)
    client.post("/v1/things", json={"a": 2}, headers={HEADER: "new"})

    import asyncio

    deleted = asyncio.run(repository.purge_expired())

    assert deleted == 1
    assert len(repository.records) == 1


# ===========================================================================
# The key itself
# ===========================================================================


def test_the_fingerprint_is_length_prefixed() -> None:
    """Without length prefixing, adjacent fields run together and two
    different requests can produce the same payload - a path of `/a/b` with
    query `c` hashing the same as `/a` with query `b/c`. Academic as an
    attack; the whole value of the column is that two different requests never
    collide.
    """
    base = {
        "organization_id": ORG,
        "user_id": USER,
        "method": "POST",
        "path_template": "/v1/things",
        "key": "req-1",
        "body": b"",
    }
    # These two ACTUALLY collide under bare concatenation: "/ab" + "c" and
    # "/a" + "bc" both give "/abc". The first version of this test used
    # "/a/b"+"c" against "/a"+"b/c", which concatenate to "/a/bc" and "/ab/c"
    # - different strings, so it passed whether or not the prefixing was
    # there, and a mutation removing the prefixing survived it.
    left = RequestIdentity(**base, concrete_path="/ab", query_string="c")
    right = RequestIdentity(**base, concrete_path="/a", query_string="bc")

    assert left.fingerprint() != right.fingerprint()


def test_a_valid_key_is_returned_trimmed() -> None:
    assert validate_key("  req-1  ") == "req-1"

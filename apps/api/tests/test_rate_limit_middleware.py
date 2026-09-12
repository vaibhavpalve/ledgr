"""Proves IAM-019's rate limiting and progressive lockout at the HTTP
edge: RateLimitingMiddleware checks BEFORE the handler runs (a locked-out
request never reaches it) and records the real outcome AFTER, from the
response status code. No database - AuthRateLimiter is backed by
InMemoryAuthAttemptRepository, matching every other middleware test in
this suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.auth.rate_limiting import AuthRateLimiter
from api.rate_limit_middleware import ProtectedEndpoint, RateLimitingMiddleware
from tests.support.fake_anomaly_alerter import FakeAnomalyAlerter
from tests.support.fake_rate_limit_repository import InMemoryAuthAttemptRepository


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _probe_app(*, clock: _FakeClock, always_fail: bool = False) -> tuple[FastAPI, list[bool]]:
    handler_calls: list[bool] = []
    limiter = AuthRateLimiter(InMemoryAuthAttemptRepository(), FakeAnomalyAlerter(), clock=clock)
    app = FastAPI()
    app.add_middleware(
        RateLimitingMiddleware,
        limiter=limiter,
        protected_paths={("POST", "/v1/auth/login"): ProtectedEndpoint(endpoint="login")},
    )

    @app.post("/v1/auth/login")
    def login() -> dict[str, str]:
        handler_calls.append(True)
        if always_fail:
            raise HTTPException(status_code=401, detail="invalid credentials")
        return {"status": "ok"}

    @app.get("/v1/whoami")
    def whoami() -> dict[str, str]:
        handler_calls.append(True)
        return {"status": "ok"}

    return app, handler_calls


def test_an_unprotected_path_is_never_touched() -> None:
    app, handler_calls = _probe_app(clock=_FakeClock())
    client = TestClient(app)

    for _ in range(50):  # far more than any threshold, to prove no limiting applies here
        response = client.get("/v1/whoami")
        assert response.status_code == 200

    assert len(handler_calls) == 50


def test_a_request_with_no_recognizable_account_key_passes_through() -> None:
    app, handler_calls = _probe_app(clock=_FakeClock())
    client = TestClient(app)

    response = client.post("/v1/auth/login", json={"not_email": "irrelevant"})

    assert response.status_code == 200
    assert handler_calls == [True]


def test_repeated_failures_eventually_lock_the_account_out_before_the_handler_runs() -> None:
    clock = _FakeClock()
    app, handler_calls = _probe_app(clock=clock, always_fail=True)
    client = TestClient(app)

    responses = [
        client.post("/v1/auth/login", json={"email": "owner@example.com"}) for _ in range(5)
    ]
    assert [r.status_code for r in responses] == [401, 401, 401, 401, 401]
    assert len(handler_calls) == 5

    # The 6th attempt should be blocked by the lockout BEFORE the handler
    # runs at all - handler_calls must not grow.
    locked_response = client.post("/v1/auth/login", json={"email": "owner@example.com"})

    assert locked_response.status_code == 429
    assert locked_response.json()["reason"] == "locked_out"
    assert len(handler_calls) == 5  # unchanged - the handler did not run this time


def test_a_successful_attempt_is_not_rate_limited_as_a_failure() -> None:
    app, handler_calls = _probe_app(clock=_FakeClock(), always_fail=False)
    client = TestClient(app)

    for _ in range(5):
        response = client.post("/v1/auth/login", json={"email": "owner@example.com"})
        assert response.status_code == 200

    assert len(handler_calls) == 5


def test_different_accounts_are_rate_limited_independently() -> None:
    clock = _FakeClock()
    app, handler_calls = _probe_app(clock=clock, always_fail=True)
    client = TestClient(app)

    for _ in range(5):
        client.post("/v1/auth/login", json={"email": "victim@example.com"})

    locked_response = client.post("/v1/auth/login", json={"email": "victim@example.com"})
    assert locked_response.status_code == 429

    unrelated_response = client.post("/v1/auth/login", json={"email": "unrelated@example.com"})
    # 401, not 429: it reached the (always-failing) handler, not blocked.
    assert unrelated_response.status_code == 401

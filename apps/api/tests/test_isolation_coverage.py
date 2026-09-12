"""IAM-005: "Automated tests assert tenant isolation on every endpoint; a
new endpoint cannot ship without an isolation test."

This test enforces the second half of that sentence structurally: it
compares every registered route against the set of routes carrying a
@pytest.mark.isolation(method, path) test (built by conftest.py's
pytest_collection_modifyitems) and fails if any route has none. It needs no
database connection — it is pure introspection over FastAPI's route table
and pytest's collected markers — so it runs, and can fail CI, even in an
environment with no Postgres available.
"""

from __future__ import annotations

import pytest

from tests.support.isolation import ISOLATION_COVERAGE_KEY, registered_routes


def test_every_route_has_isolation_coverage(pytestconfig: pytest.Config) -> None:
    covered = pytestconfig.stash[ISOLATION_COVERAGE_KEY]
    required = registered_routes()
    missing = sorted(required - covered)

    assert not missing, (
        "These routes have no @pytest.mark.isolation(method, path) test "
        "(IAM-005 requires one before a new endpoint can ship):\n"
        + "\n".join(f"  {method} {path}" for method, path in missing)
    )

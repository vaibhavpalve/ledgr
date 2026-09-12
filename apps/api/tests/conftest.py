from __future__ import annotations

import pytest

from tests.support.isolation import ISOLATION_COVERAGE_KEY

# The "isolation" marker itself is registered in pyproject.toml
# ([tool.pytest.ini_options].markers), which is what makes --strict-markers
# treat @pytest.mark.isolation(...) as known rather than a typo.


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Builds the isolation-coverage set from @pytest.mark.isolation markers
    on every collected test item. This runs once, after collection finishes
    and before any test executes, so the coverage set is complete and
    available to test_isolation_coverage.py regardless of test order — and
    regardless of whether a DB-backed isolation test goes on to be skipped
    (skip is a runtime outcome; the marker is already visible at collection
    time either way, which is exactly what IAM-005's "a test exists for this
    route" guarantee needs).
    """
    covered: set[tuple[str, str]] = set()
    for item in items:
        marker = item.get_closest_marker("isolation")
        if marker is None:
            continue
        method, path = marker.args
        covered.add((str(method).upper(), str(path)))
    config.stash[ISOLATION_COVERAGE_KEY] = covered

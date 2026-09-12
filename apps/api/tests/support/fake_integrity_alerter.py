"""In-memory IntegrityAlerter double, so tests can assert NFR-033's "alerting
on any deviation" happened without reading log output.

The same shape as tests/support/fake_anomaly_alerter.py: record the calls,
assert on them. What matters here is the count as much as the content - the
alerter must be called ONCE per run that found something, and not at all for a
clean run, because an integrity job that pages on every healthy night is one
whose pages get muted.
"""

from __future__ import annotations

from api.ledger.integrity import IntegrityReport


class FakeIntegrityAlerter:
    def __init__(self) -> None:
        self.alerts: list[IntegrityReport] = []

    async def deviations_detected(self, report: IntegrityReport) -> None:
        self.alerts.append(report)

    @property
    def called(self) -> bool:
        return bool(self.alerts)

    @property
    def only(self) -> IntegrityReport:
        """The single alert, asserting there was exactly one."""
        assert len(self.alerts) == 1, f"expected exactly one alert, got {len(self.alerts)}"
        return self.alerts[0]

"""In-memory AnomalyAlerter double for testing api.auth.rate_limiting's
credential-stuffing detection without inspecting log output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class RecordedAlert:
    source_ip: str
    endpoint: str
    distinct_accounts: int
    window: timedelta


class FakeAnomalyAlerter:
    def __init__(self) -> None:
        self.alerts: list[RecordedAlert] = []

    async def alert_credential_stuffing(
        self, *, source_ip: str, endpoint: str, distinct_accounts: int, window: timedelta
    ) -> None:
        self.alerts.append(
            RecordedAlert(
                source_ip=source_ip,
                endpoint=endpoint,
                distinct_accounts=distinct_accounts,
                window=window,
            )
        )

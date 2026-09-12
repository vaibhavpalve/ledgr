"""An in-memory PeriodRepository that reimplements 0021's transition rules.

Same contract as tests/support/fake_ledger_repository.py: it refuses what
Postgres refuses, with errors naming the same requirement, so the DB-free
tests exercise the real lifecycle rather than a stub.

What it deliberately CANNOT reimplement is the `current_user <> 'ledgr_ledger'`
gate. That guard's whole purpose is to refuse a write that did not arrive
through the definer functions, and in Python there is no equivalent of
"arrived through a different privilege". It is asserted for real in
tests/integration/test_period_locking.py, and its absence here is the reason
that file exists rather than being optional.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from api.ledger.periods import (
    Period,
    PeriodError,
    PeriodStatus,
    Suppletie,
    SuppletieCorrection,
    SuppletieStatus,
)

#: Mirrors period_status_transition(). vat_filed is absent as a source: it is
#: terminal, and expressing that as "no outgoing edges" rather than as an
#: `if` makes it structural here too.
_TRANSITIONS: dict[PeriodStatus, frozenset[PeriodStatus]] = {
    PeriodStatus.OPEN: frozenset({PeriodStatus.LOCKED, PeriodStatus.VAT_FILED}),
    PeriodStatus.LOCKED: frozenset({PeriodStatus.OPEN, PeriodStatus.VAT_FILED}),
    PeriodStatus.VAT_FILED: frozenset(),
}


@dataclass
class InMemoryPeriodRepository:
    organization_id: uuid.UUID = field(default_factory=uuid.uuid4)
    periods: dict[uuid.UUID, Period] = field(default_factory=dict)
    suppleties: dict[uuid.UUID, Suppletie] = field(default_factory=dict)
    corrections: dict[uuid.UUID, list[SuppletieCorrection]] = field(default_factory=dict)

    def add_period(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        period_number: int = 1,
        status: PeriodStatus = PeriodStatus.OPEN,
        start_date: object = None,
        end_date: object = None,
    ) -> Period:
        from datetime import date

        period = Period(
            id=uuid.uuid4(),
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            period_number=period_number,
            start_date=start_date or date(2026, 1, 1),  # type: ignore[arg-type]
            end_date=end_date or date(2026, 1, 31),  # type: ignore[arg-type]
            status=status,
        )
        self.periods[period.id] = period
        return period

    async def period(self, period_id: uuid.UUID) -> Period | None:
        return self.periods.get(period_id)

    async def organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        return self.organization_id

    # -- transitions ------------------------------------------------------

    def _transition(self, period_id: uuid.UUID, target: PeriodStatus) -> Period:
        period = self.periods.get(period_id)
        if period is None:
            raise PeriodError(f"period {period_id} does not exist")
        if target not in _TRANSITIONS[period.status]:
            if period.status is PeriodStatus.VAT_FILED:
                raise PeriodError(
                    f"period {period.period_number} is VAT-filed and hard-locked; "
                    "correcting it requires a suppletie, not an unlock (FR-GL-007)"
                )
            raise PeriodError(
                f"unsupported period transition {period.status.value} -> {target.value} (FR-GL-007)"
            )
        return period

    async def lock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period:
        period = self._transition(period_id, PeriodStatus.LOCKED)
        locked = replace(
            period,
            status=PeriodStatus.LOCKED,
            locked_at=datetime.now(UTC),
            locked_by_user_id=user_id,
        )
        self.periods[period_id] = locked
        return locked

    async def unlock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period:
        period = self._transition(period_id, PeriodStatus.OPEN)
        # Clears the lock columns, exactly as the trigger does. Who unlocked it
        # is an audit_log entry, not a column.
        unlocked = replace(
            period,
            status=PeriodStatus.OPEN,
            locked_at=None,
            locked_by_user_id=None,
        )
        self.periods[period_id] = unlocked
        return unlocked

    async def mark_filed(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        filing_reference: str | None,
    ) -> Period:
        period = self._transition(period_id, PeriodStatus.VAT_FILED)
        now = datetime.now(UTC)
        filed = replace(
            period,
            status=PeriodStatus.VAT_FILED,
            filed_at=now,
            filed_by_user_id=user_id,
            filing_reference=filing_reference,
            # A filed period is also locked and stays that way; without this it
            # would read as "never locked", the opposite of the truth.
            locked_at=period.locked_at or now,
            locked_by_user_id=period.locked_by_user_id or user_id,
        )
        self.periods[period_id] = filed
        return filed

    # -- suppletie --------------------------------------------------------

    async def open_suppletie(
        self, *, period_id: uuid.UUID, reason: str, user_id: uuid.UUID
    ) -> Suppletie:
        period = self.periods.get(period_id)
        if period is None:
            raise PeriodError(f"period {period_id} does not exist")
        if period.status is not PeriodStatus.VAT_FILED:
            raise PeriodError(
                f"period {period.period_number} is {period.status.value} and has no "
                "filed return to correct (FR-GL-007)"
            )
        if any(
            s.period_id == period_id and s.status is SuppletieStatus.OPEN
            for s in self.suppleties.values()
        ):
            raise PeriodError(f"period {period.period_number} already has an open suppletie")

        suppletie = Suppletie(
            id=uuid.uuid4(),
            administration_id=period.administration_id,
            period_id=period_id,
            status=SuppletieStatus.OPEN,
            reason=reason,
            opened_by_user_id=user_id,
            opened_at=datetime.now(UTC),
        )
        self.suppleties[suppletie.id] = suppletie
        return suppletie

    async def close_suppletie(
        self,
        *,
        suppletie_id: uuid.UUID,
        status: SuppletieStatus,
        filing_reference: str | None,
    ) -> Suppletie:
        suppletie = self.suppleties.get(suppletie_id)
        if suppletie is None or suppletie.status is not SuppletieStatus.OPEN:
            raise PeriodError(f"suppletie {suppletie_id} does not exist or is already closed")
        now = datetime.now(UTC)
        closed = replace(
            suppletie,
            status=status,
            submitted_at=now if status is SuppletieStatus.SUBMITTED else None,
            filed_at=now if status is SuppletieStatus.FILED else None,
            filing_reference=filing_reference or suppletie.filing_reference,
        )
        self.suppleties[suppletie_id] = closed
        return closed

    async def suppletie(self, suppletie_id: uuid.UUID) -> Suppletie | None:
        return self.suppleties.get(suppletie_id)

    async def open_suppletie_for(self, period_id: uuid.UUID) -> Suppletie | None:
        for suppletie in self.suppleties.values():
            if suppletie.period_id == period_id and suppletie.status is SuppletieStatus.OPEN:
                return suppletie
        return None

    async def suppletie_corrections(self, suppletie_id: uuid.UUID) -> Sequence[SuppletieCorrection]:
        return self.corrections.get(suppletie_id, [])

"""In-memory SodRepository double for testing api.authz.sod without a
database.

Mirrors the invariants migrations/0012_segregation_of_duties.sql enforces:
one active deviation per (organization, rule), a non-blank reason, and
append-only events. Same discipline as the other fakes in this directory -
a fake that accepts what the database would reject lets a test pass on state
production can never hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from api.authz.sod import Deviation, SodEvent, SodOutcome, SodRule


class InMemorySodRepository:
    def __init__(self) -> None:
        self._active_users: dict[uuid.UUID, int] = {}
        self._owners: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self._deviations: list[Deviation] = []
        self._events: list[SodEvent] = []

    # --- test setup -------------------------------------------------------

    def set_active_user_count(self, organization_id: uuid.UUID, count: int) -> None:
        self._active_users[organization_id] = count

    def make_owner(self, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self._owners.add((user_id, organization_id))

    @property
    def events(self) -> list[SodEvent]:
        return list(self._events)

    # --- SodRepository ----------------------------------------------------

    async def active_user_count(self, organization_id: uuid.UUID) -> int:
        # Defaults high rather than low: an unset count in a test must not
        # silently exempt the organization and make a rule look enforced
        # when it was skipped.
        return self._active_users.get(organization_id, 10)

    async def holds_owner_role(self, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        return (user_id, organization_id) in self._owners

    async def active_deviation(
        self, *, organization_id: uuid.UUID, rule: SodRule
    ) -> Deviation | None:
        for deviation in self._deviations:
            if (
                deviation.organization_id == organization_id
                and deviation.rule is rule
                and deviation.is_active
            ):
                return deviation
        return None

    async def list_deviations(self, organization_id: uuid.UUID) -> Sequence[Deviation]:
        return [d for d in self._deviations if d.organization_id == organization_id]

    async def create_deviation(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        acknowledged_by_user_id: uuid.UUID,
        reason: str,
    ) -> uuid.UUID:
        # sod_policy_deviation's CHECK on reason.
        if not reason.strip():
            raise ValueError("a SoD deviation requires a non-blank reason")
        # sod_deviation_one_active_idx.
        if await self.active_deviation(organization_id=organization_id, rule=rule) is not None:
            raise ValueError(
                f"organization {organization_id} already has an active deviation for {rule.value}"
            )

        deviation = Deviation(
            id=uuid.uuid4(),
            organization_id=organization_id,
            rule=rule,
            acknowledged_by_user_id=acknowledged_by_user_id,
            reason=reason,
            acknowledged_at=datetime.now(UTC),
        )
        self._deviations.append(deviation)
        return deviation.id

    async def revoke_deviation(
        self, *, deviation_id: uuid.UUID, revoked_by_user_id: uuid.UUID, at: datetime
    ) -> None:
        for index, deviation in enumerate(self._deviations):
            if deviation.id != deviation_id:
                continue
            if deviation.revoked_at is not None:
                # sod_deviation_immutable_trg's un-revoke guard.
                raise ValueError("a revoked SoD deviation cannot be re-revoked")
            self._deviations[index] = Deviation(
                id=deviation.id,
                organization_id=deviation.organization_id,
                rule=deviation.rule,
                acknowledged_by_user_id=deviation.acknowledged_by_user_id,
                reason=deviation.reason,
                acknowledged_at=deviation.acknowledged_at,
                revoked_at=at,
                revoked_by_user_id=revoked_by_user_id,
            )
            return

    async def record_event(
        self,
        *,
        organization_id: uuid.UUID,
        rule: SodRule,
        outcome: SodOutcome,
        actor_user_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID | None,
        detail: str,
        deviation_id: uuid.UUID | None,
        at: datetime,
    ) -> uuid.UUID:
        # sod_event_deviation_attributed.
        if outcome == "deviation_applied" and deviation_id is None:
            raise ValueError("a deviation_applied event must name the deviation that permitted it")

        event = SodEvent(
            id=uuid.uuid4(),
            organization_id=organization_id,
            rule=rule,
            outcome=outcome,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            deviation_id=deviation_id,
            occurred_at=at,
        )
        self._events.append(event)
        return event.id

    async def list_events(self, organization_id: uuid.UUID) -> Sequence[SodEvent]:
        return [e for e in self._events if e.organization_id == organization_id]

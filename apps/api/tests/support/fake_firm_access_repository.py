"""In-memory doubles for IAM-109's register and IAM-110's revocation, backed
by the same InMemoryAuthorizationRepository the rest of the authz tests use.

Not a standalone store, for the same reason the firm staff fake is not: the
register reads role_assignment rows and revocation writes them, so a fake
with its own copy would let a test pass on state where the two disagree -
exactly the bug the register exists to make visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from api.authz.engagement_revocation import SwitchableAdministration
from api.authz.firm_access_register import FirmAccessEntry
from tests.support.fake_authz_repository import InMemoryAuthorizationRepository


@dataclass
class _Access:
    first_accessed_at: datetime
    last_accessed_at: datetime
    access_count: int


class InMemoryFirmAccessRepository:
    def __init__(self, authz: InMemoryAuthorizationRepository) -> None:
        self._authz = authz
        self._access: dict[tuple[uuid.UUID, uuid.UUID], _Access] = {}
        self._sessions: list[dict[str, object]] = []
        self._users: dict[uuid.UUID, str] = {}
        self._organizations: dict[uuid.UUID, str] = {}
        self._administration_names: dict[uuid.UUID, str] = {}

    # --- test setup -------------------------------------------------------

    def add_user(self, user_id: uuid.UUID, email: str) -> None:
        self._users[user_id] = email

    def name_organization(self, organization_id: uuid.UUID, name: str) -> None:
        self._organizations[organization_id] = name

    def name_administration(self, administration_id: uuid.UUID, name: str) -> None:
        self._administration_names[administration_id] = name

    def open_session(
        self, *, user_id: uuid.UUID, active_administration_id: uuid.UUID | None
    ) -> uuid.UUID:
        session_id = uuid.uuid4()
        self._sessions.append(
            {
                "id": session_id,
                "user_id": user_id,
                "active_administration_id": active_administration_id,
                "revoked_at": None,
            }
        )
        return session_id

    def session_context(self, session_id: uuid.UUID) -> uuid.UUID | None:
        for session in self._sessions:
            if session["id"] == session_id:
                return session["active_administration_id"]  # type: ignore[return-value]
        raise KeyError(session_id)

    def session_is_revoked(self, session_id: uuid.UUID) -> bool:
        for session in self._sessions:
            if session["id"] == session_id:
                return session["revoked_at"] is not None
        raise KeyError(session_id)

    # --- FirmAccessRegisterRepository -------------------------------------

    async def list_firm_access(
        self, *, administration_id: uuid.UUID, now: datetime
    ) -> Sequence[FirmAccessEntry]:
        owner = self._authz.administration_owner(administration_id)
        entries: list[FirmAccessEntry] = []

        for assignment in self._authz.assignments_on(administration_id, now=now):
            granting_org = assignment.granted_by_organization_id
            # Firm grants only - the client's own users hold identically
            # shaped rows on the same administration.
            if granting_org is None or granting_org == owner:
                continue
            access = self._access.get((assignment.user_id, administration_id))
            entries.append(
                FirmAccessEntry(
                    user_id=assignment.user_id,
                    email=self._users.get(assignment.user_id, "unknown@example.com"),
                    firm_organization_id=granting_org,
                    firm_name=self._organizations.get(granting_org, "Unknown Firm"),
                    role_name=self._authz.role_name(assignment.role_id),
                    granted_at=assignment.created_at or now,
                    granted_by_user_id=assignment.granted_by_user_id or assignment.user_id,
                    expires_at=assignment.expires_at,
                    last_accessed_at=access.last_accessed_at if access else None,
                    first_accessed_at=access.first_accessed_at if access else None,
                    access_count=access.access_count if access else 0,
                )
            )

        # NULLS LAST, most recent first.
        entries.sort(
            key=lambda e: (e.last_accessed_at is not None, e.last_accessed_at or e.granted_at),
            reverse=True,
        )
        return entries

    async def record_access(
        self,
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
        window: timedelta,
    ) -> bool:
        key = (user_id, administration_id)
        existing = self._access.get(key)
        if existing is None:
            self._access[key] = _Access(first_accessed_at=at, last_accessed_at=at, access_count=1)
            return True
        if existing.last_accessed_at >= at - window:
            return False
        # administration_access_monotonic_trg: never moves backwards.
        existing.last_accessed_at = max(existing.last_accessed_at, at)
        existing.access_count += 1
        return True

    # --- EngagementRevocationRepository -----------------------------------

    async def active_engagement_exists(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool:
        return self._authz.has_engagement(
            firm_organization_id=firm_organization_id, administration_id=administration_id
        )

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self._authz.administration_owner(administration_id)

    async def revoke_engagement(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        revoked_by_user_id: uuid.UUID,
        at: datetime,
    ) -> None:
        self._authz.end_engagement(
            firm_organization_id=firm_organization_id, administration_id=administration_id
        )

    async def revoke_firm_grants_on(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> Sequence[uuid.UUID]:
        return self._authz.revoke_all_firm_grants_on(
            firm_organization_id=firm_organization_id,
            administration_id=administration_id,
            at=at,
        )

    async def clear_administration_context(
        self, *, administration_id: uuid.UUID, user_ids: Sequence[uuid.UUID], at: datetime
    ) -> int:
        cleared = 0
        for session in self._sessions:
            if (
                session["active_administration_id"] == administration_id
                and session["user_id"] in set(user_ids)
                and session["revoked_at"] is None
            ):
                session["active_administration_id"] = None
                cleared += 1
        return cleared

    async def set_active_administration(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        administration_id: uuid.UUID | None,
    ) -> bool:
        for session in self._sessions:
            # user_id as well as session_id, mirroring the SQL: a session
            # identifier in the wrong hands must not move somebody else's
            # session, and a revoked session cannot be repositioned.
            if (
                session["id"] == session_id
                and session["user_id"] == user_id
                and session["revoked_at"] is None
            ):
                session["active_administration_id"] = administration_id
                return True
        return False

    async def switchable_administrations(
        self, *, user_id: uuid.UUID, now: datetime
    ) -> Sequence[SwitchableAdministration]:
        return [
            SwitchableAdministration(
                administration_id=assignment.scope_id,
                legal_name=self._administration_names.get(
                    assignment.scope_id, "Unnamed Administration"
                ),
                role_name=self._authz.role_name(assignment.role_id),
                expires_at=assignment.expires_at,
            )
            for assignment in self._authz.live_administration_assignments(user_id=user_id, now=now)
        ]

    def access_record(self, *, user_id: uuid.UUID, administration_id: uuid.UUID) -> _Access | None:
        return self._access.get((user_id, administration_id))

    def force_access_timestamp(
        self, *, user_id: uuid.UUID, administration_id: uuid.UUID, at: datetime
    ) -> None:
        """A raw UPDATE, bypassing record_access's throttle - the only path
        on which administration_access_monotonic_trg can actually fire.

        The throttle already absorbs a backwards timestamp on the ordinary
        upsert path (an `at` older than the stored value never clears the
        cutoff), so the trigger is defence for writes that do not go through
        it: a backfill, an ops script, a future service writing the row
        directly. Mirrored here so that guard is exercised rather than
        assumed.
        """
        key = (user_id, administration_id)
        existing = self._access[key]
        # administration_access_monotonic_trg
        existing.last_accessed_at = max(existing.last_accessed_at, at)

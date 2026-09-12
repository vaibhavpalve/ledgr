"""In-memory FirmStaffRepository double, backed by the same
InMemoryAuthorizationRepository the rest of the authz tests use.

Deliberately not a standalone store: a firm staff grant IS a role_assignment
(IAM-107 - "granted per client administration" means the same row shape as
any other grant), so a fake with its own separate list would let a test pass
on state where the two disagree. Everything here reads and writes the
assignment list authorize() evaluates.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from api.authz.firm_staff import FirmStaffGrant
from api.authz.model import ScopeType
from tests.support.fake_authz_repository import InMemoryAuthorizationRepository

MANAGE_USER_ROLE = ("manage", "user_role")


class InMemoryFirmStaffRepository:
    def __init__(
        self, authz: InMemoryAuthorizationRepository, *, firm_organization_id: uuid.UUID
    ) -> None:
        self._authz = authz
        # The tenant context grants are made under. In production
        # role_assignment_firm_staff_guard_trg reads this from
        # app.current_org_id(); here it is the firm the service was built for.
        self._firm_organization_id = firm_organization_id

    async def get_system_role(self, name: str) -> tuple[uuid.UUID, ScopeType] | None:
        role_id = self._authz.role_id_or_none(name)
        if role_id is None:
            return None
        return role_id, self._authz.role_scope_type(role_id)

    async def holds_manage_user_role(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        held = await self._authz.unconditionally_held_permissions(
            user_id=user_id, organization_id=organization_id, now=datetime.now(UTC)
        )
        return MANAGE_USER_ROLE in held

    async def has_active_engagement(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool:
        return self._authz.has_engagement(
            firm_organization_id=firm_organization_id, administration_id=administration_id
        )

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self._authz.administration_owner(administration_id)

    async def role_permissions(self, role_id: uuid.UUID) -> set[tuple[str, str]]:
        return await self._authz.role_permissions((role_id,))

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]:
        return await self._authz.unconditionally_held_permissions(
            user_id=user_id, organization_id=organization_id, now=now
        )

    async def create_firm_staff_assignment(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        administration_id: uuid.UUID,
        granted_by_user_id: uuid.UUID,
        expires_at: datetime | None,
        at: datetime,
    ) -> uuid.UUID:
        return self._authz.assign(
            user_id=user_id,
            role=self._authz.role_name(role_id),
            scope_id=administration_id,
            expires_at=expires_at,
            granted_by_organization_id=self._firm_organization_id,
            granted_by_user_id=granted_by_user_id,
        )

    async def revoke_firm_staff_assignments(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> int:
        return self._authz.revoke_firm_staff(
            firm_organization_id=firm_organization_id,
            staff_user_id=staff_user_id,
            administration_id=administration_id,
            at=at,
        )

    async def list_firm_staff_grants(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID | None,
        now: datetime,
    ) -> Sequence[FirmStaffGrant]:
        return self._authz.firm_staff_grants(
            firm_organization_id=firm_organization_id,
            staff_user_id=staff_user_id,
            now=now,
        )

"""In-memory AuthorizationRepository double for testing api.authz.service
without a database.

This fake deliberately re-implements the INVARIANTS that
migrations/0009_authorization.sql enforces with triggers, not just the happy
path - a role's scope_type must match its assignments', an
organization-scope permission cannot be bundled into an
administration-scope role, and a condition key must be one of the closed
set. The same discipline as tests/support/fake_key_repository.py, and for
the same reason: the encryption-key work found a real ordering bug that only
a partial unique index would have caught, and a fake that had modelled the
index would have caught it without Postgres. A fake that accepts what the
database would reject lets a test pass on data production can never hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.authz.conditions import CONDITION_KEYS
from api.authz.firm_staff import FirmStaffGrant
from api.authz.model import AssignmentRecord, Grant, RoleRecord, ScopeType
from api.authz.rights_floor import OWNER_ROLE_NAME


@dataclass
class _Role:
    id: uuid.UUID
    name: str
    scope_type: ScopeType
    is_system: bool
    organization_id: uuid.UUID | None = None
    archived_at: datetime | None = None
    permissions: set[tuple[str, str]] = field(default_factory=set)
    # Provenance, mirroring role_component. Never read when authorizing -
    # `permissions` above is already the flattened closure.
    component_role_ids: tuple[uuid.UUID, ...] = ()


@dataclass
class _Assignment:
    id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    scope_type: ScopeType
    scope_id: uuid.UUID
    conditions: Mapping[str, Any]
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    granted_by_user_id: uuid.UUID | None = None
    granted_by_organization_id: uuid.UUID | None = None
    created_at: datetime | None = None


class InMemoryAuthorizationRepository:
    def __init__(self) -> None:
        # (action, resource_type) -> resource_scope
        self._permissions: dict[tuple[str, str], ScopeType] = {}
        self._roles: dict[uuid.UUID, _Role] = {}
        self._roles_by_name: dict[str, uuid.UUID] = {}
        self._assignments: list[_Assignment] = []
        # administration_id -> owning organization_id. Ownership only, never
        # engagement - matching administration.organization_id exactly.
        self._administration_owners: dict[uuid.UUID, uuid.UUID] = {}
        # administration_id -> firm organizations with an active engagement
        self._engagements: dict[uuid.UUID, set[uuid.UUID]] = {}

    # --- test setup -------------------------------------------------------

    def add_administration(self, administration_id: uuid.UUID, *, owned_by: uuid.UUID) -> None:
        self._administration_owners[administration_id] = owned_by

    def define_permission(self, action: str, resource_type: str, resource_scope: ScopeType) -> None:
        self._permissions[(action, resource_type)] = resource_scope

    def define_role(
        self,
        name: str,
        *,
        scope_type: ScopeType,
        permissions: Iterable[tuple[str, str]] = (),
        organization_id: uuid.UUID | None = None,
        is_system: bool = True,
        component_role_ids: Sequence[uuid.UUID] = (),
    ) -> uuid.UUID:
        role = _Role(
            id=uuid.uuid4(),
            name=name,
            scope_type=scope_type,
            is_system=is_system,
            organization_id=organization_id,
            component_role_ids=tuple(component_role_ids),
        )
        for permission in permissions:
            self._bundle(role, permission)

        # role_component_guard_trg's scope rule. The service refuses this
        # earlier with a clearer message; the fake enforces it too so a test
        # cannot construct state the database would reject.
        for component_id in component_role_ids:
            component = self._roles[component_id]
            if role.scope_type == "administration" and component.scope_type == "organization":
                raise ValueError(
                    f"administration-scope role {name!r} cannot include "
                    f"organization-scope role {component.name!r}"
                )

        self._roles[role.id] = role
        self._roles_by_name[name] = role.id
        return role.id

    def archive_role(self, name: str) -> None:
        self._roles[self._roles_by_name[name]].archived_at = datetime.now()

    def role_id(self, name: str) -> uuid.UUID:
        return self._roles_by_name[name]

    def role_id_or_none(self, name: str) -> uuid.UUID | None:
        return self._roles_by_name.get(name)

    def role_name(self, role_id: uuid.UUID) -> str:
        return self._roles[role_id].name

    def role_scope_type(self, role_id: uuid.UUID) -> ScopeType:
        return self._roles[role_id].scope_type

    def revoke_firm_staff(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> int:
        """Revokes only grants THIS firm made. A firm taking a staff member
        off a client must not also revoke the client's own users, who hold
        identically-scoped rows on the same administration.
        """
        revoked = 0
        for assignment in self._assignments:
            if (
                assignment.user_id == staff_user_id
                and assignment.scope_type == "administration"
                and assignment.scope_id == administration_id
                and assignment.granted_by_organization_id == firm_organization_id
                and assignment.revoked_at is None
            ):
                assignment.revoked_at = at
                revoked += 1
        return revoked

    def administration_owner(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self._administration_owners.get(administration_id)

    def assignments_on(self, administration_id: uuid.UUID, *, now: datetime) -> list[_Assignment]:
        return [
            a
            for a in self._assignments
            if a.scope_type == "administration"
            and a.scope_id == administration_id
            and self._is_live(a, now=now)
        ]

    def live_administration_assignments(
        self, *, user_id: uuid.UUID, now: datetime
    ) -> list[_Assignment]:
        return [
            a
            for a in self._assignments
            if a.user_id == user_id
            and a.scope_type == "administration"
            and self._is_live(a, now=now)
            and self._roles[a.role_id].archived_at is None
        ]

    def end_engagement(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> None:
        self._engagements.get(administration_id, set()).discard(firm_organization_id)

    def revoke_all_firm_grants_on(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID, at: datetime
    ) -> list[uuid.UUID]:
        """Keyed on provenance: the client's own users hold identically
        shaped rows on this administration and must survive.
        """
        affected: list[uuid.UUID] = []
        for assignment in self._assignments:
            if (
                assignment.scope_type == "administration"
                and assignment.scope_id == administration_id
                and assignment.granted_by_organization_id == firm_organization_id
                and assignment.revoked_at is None
            ):
                assignment.revoked_at = at
                affected.append(assignment.user_id)
        return affected

    def firm_staff_grants(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID | None,
        now: datetime,
    ) -> list[FirmStaffGrant]:
        return [
            FirmStaffGrant(
                assignment_id=a.id,
                user_id=a.user_id,
                administration_id=a.scope_id,
                role_name=self._roles[a.role_id].name,
                granted_at=a.created_at or now,
                granted_by_user_id=a.granted_by_user_id or a.user_id,
                expires_at=a.expires_at,
            )
            for a in self._assignments
            if a.scope_type == "administration"
            and a.granted_by_organization_id == firm_organization_id
            and self._is_live(a, now=now)
            and (staff_user_id is None or a.user_id == staff_user_id)
        ]

    def force_add_permission(self, role_name: str, permission: tuple[str, str]) -> None:
        """Bypasses every guard to mutate a role's bundle in place.

        Only for proving that a composed role is INSULATED from its
        components changing - which role_immutable_trg makes impossible in
        production, so the only way to test the insulation is to force the
        thing that cannot happen. Never use this to set up ordinary state.
        """
        self._roles[self._roles_by_name[role_name]].permissions.add(permission)

    def assign(
        self,
        *,
        user_id: uuid.UUID,
        role: str,
        scope_id: uuid.UUID,
        conditions: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
        granted_by_organization_id: uuid.UUID | None = None,
        granted_by_user_id: uuid.UUID | None = None,
        created_at: datetime | None = None,
    ) -> uuid.UUID:
        role_id = self._roles_by_name[role]
        record = self._roles[role_id]

        for key in conditions or {}:
            # role_assignment_conditions_guard_trg
            if key not in CONDITION_KEYS:
                raise ValueError(f"unknown attribute condition {key!r}")

        self._assert_owner_floor_intact(
            record, scope_id=scope_id, conditions=conditions, expires_at=expires_at
        )

        # role_assignment_firm_staff_guard_trg derives the granting
        # organization from tenant context; a test that does not say
        # otherwise is the owning organization granting within its own
        # administration, or the organization itself for an org-scoped grant.
        granter_org = granted_by_organization_id
        if granter_org is None:
            granter_org = (
                scope_id
                if record.scope_type == "organization"
                else self._administration_owners.get(scope_id)
            )
        self._assert_engagement_for_firm_grant(
            scope_type=record.scope_type, scope_id=scope_id, granted_by_organization_id=granter_org
        )

        assignment = _Assignment(
            id=uuid.uuid4(),
            user_id=user_id,
            role_id=role_id,
            # role_assignment_scope_guard_trg: an assignment's scope_type is
            # never independently chosen, it always equals the role's.
            scope_type=record.scope_type,
            scope_id=scope_id,
            conditions=dict(conditions or {}),
            expires_at=expires_at,
            revoked_at=revoked_at,
            granted_by_user_id=granted_by_user_id,
            granted_by_organization_id=granter_org,
            # Defaults to wall clock, matching role_assignment.created_at's
            # `default now()`. A test with an injected clock passes its own
            # so "since when" is deterministic.
            created_at=created_at or datetime.now(UTC),
        )
        self._assignments.append(assignment)
        return assignment.id

    def _assert_engagement_for_firm_grant(
        self,
        *,
        scope_type: ScopeType,
        scope_id: uuid.UUID,
        granted_by_organization_id: uuid.UUID | None,
    ) -> None:
        """role_assignment_firm_staff_guard_trg (IAM-107): an
        administration-scoped grant made from an organization that does not
        own the administration requires an active engagement.
        """
        if scope_type != "administration" or granted_by_organization_id is None:
            return
        owner = self._administration_owners.get(scope_id)
        if owner is None or owner == granted_by_organization_id:
            return
        if granted_by_organization_id not in self._engagements.get(scope_id, set()):
            raise ValueError(
                f"organization {granted_by_organization_id} has no active engagement on "
                f"administration {scope_id} (IAM-107)"
            )

    def _assert_owner_floor_intact(
        self,
        role: _Role,
        *,
        scope_id: uuid.UUID,
        conditions: Mapping[str, Any] | None,
        expires_at: datetime | None,
    ) -> None:
        """role_assignment_owner_floor_guard_trg's INSERT half (IAM-105).
        An Owner assignment that expires, or that carries a condition, could
        remove the client rights floor without anyone touching a profile.
        """
        carries_floor = (
            role.is_system and role.name == OWNER_ROLE_NAME and role.scope_type == "organization"
        )
        if not carries_floor:
            return
        if expires_at is not None:
            raise ValueError("an Owner assignment cannot carry an expiry (IAM-105)")
        if conditions:
            raise ValueError("an Owner assignment cannot carry attribute conditions (IAM-105)")

    def _bundle(self, role: _Role, permission: tuple[str, str]) -> None:
        resource_scope = self._permissions.get(permission)
        if resource_scope is None:
            raise ValueError(
                f"permission {permission} is not defined; call define_permission first"
            )
        # role_permission_scope_guard_trg
        if role.scope_type == "administration" and resource_scope == "organization":
            raise ValueError(
                f"cannot bundle organization-scope permission {permission} "
                f"into administration-scope role {role.name!r}"
            )
        role.permissions.add(permission)

    # --- AuthorizationRepository -----------------------------------------

    async def live_grants(
        self, *, user_id: uuid.UUID, action: str, resource_type: str, now: datetime
    ) -> Sequence[Grant]:
        wanted = (action, resource_type)
        resource_scope = self._permissions.get(wanted)
        if resource_scope is None:
            return []

        grants: list[Grant] = []
        for assignment in self._assignments:
            if assignment.user_id != user_id:
                continue
            if assignment.revoked_at is not None:
                continue
            if assignment.expires_at is not None and assignment.expires_at <= now:
                continue

            role = self._roles[assignment.role_id]
            if role.archived_at is not None:
                continue
            if wanted not in role.permissions:
                continue

            grants.append(
                Grant(
                    assignment_id=assignment.id,
                    role_id=role.id,
                    role_name=role.name,
                    scope_type=assignment.scope_type,
                    scope_id=assignment.scope_id,
                    resource_scope=resource_scope,
                    conditions=assignment.conditions,
                    granted_by_organization_id=assignment.granted_by_organization_id,
                )
            )
        return grants

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self._administration_owners.get(administration_id)

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]:
        owned = {
            admin_id
            for admin_id, owner in self._administration_owners.items()
            if owner == organization_id
        }

        held: set[tuple[str, str]] = set()
        for assignment in self._assignments:
            if assignment.user_id != user_id:
                continue
            if assignment.revoked_at is not None:
                continue
            if assignment.expires_at is not None and assignment.expires_at <= now:
                continue
            # The `ra.conditions = '{}'::jsonb` half of _PERMISSIONS_HELD_SQL:
            # a conditioned grant does not confer the permission outright.
            if assignment.conditions:
                continue

            reaches = (
                assignment.scope_id == organization_id
                if assignment.scope_type == "organization"
                else assignment.scope_id in owned
            )
            if not reaches:
                continue

            role = self._roles[assignment.role_id]
            if role.archived_at is not None:
                continue
            held |= role.permissions
        return held

    def _is_live(self, assignment: _Assignment, *, now: datetime | None = None) -> bool:
        if assignment.revoked_at is not None:
            return False
        if assignment.expires_at is None:
            return True
        return assignment.expires_at > (now or datetime.now(UTC))

    def _owner_assignments(self, organization_id: uuid.UUID) -> list[_Assignment]:
        return [
            a
            for a in self._assignments
            if a.scope_type == "organization"
            and a.scope_id == organization_id
            and self._roles[a.role_id].name == OWNER_ROLE_NAME
            and self._roles[a.role_id].is_system
            and self._is_live(a)
        ]

    async def is_organization_owner(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        return any(a.user_id == user_id for a in self._owner_assignments(organization_id))

    async def count_active_owners(self, organization_id: uuid.UUID) -> int:
        return len({a.user_id for a in self._owner_assignments(organization_id)})

    async def get_assignment(self, assignment_id: uuid.UUID) -> AssignmentRecord | None:
        for assignment in self._assignments:
            if assignment.id == assignment_id:
                return AssignmentRecord(
                    id=assignment.id,
                    user_id=assignment.user_id,
                    role_id=assignment.role_id,
                    role_name=self._roles[assignment.role_id].name,
                    scope_type=assignment.scope_type,
                    scope_id=assignment.scope_id,
                    revoked_at=assignment.revoked_at,
                )
        return None

    async def revoke_assignment(self, *, assignment_id: uuid.UUID, at: datetime) -> None:
        for assignment in self._assignments:
            if assignment.id == assignment_id and assignment.revoked_at is None:
                # role_assignment_owner_floor_guard_trg: the last Owner of an
                # organization cannot be revoked, whatever the caller does.
                role = self._roles[assignment.role_id]
                if (
                    role.is_system
                    and role.name == OWNER_ROLE_NAME
                    and assignment.scope_type == "organization"
                    and len({a.user_id for a in self._owner_assignments(assignment.scope_id)}) <= 1
                ):
                    raise ValueError(
                        f"cannot revoke the last Owner of organization {assignment.scope_id} "
                        "(IAM-105)"
                    )
                assignment.revoked_at = at
                return

    def engage_firm(self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        """An active firm_engagement - the client's consent that IAM-107
        requires before a firm may grant its staff access here.
        """
        self._engagements.setdefault(administration_id, set()).add(firm_organization_id)

    def has_engagement(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool:
        return firm_organization_id in self._engagements.get(administration_id, set())

    async def get_roles(self, role_ids: Sequence[uuid.UUID]) -> Sequence[RoleRecord]:
        return [
            RoleRecord(
                id=role.id,
                name=role.name,
                scope_type=role.scope_type,
                organization_id=role.organization_id,
                is_system=role.is_system,
                archived_at=role.archived_at,
            )
            for role_id in role_ids
            if (role := self._roles.get(role_id)) is not None
        ]

    async def role_permissions(self, role_ids: Sequence[uuid.UUID]) -> set[tuple[str, str]]:
        held: set[tuple[str, str]] = set()
        for role_id in role_ids:
            role = self._roles.get(role_id)
            if role is not None:
                # Flat, like the SQL: `permissions` is already the closure.
                held |= role.permissions
        return held

    async def create_custom_role(
        self,
        *,
        organization_id: uuid.UUID,
        name: str,
        scope_type: ScopeType,
        permissions: Iterable[tuple[str, str]],
        component_role_ids: Sequence[uuid.UUID],
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        return self.define_role(
            name,
            scope_type=scope_type,
            permissions=permissions,
            organization_id=organization_id,
            is_system=False,
            component_role_ids=component_role_ids,
        )

    async def create_assignment(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID,
        conditions: Mapping[str, Any],
        granted_by_user_id: uuid.UUID,
        expires_at: datetime | None,
    ) -> uuid.UUID:
        role = self._roles[role_id]
        # role_assignment_scope_guard_trg: an assignment's scope_type always
        # equals its role's.
        if role.scope_type != scope_type:
            raise ValueError(
                f"role {role.name!r} is {role.scope_type}-scoped; "
                f"cannot assign it at {scope_type} scope"
            )
        for key in conditions:
            if key not in CONDITION_KEYS:
                raise ValueError(f"unknown attribute condition {key!r}")

        self._assert_owner_floor_intact(
            role, scope_id=scope_id, conditions=conditions, expires_at=expires_at
        )

        assignment = _Assignment(
            id=uuid.uuid4(),
            user_id=user_id,
            role_id=role_id,
            scope_type=scope_type,
            scope_id=scope_id,
            conditions=dict(conditions),
            expires_at=expires_at,
        )
        self._assignments.append(assignment)
        return assignment.id

"""In-memory ProfileRepository double for testing api.authz.profiles
without a database.

Mirrors 0013_client_access_profiles.sql's invariants: versions are
append-only and immutable, at most one assignment per administration is in
force, and a profile cannot contain an organization-scope permission.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.authz.matrix import permission_catalogue
from api.authz.profiles import (
    BUILTIN_PROFILES,
    ClientAccessProfile,
    Permission,
    ProfileRestrictions,
    ProfileVersion,
)

_PERMISSION_SCOPES: dict[Permission, str] = {
    permission.key: scope for permission, scope in permission_catalogue()
}


@dataclass
class _Assignment:
    id: uuid.UUID
    administration_id: uuid.UUID
    profile_id: uuid.UUID
    assigned_by_user_id: uuid.UUID
    assigned_at: datetime
    superseded_at: datetime | None = None


class InMemoryProfileRepository:
    def __init__(self, *, seed_builtins: bool = True) -> None:
        self._profiles: dict[uuid.UUID, ClientAccessProfile] = {}
        self._by_name: dict[str, uuid.UUID] = {}
        self._versions: dict[uuid.UUID, list[ProfileVersion]] = {}
        self._assignments: list[_Assignment] = []
        # firm_organization_id -> administration_id -> permissions
        self._firm_holdings: dict[uuid.UUID, dict[uuid.UUID, set[Permission]]] = {}
        self._administration_owner: dict[uuid.UUID, uuid.UUID] = {}

        if seed_builtins:
            self.seed_builtin_profiles()

    # --- test setup -------------------------------------------------------

    def seed_builtin_profiles(self) -> None:
        """The same three profiles migration 0013's companion seeding
        creates, built from the single BUILTIN_PROFILES definition so the
        fake and production cannot describe different profiles.
        """
        for name, description, permissions, restrictions in BUILTIN_PROFILES:
            profile_id = uuid.uuid4()
            self._profiles[profile_id] = ClientAccessProfile(
                id=profile_id,
                name=name,
                description=description,
                is_builtin=True,
                firm_organization_id=None,
            )
            self._by_name[name] = profile_id
            self._versions[profile_id] = [
                ProfileVersion(
                    id=uuid.uuid4(),
                    profile_id=profile_id,
                    version=1,
                    summary=description,
                    permissions=permissions,
                    restrictions=restrictions,
                )
            ]

    def profile_id(self, name: str) -> uuid.UUID:
        return self._by_name[name]

    def set_firm_holdings(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        permissions: set[Permission],
    ) -> None:
        self._firm_holdings.setdefault(firm_organization_id, {})[administration_id] = permissions

    def archive_profile(self, profile_id: uuid.UUID, at: datetime) -> None:
        profile = self._profiles[profile_id]
        self._profiles[profile_id] = ClientAccessProfile(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            is_builtin=profile.is_builtin,
            firm_organization_id=profile.firm_organization_id,
            archived_at=at,
        )

    # --- ProfileRepository ------------------------------------------------

    async def get_profile(self, profile_id: uuid.UUID) -> ClientAccessProfile | None:
        return self._profiles.get(profile_id)

    async def get_profile_by_name(self, name: str) -> ClientAccessProfile | None:
        profile_id = self._by_name.get(name)
        return self._profiles.get(profile_id) if profile_id is not None else None

    async def current_version(self, profile_id: uuid.UUID) -> ProfileVersion | None:
        versions = self._versions.get(profile_id, [])
        return max(versions, key=lambda v: v.version) if versions else None

    async def current_profile_for(
        self, administration_id: uuid.UUID
    ) -> tuple[ClientAccessProfile, ProfileVersion] | None:
        for assignment in self._assignments:
            if (
                assignment.administration_id == administration_id
                and assignment.superseded_at is None
            ):
                profile = self._profiles[assignment.profile_id]
                version = await self.current_version(profile.id)
                if version is None:
                    return None
                return profile, version
        return None

    def own_administration(self, administration_id: uuid.UUID, *, owner: uuid.UUID) -> None:
        self._administration_owner[administration_id] = owner

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self._administration_owner.get(administration_id)

    async def administrations_using(self, profile_id: uuid.UUID) -> Sequence[uuid.UUID]:
        return [
            a.administration_id
            for a in self._assignments
            if a.profile_id == profile_id and a.superseded_at is None
        ]

    async def firm_permissions_on(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> set[Permission]:
        return set(self._firm_holdings.get(firm_organization_id, {}).get(administration_id, set()))

    async def create_profile(
        self,
        *,
        firm_organization_id: uuid.UUID,
        name: str,
        description: str,
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        # client_access_profile_firm_name_idx
        existing = self._by_name.get(name)
        if existing is not None and self._profiles[existing].firm_organization_id == (
            firm_organization_id
        ):
            raise ValueError(f"firm {firm_organization_id} already has a profile named {name!r}")

        profile_id = uuid.uuid4()
        self._profiles[profile_id] = ClientAccessProfile(
            id=profile_id,
            name=name,
            description=description,
            is_builtin=False,
            firm_organization_id=firm_organization_id,
        )
        self._by_name[name] = profile_id
        self._versions[profile_id] = []
        return profile_id

    async def publish_version(
        self,
        *,
        profile_id: uuid.UUID,
        version: int,
        summary: str,
        permissions: frozenset[Permission],
        restrictions: Mapping[str, Any],
        published_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        # client_access_profile_permission_guard_trg
        for permission in permissions:
            if _PERMISSION_SCOPES.get(permission) == "organization":
                raise ValueError(
                    f"a client access profile cannot contain the organization-scope "
                    f"permission {permission}"
                )
        # profile_version_unique
        if any(v.version == version for v in self._versions.get(profile_id, [])):
            raise ValueError(f"profile {profile_id} already has a version {version}")

        record = ProfileVersion(
            id=uuid.uuid4(),
            profile_id=profile_id,
            version=version,
            summary=summary,
            permissions=permissions,
            restrictions=ProfileRestrictions.from_mapping(restrictions),
            published_by_user_id=published_by_user_id,
        )
        self._versions.setdefault(profile_id, []).append(record)
        return record.id

    async def assign_profile(
        self,
        *,
        administration_id: uuid.UUID,
        profile_id: uuid.UUID,
        assigned_by_user_id: uuid.UUID,
        at: datetime,
    ) -> uuid.UUID:
        # administration_access_profile_current_idx: supersede first, so at
        # most one assignment per administration is ever in force.
        for index, assignment in enumerate(self._assignments):
            if (
                assignment.administration_id == administration_id
                and assignment.superseded_at is None
            ):
                self._assignments[index] = _Assignment(
                    id=assignment.id,
                    administration_id=assignment.administration_id,
                    profile_id=assignment.profile_id,
                    assigned_by_user_id=assignment.assigned_by_user_id,
                    assigned_at=assignment.assigned_at,
                    superseded_at=at,
                )

        record = _Assignment(
            id=uuid.uuid4(),
            administration_id=administration_id,
            profile_id=profile_id,
            assigned_by_user_id=assigned_by_user_id,
            assigned_at=at,
        )
        self._assignments.append(record)
        return record.id

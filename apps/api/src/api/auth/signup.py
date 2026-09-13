"""FR-MDL-001/FR-ONB-001a/FR-ONB-001b: the one signup question, and what it
creates.

Ties three previously-separate foundations together for the first time -
`api.auth.service.AuthenticationService` (a user and a password credential),
migration 0001's `app.signup_self_managed_organization`/
`app.signup_firm_organization` (an organization, kind branched on the one
question), and the authorization schema's `role_assignment` (making the new
user its Owner) - in one transaction, so there is no instant at which a user
exists with no organization, or an organization exists with no owner.

--- Why the founding grant bypasses api.authz.service.AuthorizationService.assign_role ---

IAM-063 correctly forbids a user granting themselves a permission they do
not hold, and `assign_role`'s self-grant check enforces exactly that - for
every case except this one. A brand-new organization's first user cannot
possibly already hold a grant on it; someone has to be first, and that is a
property of founding an organization, not an escalation within one that
already has a power structure. This module inserts the founding
`role_assignment` row directly, in the same transaction as the organization
that makes it meaningful, rather than teaching the general-purpose grant
path a bootstrap exception it would then have to keep safe for every OTHER
caller too.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.breach_check import PasswordBreachChecker
from api.auth.models import User
from api.auth.repository import SqlUserRepository
from api.auth.service import AuthenticationService

AccountModel = Literal["self_managed", "firm"]


class OwnerRoleMissingError(Exception):
    """The system 'Owner' role (seeded by migration 0010's generated
    catalogue) does not exist. Unreachable in a correctly migrated
    database - raised rather than assumed, because a signup that silently
    created an organization with no owner would be a worse failure than a
    loud one here.
    """


@dataclass(frozen=True, slots=True)
class SignupResult:
    user: User
    organization_id: uuid.UUID
    account_model: AccountModel


class SignupService:
    def __init__(self, session: AsyncSession, breach_checker: PasswordBreachChecker) -> None:
        self._session = session
        self._authentication = AuthenticationService(SqlUserRepository(session), breach_checker)

    async def signup(
        self,
        *,
        account_model: AccountModel,
        organization_name: str,
        kvk_number: str | None,
        email: str,
        password: str,
    ) -> SignupResult:
        """FR-MDL-001's one question, answered by `account_model`. Raises
        whatever AuthenticationService.register_user raises
        (UserAlreadyExistsError, WeakPasswordError) before anything else
        happens - the organization is created only once the account itself
        is known to be creatable.
        """
        user = await self._authentication.register_user(email, password)

        organization_id = await self._create_organization(
            account_model, name=organization_name, kvk_number=kvk_number
        )

        # Every subsequent statement in this transaction (the role
        # assignment below) is tenant-scoped RLS, not a SECURITY DEFINER
        # bypass - it needs app.current_org_id() to actually be the
        # organization just created. is_local=false would survive a commit
        # this transaction has not reached yet; true (the default) is
        # correct and sufficient here since everything remaining runs
        # inside this same transaction.
        await self._session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )
        await self._grant_founding_owner(user.id, organization_id)

        return SignupResult(user=user, organization_id=organization_id, account_model=account_model)

    async def _create_organization(
        self, account_model: AccountModel, *, name: str, kvk_number: str | None
    ) -> uuid.UUID:
        function_name = (
            "app.signup_self_managed_organization"
            if account_model == "self_managed"
            else "app.signup_firm_organization"
        )
        result = await self._session.execute(
            text(f"SELECT (({function_name}(:name, :kvk))).id"),
            {"name": name, "kvk": kvk_number},
        )
        organization_id: uuid.UUID = result.scalar_one()
        return organization_id

    async def _grant_founding_owner(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        role_id_result = await self._session.execute(
            text("SELECT id FROM \"role\" WHERE name = 'Owner' AND is_system"),
        )
        role_id = role_id_result.scalar_one_or_none()
        if role_id is None:
            raise OwnerRoleMissingError

        await self._session.execute(
            text(
                "INSERT INTO role_assignment "
                "(user_id, role_id, scope_type, scope_id, granted_by_user_id) "
                "VALUES (:user_id, :role_id, 'organization', :org_id, :user_id)"
            ),
            {"user_id": str(user_id), "role_id": str(role_id), "org_id": str(organization_id)},
        )

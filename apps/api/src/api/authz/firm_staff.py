"""Firm staff access: IAM-107 and IAM-108 (PRD §8.6).

    IAM-107  Firm staff access is granted per client administration, not per
             firm. A new firm employee starts with access to zero clients.
    IAM-108  Firm staff grants may carry an expiry, and support seasonal or
             interim staff working on a defined client set for a defined
             period.

--- Most of IAM-107 is a property the model already had ---

"Not per firm" holds because an organization-scoped grant cascades only to
the administrations that organization OWNS (ADR-011's _scope_covers), and a
firm never owns its client's administration. "Starts with access to zero
clients" holds because access is a role_assignment row or it does not exist -
there is no bulk path, no wildcard scope, and nothing that runs on hire.
Neither is enforced by a check; both are consequences of shapes chosen
earlier, which is why this module mostly ADDS a way to grant access
correctly rather than a way to stop granting it wrongly.

What was genuinely missing is provenance. An administration-scoped grant on
a client's books looked identical whether the client made it or the firm
did, which ADR-016 recorded as a real defect. Migration 0016 adds
granted_by_organization_id, derived from tenant context by a trigger, and
IAM-107 is the requirement that makes that distinction load-bearing rather
than merely tidy.

--- Why firm staff grants are NOT bounded by the granter's own permissions ---

ADR-013 made assign_role refuse to confer what the granter does not hold.
That rule cannot apply here, and the reason is structural rather than a
concession: §8.4 gives the Firm Manager the job of "assigning firm staff to
clients" while explicitly denying them ledger permissions, so a ceiling
based on the granter's own holdings would make the role unable to do the one
thing it exists for. A ceiling based on the FIRM's holdings deadlocks
instead - a newly engaged administration is one no firm staff member holds
anything on yet, so nobody could be assigned the first role.

What bounds a firm staff grant is the ENGAGEMENT. An active firm_engagement
is the client's own consent to this firm doing this administration's books
(FR-MDL-005/007, and the client can revoke it at any time - IAM-105). Within
that consent, how a firm allocates its own staff is the firm's business. The
client's protections do not depend on this ceiling: IAM-102 bounds what the
firm may give the CLIENT's users, and IAM-105's floor is untouchable by any
of it.

One limit does survive, and it is the important one: IAM-063's segregation
rule still applies, so a Firm Manager may assign Accountant to a COLLEAGUE
but not to themselves. That is exactly §8.4's "cannot post to a client's
ledger without also holding Accountant on it" read together with "nobody
grants themselves permissions they do not hold".
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from api.audit.log import AuditOutcome
from api.audit.trail import AuditTrail
from api.authz.model import ScopeType
from api.authz.sod import DutyContext, SegregationOfDutiesService


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FirmStaffGrant:
    assignment_id: uuid.UUID
    user_id: uuid.UUID
    administration_id: uuid.UUID
    role_name: str
    granted_at: datetime
    granted_by_user_id: uuid.UUID
    expires_at: datetime | None = None

    def is_live(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now


class NoActiveEngagementError(Exception):
    """IAM-107: a firm may only reach a client administration through an
    engagement the client agreed to.
    """


class NotPermittedToAssignStaffError(Exception):
    """§8.4 gives "assigning firm staff to clients" to the Firm Manager,
    which is `manage user_role` held at the FIRM - not at the client.
    """


class FirmStaffAccessService:
    """Grants and revokes firm staff access, one client administration at a
    time.

    Deliberately does not go through AuthorizationService.assign_role: that
    method checks authority and the IAM-036 ceiling at the organization
    OWNING the scope, which for a client administration is the client. Firm
    staff assignment is authorized at the firm and bounded by the engagement
    instead - see this module's docstring.
    """

    def __init__(
        self,
        repository: FirmStaffRepository,
        *,
        sod: SegregationOfDutiesService | None = None,
        audit: AuditTrail | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._sod = sod
        self._audit = audit
        self._clock = clock

    async def _audit_change(
        self,
        *,
        firm_organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
        action: str,
        outcome: AuditOutcome,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Recorded against the CLIENT's organization, not the firm's.

        IAM-094 lets a customer read their own audit log, and "a firm
        employee was given access to my books" is an event about the client -
        it is the one they most need to see. The firm-side view of the same
        fact is IAM-109's register (api.authz.firm_access_register), which
        reads live grants rather than history.
        """
        if self._audit is None:
            return
        owner = await self._repository.owning_organization(administration_id)
        if owner is None:
            return
        await self._audit.permission_change(
            organization_id=owner,
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type="firm_staff_access",
            outcome=outcome,
            detail={
                "firm_organization_id": str(firm_organization_id),
                "staff_user_id": str(staff_user_id),
                **(detail or {}),
            },
        )

    async def grant_access(
        self,
        *,
        firm_organization_id: uuid.UUID,
        granter_user_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_ids: Sequence[uuid.UUID],
        role_name: str,
        expires_at: datetime | None = None,
    ) -> list[uuid.UUID]:
        """IAM-107 and IAM-108 together: a defined client set, one role, one
        optional expiry.

        The set is validated in full before anything is written. A seasonal
        grant covering six clients where the firm's engagement on the fourth
        has lapsed must not leave three of them granted - "a defined client
        set for a defined period" is one decision, and a partially applied
        one would be a different, silently smaller decision nobody made.
        """
        if not administration_ids:
            raise ValueError("granting access to no administrations is not a grant")

        role = await self._repository.get_system_role(role_name)
        if role is None:
            raise ValueError(f"no system role named {role_name!r}")
        role_id, role_scope = role
        if role_scope != "administration":
            # IAM-107's "per client administration, not per firm" as a type
            # error: an organization-scoped role has no per-client meaning,
            # and granting one at the firm would reach the firm's own books
            # rather than the client's.
            raise ValueError(
                f"{role_name!r} is an organization-scope role and cannot be granted "
                "per client administration (IAM-107)"
            )

        if not await self._repository.holds_manage_user_role(
            user_id=granter_user_id, organization_id=firm_organization_id
        ):
            raise NotPermittedToAssignStaffError(
                f"user {granter_user_id} cannot assign firm staff for organization "
                f"{firm_organization_id}: assigning staff to clients requires "
                "manage user_role at the firm (§8.4, Firm Manager)"
            )

        wanted = list(dict.fromkeys(administration_ids))
        for administration_id in wanted:
            if not await self._repository.has_active_engagement(
                firm_organization_id=firm_organization_id,
                administration_id=administration_id,
            ):
                raise NoActiveEngagementError(
                    f"organization {firm_organization_id} has no active engagement on "
                    f"administration {administration_id} (IAM-107)"
                )

        await self._check_self_grant(
            firm_organization_id=firm_organization_id,
            granter_user_id=granter_user_id,
            staff_user_id=staff_user_id,
            role_id=role_id,
        )

        now = self._clock()
        if expires_at is not None and expires_at <= now:
            raise ValueError(
                "a firm staff grant that has already expired grants nothing; omit the "
                "expiry or set it in the future (IAM-108)"
            )

        assignments: list[uuid.UUID] = []
        for administration_id in wanted:
            assignments.append(
                await self._repository.create_firm_staff_assignment(
                    user_id=staff_user_id,
                    role_id=role_id,
                    administration_id=administration_id,
                    granted_by_user_id=granter_user_id,
                    expires_at=expires_at,
                    at=now,
                )
            )
            # One entry per administration, not one for the batch: each
            # client's audit log should show the grant that affected THEM,
            # and a client reading a row about four other companies would be
            # both confusing and a disclosure.
            await self._audit_change(
                firm_organization_id=firm_organization_id,
                actor_user_id=granter_user_id,
                staff_user_id=staff_user_id,
                administration_id=administration_id,
                action="grant",
                outcome=AuditOutcome.SUCCESS,
                detail={
                    "role": role_name,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
            )
        return assignments

    async def revoke_access(
        self,
        *,
        firm_organization_id: uuid.UUID,
        revoker_user_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> int:
        """Takes a staff member off one client. Returns how many grants were
        revoked, which is zero when they had none - not an error, because
        "make sure this person cannot reach this client" is a reasonable
        thing to ask when the answer is already yes.

        No client-rights-floor concern applies: these are the FIRM's grants
        on someone else's administration, and IAM-105 protects the client's
        Owner, whose assignment lives at the client's own organization.
        """
        if not await self._repository.holds_manage_user_role(
            user_id=revoker_user_id, organization_id=firm_organization_id
        ):
            raise NotPermittedToAssignStaffError(
                f"user {revoker_user_id} cannot manage firm staff for organization "
                f"{firm_organization_id}"
            )

        revoked = await self._repository.revoke_firm_staff_assignments(
            firm_organization_id=firm_organization_id,
            staff_user_id=staff_user_id,
            administration_id=administration_id,
            at=self._clock(),
        )
        if revoked:
            await self._audit_change(
                firm_organization_id=firm_organization_id,
                actor_user_id=revoker_user_id,
                staff_user_id=staff_user_id,
                administration_id=administration_id,
                action="revoke",
                outcome=AuditOutcome.SUCCESS,
                detail={"grants_revoked": revoked},
            )
        return revoked

    async def list_access(
        self, *, firm_organization_id: uuid.UUID, staff_user_id: uuid.UUID | None = None
    ) -> Sequence[FirmStaffGrant]:
        """ "Which clients can this employee reach, and until when." With
        staff_user_id omitted, the firm's whole portfolio of staff grants -
        the view a Firm Manager needs to answer IAM-108's seasonal-staff
        question before an engagement ends.
        """
        return await self._repository.list_firm_staff_grants(
            firm_organization_id=firm_organization_id,
            staff_user_id=staff_user_id,
            now=self._clock(),
        )

    async def _check_self_grant(
        self,
        *,
        firm_organization_id: uuid.UUID,
        granter_user_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> None:
        """IAM-063 survives the engagement bound: a Firm Manager assigning
        Accountant to a colleague is doing their job, and assigning it to
        themselves is granting themselves a permission they do not hold.
        """
        if self._sod is None or granter_user_id != staff_user_id:
            return

        conferred = await self._repository.role_permissions(role_id)
        held = await self._repository.unconditionally_held_permissions(
            user_id=granter_user_id,
            organization_id=firm_organization_id,
            now=self._clock(),
        )
        decision = await self._sod.check(
            DutyContext(
                organization_id=firm_organization_id,
                actor_user_id=granter_user_id,
                action="grant",
                resource_type="user_role",
                subject_user_id=staff_user_id,
                permissions_not_held=tuple(sorted(conferred - held)),
            )
        )
        if decision.refused:
            raise NotPermittedToAssignStaffError(decision.detail)


class FirmStaffRepository(Protocol):
    """See api.authz.firm_staff_repository for the SQL implementation and
    tests/support/fake_firm_staff_repository.py for the in-memory double.
    """

    async def get_system_role(self, name: str) -> tuple[uuid.UUID, ScopeType] | None: ...

    async def holds_manage_user_role(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool: ...

    async def has_active_engagement(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool: ...

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        """The client that owns this administration - which organization's
        audit log a firm staff grant belongs in.
        """
        ...

    async def role_permissions(self, role_id: uuid.UUID) -> set[tuple[str, str]]: ...

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]: ...

    async def create_firm_staff_assignment(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        administration_id: uuid.UUID,
        granted_by_user_id: uuid.UUID,
        expires_at: datetime | None,
        at: datetime,
    ) -> uuid.UUID: ...

    async def revoke_firm_staff_assignments(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> int: ...

    async def list_firm_staff_grants(
        self,
        *,
        firm_organization_id: uuid.UUID,
        staff_user_id: uuid.UUID | None,
        now: datetime,
    ) -> Sequence[FirmStaffGrant]: ...

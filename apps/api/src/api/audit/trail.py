"""Service-layer audit recording, for the IAM-090 paths that have no HTTP
route to hang a declaration on.

api.audit.middleware covers everything that arrives through a route. Two of
IAM-090's categories are mostly reached by services that no route calls yet:

  * permission_change - AuthorizationService.assign_role and
    revoke_assignment, FirmStaffAccessService, EngagementRevocationService.
    All built, all callable, none behind an endpoint.
  * authentication - api.auth.service and api.auth.account_recovery, same.

Wiring those at the service rather than waiting for their routes matters
because the record has to be made where the DECISION is, not where the HTTP
happens to be. A future route that calls assign_role gets the audit entry
for free; one that reimplements the grant would not, and that is the right
way round.

--- Why every method here takes an outcome ---

There is no `record_success` shortcut. A helper that defaulted to success
would make the failure path the one you have to remember, and the failure
path is the one worth having. Callers pass the outcome explicitly, including
on the exception branch.

--- Why the trail is optional everywhere it is injected ---

Every service that takes an AuditTrail takes it as `audit: AuditTrail | None
= None`, the same shape as `sod` and `profiles` on AuthorizationService. Not
to make auditing optional in production - api.main wires it - but so the
several hundred existing tests that construct these services can keep doing
so without a database. A required parameter would have meant either a null
object with the same effect, or rewriting every construction site to pass
something they do not exercise.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome


class AuditTrail:
    """A thin façade over AuditLog with one method per IAM-090 category the
    service layer emits. Thin on purpose: the guarantees are in the schema
    (ADR-020), and anything clever here would only be somewhere for them to
    go wrong.
    """

    def __init__(self, log: AuditLog) -> None:
        self._log = log

    async def permission_change(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        administration_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        actor_type: ActorType = ActorType.USER,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """IAM-090's "permission grants and revocations".

        Recorded for refused attempts as well as granted ones: someone
        repeatedly trying to grant themselves a role they cannot have is
        precisely what a reviewer is looking for, and a log of only the
        successes would not show it.
        """
        await self._record(
            AuditCategory.PERMISSION_CHANGE,
            organization_id=organization_id,
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            detail=detail,
        )

    async def configuration_change(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        administration_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        actor_type: ActorType = ActorType.USER,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """IAM-090's "configuration changes" - client access profiles, SoD
        deviations, security policy.
        """
        await self._record(
            AuditCategory.CONFIGURATION,
            organization_id=organization_id,
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            detail=detail,
        )

    async def authentication(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        action: str,
        outcome: AuditOutcome,
        actor_type: ActorType = ActorType.USER,
        source_ip: str | None = None,
        user_agent: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """IAM-090's "authentication events".

        Tenant-scoped, like every entry in this log: what a customer
        reviewing their own audit trail (IAM-094) wants to see is who signed
        in to THEIR organization. An attempt against an email that matches no
        account belongs to no tenant and is already recorded in auth_attempt
        (IAM-019) - putting it in some tenant's log would mean choosing a
        tenant arbitrarily.
        """
        await self._record(
            AuditCategory.AUTHENTICATION,
            organization_id=organization_id,
            administration_id=None,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            resource_type="session",
            resource_id=None,
            outcome=outcome,
            source_ip=source_ip,
            user_agent=user_agent,
            detail=detail,
        )

    async def _record(
        self,
        category: AuditCategory,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        actor_type: ActorType,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None,
        outcome: AuditOutcome,
        source_ip: str | None = None,
        user_agent: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        await self._log.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                category=category,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                source_ip=source_ip,
                user_agent=user_agent,
                detail=detail or {},
            )
        )

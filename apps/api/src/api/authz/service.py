"""The authorization library. CLAUDE.md's third non-negotiable - "one
authorization library, used everywhere; there is no second implementation" -
means this module, and every access decision in LEDGR resolves here: HTTP
routes through api.authz.dependencies, background workers and the ledger
service by calling AuthorizationService.authorize() directly.

IAM-034: every decision is evaluated server-side, per request, against
current state. Nothing here caches. There is no permission set baked into a
token, no memoized "effective permissions" object with a TTL, and no
in-process grant cache to invalidate. authorize() reads live rows on every
call, which is also how IAM-037's 60-second propagation bound is met - a
revoked grant stops working on the very next request, not within a minute
of one. That costs a query per decision; api.mfa_middleware and
api.auth.rate_limiting already accept the same tradeoff for the same
reason, and correctness on a revoked grant is not something to trade away
for a round trip.

--- The evaluation algorithm ---

Default deny (IAM-031) is the function's exit condition, not a branch: the
only `return allowed` is inside the loop, so every path that falls out of it
denies. Adding a new grant shape without handling it correctly therefore
fails closed by construction.

For each live grant the user holds that bundles the requested
(action, resource_type):

  1. Does the grant's scope cover the target? (see _scope_covers)
  2. Are the grant's attribute conditions satisfied by this request's
     attributes? (api.authz.conditions, IAM-033)

The first grant passing both allows. Holding two roles means holding the
union of what they permit, which is ordinary RBAC - but note that a
condition on one assignment never constrains a different assignment. An
Approver capped at EUR 5,000 who is ALSO an Accountant approves without a
ceiling, because the Accountant grant is a separate, uncapped grant. That is
correct (the cap belongs to the Approver hat, not to the person), and it is
the behaviour access reviews under IAM-040 exist to keep honest.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from api.audit.log import AuditOutcome
from api.audit.trail import AuditTrail
from api.authz.conditions import evaluate_conditions
from api.authz.model import (
    AdministrationScope,
    AssignmentRecord,
    AuthorizationDecision,
    AuthorizationRequest,
    Grant,
    OrganizationScope,
    RoleRecord,
    ScopeType,
    TargetScope,
)
from api.authz.profiles import ClientAccessProfileService
from api.authz.rights_floor import (
    CLIENT_RIGHTS_FLOOR,
    OWNER_ROLE_NAME,
    floor_phrase,
)
from api.authz.sod import DutyContext, SegregationOfDutiesService, SodDecision

# The permission that makes someone an "organization admin" in IAM-036's
# sense. Named once so the authority check and its error message cannot
# drift apart.
COMPOSE_ROLES_PERMISSION = ("manage", "user_role")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PrivilegeEscalationError(Exception):
    """IAM-036: a custom role may only be composed from permissions its
    author already holds.

    `via` maps each missing permission to where it entered the request - the
    name of the component role that contributed it, or DIRECTLY. Without it,
    an author nesting a role with sixty permissions gets told only that the
    composition was refused, with no way to see which component was
    responsible.
    """

    DIRECTLY = "requested directly"

    def __init__(
        self,
        missing: Iterable[tuple[str, str]],
        *,
        via: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self.missing = sorted(missing)
        self.via = dict(via or {})
        listed = ", ".join(
            f"{action} {resource_type} (via {self.via.get((action, resource_type), self.DIRECTLY)})"
            for action, resource_type in self.missing
        )
        super().__init__(
            f"cannot compose a role containing permissions the author does not hold: {listed}"
        )


class RoleCompositionError(Exception):
    """A composition that is malformed rather than an escalation attempt: an
    unknown or invisible component, one belonging to another organization, an
    archived one, or a scope mismatch. Separate from
    PrivilegeEscalationError so a caller (and an audit trail) can tell "you
    tried to grant yourself more" from "that role id is wrong."
    """


class NotPermittedToComposeError(Exception):
    """IAM-036 says organization ADMINS compose roles. Holding the
    permissions that would go into a role is not by itself authority to
    define one; `manage user_role` is.
    """


class ClientRightsFloorError(Exception):
    """IAM-105: the action would leave the client's Owner without a right
    the floor guarantees. Distinct from every other refusal in this module
    because it is not about the ACTOR's authority - a legitimate Owner
    acting entirely within their permissions is still refused, because the
    floor is not theirs to remove either.
    """


class SegregationOfDutiesError(Exception):
    """A refusal by api.authz.sod rather than by the permission model. Kept
    separate from PrivilegeEscalationError because the two mean different
    things to whoever reads the error: "you may not hold this" versus "you
    may hold it, but not by this route, from this actor."
    """

    def __init__(self, decision: SodDecision) -> None:
        self.decision = decision
        super().__init__(decision.detail)


class AuthorizationRepository(Protocol):
    async def live_grants(
        self,
        *,
        user_id: uuid.UUID,
        action: str,
        resource_type: str,
        now: datetime,
    ) -> Sequence[Grant]:
        """Every unrevoked, unexpired assignment held by this user whose
        role bundles this exact (action, resource_type). Filtering by
        action/resource_type in the query rather than in Python keeps the
        per-request cost proportional to what was asked for, not to how many
        roles the user holds in total.
        """
        ...

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        """administration.organization_id - DIRECT ownership only. Never an
        engagement: see _scope_covers for why that distinction is the whole
        of §8.4's "a Firm Manager cannot post to a client's ledger."
        """
        ...

    async def unconditionally_held_permissions(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, now: datetime
    ) -> set[tuple[str, str]]:
        """Every (action, resource_type) this user can exercise WITHOUT
        limit somewhere within this organization - the ceiling on what they
        may compose into a custom role or hand to someone else (IAM-036).

        Permissions reachable only through a conditioned grant are excluded.
        An Approver capped at EUR 5,000 does not hold "approve purchase
        invoices"; they hold "approve purchase invoices up to EUR 5,000",
        which is not a thing this model can put in a role - conditions live
        on assignments. Counting it would let them author an uncapped role
        and, if they also hold `manage user_role`, assign it to themselves.
        """
        ...

    async def is_organization_owner(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        """A live, unrevoked, unexpired assignment of the SYSTEM Owner role
        at this organization - IAM-105's "the client's Owner".
        """
        ...

    async def count_active_owners(self, organization_id: uuid.UUID) -> int:
        """How many users hold that role right now. The floor needs at least
        one at all times, so revoking the last is refused (IAM-105).
        """
        ...

    async def revoke_assignment(self, *, assignment_id: uuid.UUID, at: datetime) -> None: ...

    async def get_assignment(self, assignment_id: uuid.UUID) -> AssignmentRecord | None: ...

    async def get_roles(self, role_ids: Sequence[uuid.UUID]) -> Sequence[RoleRecord]:
        """The named roles, as far as the caller may see them. A role the
        caller cannot see is simply absent from the result rather than an
        error here - the service turns that into a RoleCompositionError, so
        "no such role" and "another organization's role" are one answer.
        """
        ...

    async def role_permissions(self, role_ids: Sequence[uuid.UUID]) -> set[tuple[str, str]]:
        """The union of what these roles bundle. Flat, not recursive: every
        stored role's role_permission rows are already its full closure (see
        migrations/0011_custom_role_composition.sql), so nesting resolves
        transitively without this ever walking role_component.
        """
        ...

    async def create_custom_role(
        self,
        *,
        organization_id: uuid.UUID,
        name: str,
        scope_type: ScopeType,
        permissions: Iterable[tuple[str, str]],
        component_role_ids: Sequence[uuid.UUID],
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID: ...

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
    ) -> uuid.UUID: ...


def _scope_covers(grant: Grant, target: TargetScope, owning_organization: uuid.UUID | None) -> bool:
    """IAM-032, in full.

    An administration-scoped grant covers exactly one administration and
    nothing else. It does not cover the organization that owns it, and it
    does not cover a sibling administration - the only comparison made is
    equality against an AdministrationScope target. There is deliberately no
    branch here that maps an administration-scoped grant onto an
    organization target; that direction is what "widening" means, and it is
    absent from the code rather than guarded against in it.

    An organization-scoped grant covers that organization, and cascades down
    to the administrations the organization DIRECTLY OWNS. The cascade keys
    on administration.organization_id and never on firm_engagement, which is
    what makes §8.4's "a Firm Manager cannot post to a client's ledger
    without also holding Accountant on it" true structurally: a firm never
    owns its client's administration, so no organization-scoped grant held
    at the firm can ever reach it. Firm staff access to a client is always a
    separate, explicit, administration-scoped assignment.
    """
    if grant.scope_type == "administration":
        return (
            isinstance(target, AdministrationScope) and grant.scope_id == target.administration_id
        )

    if isinstance(target, OrganizationScope):
        return grant.scope_id == target.organization_id

    return owning_organization is not None and grant.scope_id == owning_organization


class AuthorizationService:
    def __init__(
        self,
        repository: AuthorizationRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sod: SegregationOfDutiesService | None = None,
        profiles: ClientAccessProfileService | None = None,
        audit: AuditTrail | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        # IAM-090's "permission grants and revocations". Optional for the
        # same reason sod and profiles are - see api.audit.trail's docstring
        # - and wired by api.main in production.
        self._audit = audit
        # Optional so the permission model can be exercised on its own, but
        # production wiring supplies it: without it, assign_role still
        # REFUSES a self-grant (the ceiling check below catches it) but does
        # so silently, and IAM-064 wants the attempt in the audit report.
        self._sod = sod
        # IAM-100's cap. Applied inside authorize() rather than left to
        # callers because CLAUDE.md's third non-negotiable means every
        # access decision resolves here - a profile a caller could forget to
        # consult would be no cap at all.
        self._profiles = profiles

    async def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        now = self._clock()

        floor = await self._client_rights_floor(request)
        if floor is not None:
            return floor

        grants = await self._repository.live_grants(
            user_id=request.user_id,
            action=request.action,
            resource_type=request.resource_type,
            now=now,
        )
        if not grants:
            return AuthorizationDecision(
                allowed=False,
                reason="no_matching_grant",
                detail=(
                    f"no live grant of {request.action} {request.resource_type} "
                    f"for user {request.user_id}"
                ),
            )

        # Resolved at most once per decision, and only when it can matter:
        # an organization-scoped grant checked against an administration
        # target is the sole case that needs it.
        owning_organization: uuid.UUID | None = None
        owner_resolved = False

        last_denial: AuthorizationDecision | None = None

        for grant in grants:
            # An organization-level capability requested against a single
            # administration is a caller error, not a near-miss: "manage
            # security policy on administration X" is not a question this
            # model can answer, and answering it by silently promoting the
            # target to the owning organization would be the widening
            # IAM-032 forbids.
            if grant.resource_scope == "organization" and isinstance(
                request.target, AdministrationScope
            ):
                last_denial = AuthorizationDecision(
                    allowed=False,
                    reason="resource_scope_mismatch",
                    detail=(
                        f"{request.action} {request.resource_type} is an organization-level "
                        "action and cannot be requested against a single administration"
                    ),
                )
                continue

            if (
                grant.scope_type == "organization"
                and isinstance(request.target, AdministrationScope)
                and not owner_resolved
            ):
                owning_organization = await self._repository.owning_organization(
                    request.target.administration_id
                )
                owner_resolved = True

            if not _scope_covers(grant, request.target, owning_organization):
                last_denial = AuthorizationDecision(
                    allowed=False,
                    reason="scope_not_covered",
                    detail=(
                        f"grant is scoped to {grant.scope_type} {grant.scope_id}, "
                        "which does not cover this request's target"
                    ),
                )
                continue

            outcome = evaluate_conditions(grant.conditions, request.attributes)
            if not outcome.satisfied:
                last_denial = AuthorizationDecision(
                    allowed=False,
                    reason="condition_not_satisfied",
                    assignment_id=grant.assignment_id,
                    detail=f"{outcome.failed_key}: {outcome.detail}",
                )
                continue

            capped = await self._capped_by_profile(request, grant)
            if capped is not None:
                last_denial = capped
                continue

            return AuthorizationDecision(
                allowed=True,
                reason="allowed",
                assignment_id=grant.assignment_id,
                detail=f"via {grant.role_name} on {grant.scope_type} {grant.scope_id}",
            )

        # Default deny (IAM-031). The most specific denial seen is reported
        # so a developer sees "amount exceeds ceiling" rather than a blanket
        # "no", but the OUTCOME is identical either way - a reported reason
        # is a debugging aid, never a partial grant.
        return last_denial or AuthorizationDecision(
            allowed=False, reason="no_matching_grant", detail="no grant covered this request"
        )

    async def _client_rights_floor(
        self, request: AuthorizationRequest
    ) -> AuthorizationDecision | None:
        """IAM-105: "Regardless of profile, the client's Owner ALWAYS
        retains ... No firm setting can remove these."

        Placed first in authorize(), before grants are even loaded, because
        "always" has to mean before every other consideration - a profile
        cap, an attribute condition, a mistake in a future layer. If the
        actor holds the Owner role at the organization that owns what they
        are asking about, and the permission is one of IAM-105's six, the
        answer is yes.

        This is the one place in the library where a permission is granted
        by something other than a role bundle, and it is narrow on purpose:
        it fires only for the fixed six permissions, only for a live,
        unrevoked, unexpired assignment of the SYSTEM Owner role, and only
        at the organization that owns the target. It is checked by role
        rather than by permission for the same reason IAM-064's
        acknowledgement is - a permission-based test could be satisfied by a
        custom role composed to hold it, and §8.4's Owner is a specific
        role, not a capability that can be arranged.

        The Owner keeps their floor on their OWN organization's
        administrations only. A firm's Owner gets nothing here on a client's
        books: the organization checked is always the one that owns the
        target, so an Owner at a firm is simply not the Owner of the client.
        """
        permission = (request.action, request.resource_type)
        if permission not in CLIENT_RIGHTS_FLOOR:
            return None

        if isinstance(request.target, OrganizationScope):
            organization_id = request.target.organization_id
        else:
            owner = await self._repository.owning_organization(request.target.administration_id)
            if owner is None:
                return None
            organization_id = owner

        if not await self._repository.is_organization_owner(
            user_id=request.user_id, organization_id=organization_id
        ):
            return None

        return AuthorizationDecision(
            allowed=True,
            reason="allowed",
            detail=(
                f"client rights floor (IAM-105): the client's Owner always retains "
                f"{floor_phrase(permission) or 'this right'}"
            ),
        )

    async def _capped_by_profile(
        self, request: AuthorizationRequest, grant: Grant
    ) -> AuthorizationDecision | None:
        """IAM-100: the firm-selected client access profile caps what the
        CLIENT's own users may do inside their administration.

        Three conditions have to hold before a cap applies, and each rules
        out a case where capping would be wrong:

        1. The target is a specific administration. A profile governs access
           inside one administration; it has nothing to say about
           organization-level actions, and 0013's permission guard refuses
           to store organization-scope permissions in one.
        2. The grant is the CLIENT's own - its scope belongs to the
           administration's owning organization. §8.6 says a profile governs
           "what the client's own users may do"; firm staff reach the same
           administration through an engagement and are not capped by the
           cap the firm itself set.
        3. A profile is actually assigned. No profile means no cap - never
           "no access" - so self-managed administrations and unconfigured
           firm-managed ones are unaffected.
        """
        if self._profiles is None or not isinstance(request.target, AdministrationScope):
            return None

        administration_id = request.target.administration_id
        owner = await self._repository.owning_organization(administration_id)
        if owner is None:
            return None

        if grant.scope_type == "organization":
            client_side = grant.scope_id == owner
        else:
            # An administration-scoped grant on this administration is held
            # by client staff AND by firm staff alike, so the grant's SCOPE
            # cannot tell them apart. Its provenance can: IAM-107 makes firm
            # staff access a grant made from the firm's tenant context, and
            # migration 0016 records that. A grant made by the owning
            # organization is the client's own and is capped; one made by an
            # engaged firm is firm staff and is not.
            #
            # A NULL granting organization (rows predating 0016; none exist)
            # falls to client-side, which caps rather than exempts.
            client_side = grant.scope_id == administration_id and (
                grant.granted_by_organization_id is None
                or grant.granted_by_organization_id == owner
            )
        if not client_side:
            return None

        access = await self._profiles.effective_access(
            administration_id,
            is_client_owner=grant.role_name == "Owner",
        )
        if access is None:
            return None

        if (request.action, request.resource_type) in access.permissions:
            # The profile's own IAM-104 restrictions apply on top, as
            # ordinary IAM-033 conditions - the same evaluator, so a
            # profile's journal restriction and a grant's behave identically.
            outcome = evaluate_conditions(access.conditions, request.attributes)
            if outcome.satisfied:
                return None
            return AuthorizationDecision(
                allowed=False,
                reason="capped_by_client_access_profile",
                assignment_id=grant.assignment_id,
                detail=(
                    f"the '{access.profile_name}' access profile restricts this: "
                    f"{outcome.failed_key}: {outcome.detail}"
                ),
            )

        return AuthorizationDecision(
            allowed=False,
            reason="capped_by_client_access_profile",
            assignment_id=grant.assignment_id,
            detail=(
                f"your accountant's '{access.profile_name}' access profile "
                f"(version {access.profile_version}) does not include "
                f"{request.action} {request.resource_type}"
            ),
        )

    async def _ceiling_for(
        self, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> set[tuple[str, str]]:
        return await self._repository.unconditionally_held_permissions(
            user_id=user_id, organization_id=organization_id, now=self._clock()
        )

    async def _require_admin(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        action, resource_type = COMPOSE_ROLES_PERMISSION
        decision = await self.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=OrganizationScope(organization_id),
            )
        )
        if decision.denied:
            raise NotPermittedToComposeError(
                f"user {user_id} does not hold {action} {resource_type} in organization "
                f"{organization_id} ({decision.reason})"
            )

    async def revoke_assignment(
        self, *, revoker_user_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> None:
        """The application path for taking a role away, and the point at
        which IAM-105 refuses to let the last Owner go.

        The same check exists as a database trigger (migration 0015), which
        is the real guarantee - this one exists so the ordinary path fails
        with an explanation rather than a constraint violation. Two layers
        because a trigger cannot produce a good error message and an
        application check cannot bind psql, the operator scripts, or a
        future service that writes the row directly.
        """
        assignment = await self._repository.get_assignment(assignment_id)
        if assignment is None:
            raise RoleCompositionError(f"assignment {assignment_id} is not available")
        if assignment.revoked_at is not None:
            return

        authority_organization = (
            assignment.scope_id
            if assignment.scope_type == "organization"
            else await self._repository.owning_organization(assignment.scope_id)
        )
        if authority_organization is None:
            raise RoleCompositionError(f"assignment {assignment_id} is not available")

        await self._require_admin(revoker_user_id, authority_organization)

        if (
            assignment.role_name == OWNER_ROLE_NAME
            and assignment.scope_type == "organization"
            and await self._repository.count_active_owners(assignment.scope_id) <= 1
        ):
            # Recorded as a refusal, not swallowed: an attempt to remove the
            # last Owner of an organization is exactly the event a reviewer
            # wants to see whether or not it succeeded (IAM-090).
            await self._audit_permission_change(
                organization_id=authority_organization,
                actor_user_id=revoker_user_id,
                action="revoke",
                outcome=AuditOutcome.DENIED,
                assignment=assignment,
                detail={"reason": "would remove the last Owner"},
            )
            raise ClientRightsFloorError(
                f"cannot revoke the last Owner of organization {assignment.scope_id}: "
                "IAM-105's client rights floor requires an Owner to hold it. Assign "
                "another Owner first."
            )

        await self._repository.revoke_assignment(assignment_id=assignment_id, at=self._clock())
        await self._audit_permission_change(
            organization_id=authority_organization,
            actor_user_id=revoker_user_id,
            action="revoke",
            outcome=AuditOutcome.SUCCESS,
            assignment=assignment,
        )

    async def _audit_permission_change(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        action: str,
        outcome: AuditOutcome,
        assignment: AssignmentRecord,
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.permission_change(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type="role_assignment",
            resource_id=assignment.id,
            administration_id=(
                assignment.scope_id if assignment.scope_type == "administration" else None
            ),
            outcome=outcome,
            detail={
                "role": assignment.role_name,
                "subject_user_id": str(assignment.user_id),
                "scope_type": assignment.scope_type,
                "scope_id": str(assignment.scope_id),
                **(detail or {}),
            },
        )

    async def create_custom_role(
        self,
        *,
        author_user_id: uuid.UUID,
        organization_id: uuid.UUID,
        name: str,
        scope_type: ScopeType,
        permissions: Iterable[tuple[str, str]] = (),
        component_roles: Sequence[uuid.UUID] = (),
    ) -> uuid.UUID:
        """IAM-036: "Custom roles can be composed by organization admins,
        but only from permissions they themselves hold - no privilege
        escalation by role authoring."

        A role may be composed from raw permissions, from other roles
        (`component_roles`), or both. Three separate gates, in order:

        1. AUTHORITY. The author must hold `manage user_role` in this
           organization. Holding the permissions that would go into a role
           is not authority to define one - IAM-036 says admins compose
           roles, and this is what makes that true rather than assumed.

        2. VALIDITY. Every component must exist, be visible to this
           organization, not be archived, and not drag an organization-scope
           role into an administration-scope one.

        3. CEILING. The flattened effective set - explicit permissions plus
           everything the components bundle - must be a subset of what the
           author holds UNCONDITIONALLY (see
           AuthorizationRepository.unconditionally_held_permissions). Checking
           the flattened set is what closes nesting: a component
           contributing a permission the author lacks is refused exactly as
           if they had typed it out.

        Nesting is transitive without recursing anywhere. Every stored
        role's role_permission rows are already its full closure (composition
        is flattened at creation - see
        migrations/0011_custom_role_composition.sql), so a component that was
        itself composed contributes everything it inherited, in one flat
        union.

        The ceiling is one query against the author's own grants rather than
        an authorize() call per candidate permission: authorize() needs a
        concrete target and, for conditioned grants, concrete resource
        attributes, neither of which a role DEFINITION has.
        """
        await self._require_admin(author_user_id, organization_id)

        # dict.fromkeys: de-duplicate while keeping the caller's order, so
        # error messages name components in the order they were given.
        component_ids = tuple(dict.fromkeys(component_roles))
        components = await self._resolve_components(component_ids, organization_id, scope_type)

        contributed_by: dict[tuple[str, str], str] = {}
        effective: set[tuple[str, str]] = set()

        for permission in permissions:
            effective.add(permission)
            contributed_by[permission] = PrivilegeEscalationError.DIRECTLY

        for component in components:
            for permission in await self._repository.role_permissions((component.id,)):
                effective.add(permission)
                # setdefault: a permission both requested directly and
                # inherited is attributed to the direct request, which is
                # what the author will recognize.
                contributed_by.setdefault(permission, component.name)

        if not effective:
            raise ValueError("a custom role must contain at least one permission")

        held = await self._ceiling_for(author_user_id, organization_id)
        missing = effective - held
        if missing:
            raise PrivilegeEscalationError(
                missing, via={key: contributed_by[key] for key in missing}
            )

        return await self._repository.create_custom_role(
            organization_id=organization_id,
            name=name,
            scope_type=scope_type,
            permissions=effective,
            component_role_ids=component_ids,
            created_by_user_id=author_user_id,
        )

    async def _resolve_components(
        self,
        component_ids: Sequence[uuid.UUID],
        organization_id: uuid.UUID,
        scope_type: ScopeType,
    ) -> list[RoleRecord]:
        if not component_ids:
            return []

        found = {record.id: record for record in await self._repository.get_roles(component_ids)}
        resolved: list[RoleRecord] = []

        for component_id in component_ids:
            record = found.get(component_id)
            if record is None:
                # Deliberately one message for "no such role" and "another
                # organization's role": distinguishing them would let an
                # author probe for the existence of roles in organizations
                # they cannot see (the same SEC-008 reasoning as
                # InvalidCredentialsError).
                raise RoleCompositionError(f"component role {component_id} is not available")
            if record.archived_at is not None:
                raise RoleCompositionError(f"component role {record.name} is archived")
            if not record.is_system and record.organization_id != organization_id:
                raise RoleCompositionError(f"component role {component_id} is not available")
            if scope_type == "administration" and record.scope_type == "organization":
                raise RoleCompositionError(
                    f"an administration-scope role cannot include the organization-scope "
                    f"role {record.name} (IAM-032)"
                )
            resolved.append(record)

        return resolved

    async def assign_role(
        self,
        *,
        granter_user_id: uuid.UUID,
        subject_user_id: uuid.UUID,
        role_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID,
        conditions: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> uuid.UUID:
        """The other half of IAM-036, without which composing safely is
        pointless: an author barred from writing a role granting more than
        they hold could otherwise just assign an existing role that does.

        Same two gates as composition - `manage user_role` in the
        organization, then the role's whole bundle must be within the
        granter's unconditional ceiling. A conditioned assignment narrows
        further; it can never reach past the ceiling, because `conditions`
        only ever subtracts (api.authz.conditions).

        For an administration-scoped assignment the authority is checked at
        the organization that OWNS that administration, not at the
        administration: `manage user_role` is an organization-level
        permission, and an administration target would be a resource-scope
        mismatch (see authorize()). Resolving the owner through the
        repository means an administration the granter cannot see yields no
        owner and the assignment is refused.
        """
        if scope_type == "organization":
            authority_organization = scope_id
        else:
            owner = await self._repository.owning_organization(scope_id)
            if owner is None:
                raise NotPermittedToComposeError(
                    f"administration {scope_id} is not visible to user {granter_user_id}"
                )
            authority_organization = owner

        await self._require_admin(granter_user_id, authority_organization)

        conferred = await self._repository.role_permissions((role_id,))
        if not conferred:
            raise RoleCompositionError(f"role {role_id} is not available or grants nothing")

        held = await self._ceiling_for(granter_user_id, authority_organization)
        missing = conferred - held

        # IAM-063, checked before the ceiling raises so the attempt is
        # RECORDED. The two overlap deliberately: the ceiling already makes
        # a self-grant of unheld permissions impossible (ADR-013), and this
        # gives the same refusal a named requirement and an audit row.
        if self._sod is not None:
            decision = await self._sod.check(
                DutyContext(
                    organization_id=authority_organization,
                    actor_user_id=granter_user_id,
                    action="grant",
                    resource_type="user_role",
                    subject_user_id=subject_user_id,
                    permissions_not_held=tuple(sorted(missing)),
                )
            )
            if decision.refused:
                await self._audit_grant(
                    organization_id=authority_organization,
                    granter_user_id=granter_user_id,
                    subject_user_id=subject_user_id,
                    role_id=role_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    outcome=AuditOutcome.DENIED,
                    detail={
                        "reason": "segregation of duties",
                        "rule": decision.rule.value if decision.rule else None,
                    },
                )
                raise SegregationOfDutiesError(decision)

        if missing:
            # A refused grant is a permission change that someone ATTEMPTED,
            # which IAM-090 covers as squarely as one that happened - and is
            # the more interesting of the two to a reviewer.
            await self._audit_grant(
                organization_id=authority_organization,
                granter_user_id=granter_user_id,
                subject_user_id=subject_user_id,
                role_id=role_id,
                scope_type=scope_type,
                scope_id=scope_id,
                outcome=AuditOutcome.DENIED,
                detail={
                    "reason": "exceeds the granter's ceiling",
                    "missing": sorted(f"{a} {r}" for a, r in missing),
                },
            )
            raise PrivilegeEscalationError(missing)

        assignment_id = await self._repository.create_assignment(
            user_id=subject_user_id,
            role_id=role_id,
            scope_type=scope_type,
            scope_id=scope_id,
            conditions=dict(conditions or {}),
            granted_by_user_id=granter_user_id,
            expires_at=expires_at,
        )
        await self._audit_grant(
            organization_id=authority_organization,
            granter_user_id=granter_user_id,
            subject_user_id=subject_user_id,
            role_id=role_id,
            scope_type=scope_type,
            scope_id=scope_id,
            outcome=AuditOutcome.SUCCESS,
            resource_id=assignment_id,
            detail={
                "conditioned": bool(conditions),
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        return assignment_id

    async def _audit_grant(
        self,
        *,
        organization_id: uuid.UUID,
        granter_user_id: uuid.UUID,
        subject_user_id: uuid.UUID,
        role_id: uuid.UUID,
        scope_type: ScopeType,
        scope_id: uuid.UUID,
        outcome: AuditOutcome,
        resource_id: uuid.UUID | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.permission_change(
            organization_id=organization_id,
            actor_user_id=granter_user_id,
            action="grant",
            resource_type="role_assignment",
            resource_id=resource_id,
            administration_id=scope_id if scope_type == "administration" else None,
            outcome=outcome,
            detail={
                "subject_user_id": str(subject_user_id),
                "role_id": str(role_id),
                "scope_type": scope_type,
                "scope_id": str(scope_id),
                **(detail or {}),
            },
        )

"""Value types for the authorization library (IAM-030 - IAM-037).

The one type worth reading closely is TargetScope. IAM-032 says "a grant at
administration level cannot be widened to organization level by any code
path," and the shape of this type is how that is made unrepresentable
rather than merely checked: an AuthorizationRequest names EITHER an
organization OR an administration as its target, never both, because
OrganizationScope and AdministrationScope are separate types and the field
holds one value.

A request carrying both would be the widening bug: an administration-scoped
grant could be matched against the administration while an
organization-scoped grant was matched against the organization field, and
the two answers unioned - which is exactly "an administration-level grant
being read as organization-level reach." There is no way to construct that
request here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

ScopeType = Literal["organization", "administration"]


@dataclass(frozen=True, slots=True)
class OrganizationScope:
    """The request targets an organization as a whole."""

    organization_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AdministrationScope:
    """The request targets one specific administration."""

    administration_id: uuid.UUID


TargetScope = OrganizationScope | AdministrationScope


@dataclass(frozen=True, slots=True)
class ResourceAttributes:
    """The request-side half of IAM-033's attribute conditions: the facts
    about *this* action that a condition on a grant is checked against.

    Every field defaults to None, meaning "not supplied." A condition that
    needs a value that was not supplied is NOT satisfied - it does not fall
    back to unrestricted. That is what makes a route that forgets to declare
    where an amount comes from fail closed against an amount-ceilinged
    grant, rather than bypassing the ceiling.

    amount is a Decimal, never a float: NFR-031 puts every monetary value in
    the calculation path on decimal types, and an amount compared against a
    ceiling is squarely in that path. api.authz.dependencies parses request
    bodies with json.loads(..., parse_float=Decimal) so a JSON number never
    becomes a float on the way here.
    """

    amount: Decimal | None = None
    cost_centre_id: uuid.UUID | None = None
    journal_id: uuid.UUID | None = None
    period_id: uuid.UUID | None = None
    source_ip: str | None = None


@dataclass(frozen=True, slots=True)
class Grant:
    """One row of the join across role_assignment / role / role_permission /
    permission: a live, unexpired assignment held by the user that bundles
    the requested (action, resource_type).
    """

    assignment_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    scope_type: ScopeType
    scope_id: uuid.UUID
    resource_scope: ScopeType
    conditions: Mapping[str, Any]
    # IAM-107: which organization's tenant context created this grant. When
    # it differs from an administration's owning organization, the holder is
    # firm staff working under an engagement rather than one of the client's
    # own users - the distinction a client access profile turns on. None only
    # for rows predating migration 0016, which are read as client-side (the
    # safe direction: capped rather than exempt).
    granted_by_organization_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RoleRecord:
    """A role's identity, without its contents. Enough to validate that a
    role may be used as a component of another (IAM-036) without loading the
    bundle.
    """

    id: uuid.UUID
    name: str
    scope_type: ScopeType
    organization_id: uuid.UUID | None
    is_system: bool
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class AssignmentRecord:
    """One role_assignment row, without its permissions - enough to decide
    whether revoking it would breach IAM-105's floor.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    scope_type: ScopeType
    scope_id: uuid.UUID
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    user_id: uuid.UUID
    action: str
    resource_type: str
    target: TargetScope
    attributes: ResourceAttributes = field(default_factory=ResourceAttributes)


DecisionReason = Literal[
    "allowed",
    "no_matching_grant",
    "scope_not_covered",
    "resource_scope_mismatch",
    "condition_not_satisfied",
    # IAM-100: the client's own role permits this, but the firm-selected
    # client access profile governing the administration does not.
    "capped_by_client_access_profile",
]


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Deliberately not a bare bool. A denial's `detail` names the specific
    condition that failed, which is what turns "403" into something a
    developer can act on without attaching a debugger - and what an audit
    record of the denial (IAM-038+, not built) would carry.
    """

    allowed: bool
    reason: DecisionReason
    assignment_id: uuid.UUID | None = None
    detail: str | None = None

    @property
    def denied(self) -> bool:
        return not self.allowed

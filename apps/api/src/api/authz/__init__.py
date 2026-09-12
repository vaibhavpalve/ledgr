"""LEDGR's authorization library (IAM-030 - IAM-037).

CLAUDE.md's third non-negotiable: "One authorization library, used
everywhere. All authorization decisions are evaluated server-side per
request against current state, through the single shared library. Default
deny: absence of a matching grant is a denial."

This package IS that library. There is no second implementation, and no
service is expected to grow one - a background worker or the ledger service
authorizes by calling AuthorizationService.authorize() with an
AuthorizationRequest, exactly as an HTTP route does through
require_permission().

Start with:
  service.py       the evaluation algorithm and the IAM-036 role composer
  conditions.py    IAM-033 attribute conditions
  dependencies.py  the HTTP declaration, and how it cannot be forgotten
  model.py         the value types, including the one that makes IAM-032's
                   "no widening" unrepresentable rather than merely checked
"""

from api.authz.conditions import CONDITION_KEYS, evaluate_conditions
from api.authz.dependencies import (
    AUTHORIZATION_EXEMPT_PATHS,
    AttributeSource,
    AttributeSources,
    AuthorizationRequirement,
    administration_from_path,
    declared_requirements,
    from_body,
    from_path,
    from_query,
    get_authorization_service,
    organization_scope,
    require_permission,
)
from api.authz.model import (
    AdministrationScope,
    AuthorizationDecision,
    AuthorizationRequest,
    Grant,
    OrganizationScope,
    ResourceAttributes,
    RoleRecord,
    ScopeType,
    TargetScope,
)
from api.authz.repository import SqlAuthorizationRepository
from api.authz.service import (
    COMPOSE_ROLES_PERMISSION,
    AuthorizationRepository,
    AuthorizationService,
    NotPermittedToComposeError,
    PrivilegeEscalationError,
    RoleCompositionError,
)

__all__ = [
    "AUTHORIZATION_EXEMPT_PATHS",
    "COMPOSE_ROLES_PERMISSION",
    "CONDITION_KEYS",
    "AdministrationScope",
    "AttributeSource",
    "AttributeSources",
    "AuthorizationDecision",
    "AuthorizationRepository",
    "AuthorizationRequest",
    "AuthorizationRequirement",
    "AuthorizationService",
    "Grant",
    "NotPermittedToComposeError",
    "OrganizationScope",
    "PrivilegeEscalationError",
    "ResourceAttributes",
    "RoleCompositionError",
    "RoleRecord",
    "ScopeType",
    "SqlAuthorizationRepository",
    "TargetScope",
    "administration_from_path",
    "declared_requirements",
    "evaluate_conditions",
    "from_body",
    "from_path",
    "from_query",
    "get_authorization_service",
    "organization_scope",
    "require_permission",
]

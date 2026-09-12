"""IAM-105, the client rights floor.

    "Regardless of profile, the client's Owner always retains: read access to
     their own source documents and filed returns, export of their complete
     data, visibility of their own audit log, the ability to revoke the
     firm's access, and the ability to manage their own users' sign-in
     security. No firm setting can remove these."

--- Why this is a floor and not just a default ---

Everywhere else in this system, access is something you are granted and can
lose. The floor is the opposite: six rights that survive every mechanism
this codebase has for taking access away. That inversion is deliberate and
statutory - a Dutch business is legally required to retain its own books
(CMP-001, PRD §10.4's seven-year retention), so an accountant who could
lock a client out of their own filed returns would put the client in breach
of an obligation they cannot delegate.

--- The floor spans two scopes, and that is load-bearing ---

Four rights are administration-scoped (documents, filed returns, export,
audit log) and two are organization-scoped (revoking the firm's access,
managing sign-in security). The split is what makes the last two
structurally unreachable by a firm: a client access profile governs one
administration and cannot contain an organization-scope permission at all
(client_access_profile_permission_guard_trg, migration 0013), so no profile
a firm can write even has a place to put them.

--- Where each guarantee actually lives ---

The floor is defended at four independent layers, because "no firm setting,
no API call, no admin action" is a claim about every write path, not about
the one the feature was built for:

  1. api.authz.service.AuthorizationService.authorize() short-circuits: a
     client Owner asking for a floor right is allowed before profile caps
     or grant conditions are consulted at all. This is the guarantee that
     holds regardless of what any other layer got wrong.
  2. api.authz.profiles.resolve() adds the floor back after IAM-104
     restrictions, so a profile cannot withhold it, and _enforce_ceiling
     excludes it from IAM-102 - the floor is the client's own, not the
     firm's to confer or withhold.
  3. migration 0015's triggers stop the Owner ASSIGNMENT that carries the
     floor from being dismantled: no expiry, no attribute conditions, and
     the last Owner of an organization cannot be revoked. Triggers, not
     policies, because BYPASSRLS (ledgr_ops, used by the operator scripts)
     skips row-level security but never skips a trigger - so this layer
     binds support tooling too.
  4. The absence of privileges: role_permission has no DELETE grant to
     ledgr_app, so a floor permission cannot be removed from the Owner
     role's bundle by anyone the application can authenticate as.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.authz.model import ScopeType

Permission = tuple[str, str]


@dataclass(frozen=True, slots=True)
class FloorRight:
    """One clause of IAM-105, with the PRD's own words attached.

    `phrase` is not decoration: it is what a client-facing explanation and
    an audit report print, and keeping it beside the permission is what lets
    test_client_rights_floor.py assert that every clause of the requirement
    has an implementation rather than trusting a list.
    """

    permission: Permission
    scope: ScopeType
    phrase: str


FLOOR_RIGHTS: tuple[FloorRight, ...] = (
    FloorRight(
        ("view", "document"),
        "administration",
        "read access to their own source documents",
    ),
    FloorRight(
        ("view", "vat_return"),
        "administration",
        "read access to their filed returns",
    ),
    FloorRight(
        ("export", "report_data"),
        "administration",
        "export of their complete data",
    ),
    FloorRight(
        ("read", "audit_log"),
        "administration",
        "visibility of their own audit log",
    ),
    FloorRight(
        ("revoke", "firm_engagement"),
        "organization",
        "the ability to revoke the firm's access",
    ),
    FloorRight(
        ("manage", "security_policy"),
        "organization",
        "the ability to manage their own users' sign-in security",
    ),
)

# Seeing the administration at all is a precondition for every
# administration-scoped right above; a floor that granted reading documents
# inside an administration the Owner cannot open would be no floor.
IMPLIED_RIGHTS: frozenset[Permission] = frozenset({("view", "administration")})

CLIENT_RIGHTS_FLOOR: frozenset[Permission] = (
    frozenset(right.permission for right in FLOOR_RIGHTS) | IMPLIED_RIGHTS
)

# The administration-scoped subset - what a client access profile could
# otherwise withhold, and therefore what api.authz.profiles.resolve() has to
# add back.
ADMINISTRATION_FLOOR: frozenset[Permission] = (
    frozenset(r.permission for r in FLOOR_RIGHTS if r.scope == "administration") | IMPLIED_RIGHTS
)

# The organization-scoped subset. No profile can contain these (see the
# module docstring), so they need no restoring - but they are still floor
# rights, and the Owner-assignment triggers protect them.
ORGANIZATION_FLOOR: frozenset[Permission] = frozenset(
    r.permission for r in FLOOR_RIGHTS if r.scope == "organization"
)

# The system role that holds the floor. Named once, and by ROLE rather than
# by permission, for the same reason IAM-064's Owner acknowledgement is:
# a permission-based test could be satisfied by a custom role composed to
# hold it, and "the client's Owner" is a specific §8.4 role, not a
# capability someone can arrange to have.
OWNER_ROLE_NAME = "Owner"


def floor_phrase(permission: Permission) -> str | None:
    for right in FLOOR_RIGHTS:
        if right.permission == permission:
            return right.phrase
    return None

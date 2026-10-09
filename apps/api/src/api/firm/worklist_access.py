"""Who the firm home's cross-client reads are FOR: the portfolio, authorized per administration.

ADR-109, and the contract's decision 1. A firm session's RLS already reaches every administration
the firm has an active engagement on (`app.has_administration_access`). That is the first line,
and it is the FIRM's reach, not the person's. The second line is the same two steps for every
firm-home route:

  1. the administrations the PERSON holds a live grant on - the switcher's own join
     (`api.firm.switcher_repository._BASE`, read through `SqlSwitcherRepository`), so the firm
     home and the switcher can never disagree about whose books someone can see;
  2. `AuthorizationService.authorize()` for each of them, with the route's permission, scoped to
     that administration. Anything denied is left out - the default deny, applied per client.

There is no second authorization implementation here: the decision is the library's. What this
module adds is the HTTP edge for a route whose target is "every administration I may see"
rather than one named in the path - `require_permission` resolves exactly one target, and an
organization-scoped check would find nothing for firm staff, whose grants are
administration-scoped by design (IAM-107).

--- Declaring it ---

`require_portfolio_permission` carries the same `PERMISSION_MARKER` `require_permission` does,
so `AuthorizationEnforcementMiddleware`, tests/test_authz_coverage.py and
tests/test_audit_coverage.py see a declared requirement on every route that uses it. Its scope is
"administration" with no path parameter: the target is each portfolio administration in turn,
never one the request names.

--- Two hundred clients ---

`authorize()` reads the user's live grants for the permission on every call. For a portfolio
that would be one identical query per client, so the service here runs on a repository that
remembers, for the length of ONE request, the grant rows and the administrations' owners it has
already read (and reads the owners of the whole portfolio in one query up front). The decisions
are still made by `AuthorizationService`, from the same rows, against current state - nothing
outlives the request.

IAM-109's access register is deliberately not written for portfolio reads: counting a client's
open items is not opening their books, and writing one row per client on every load of the
firm home would make the register say everybody looked at everything every morning.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.authz.dependencies import (
    PERMISSION_MARKER,
    AttributeSources,
    AuthorizationRequirement,
    ScopeSpec,
)
from api.authz.model import (
    AdministrationScope,
    AuthorizationDecision,
    AuthorizationRequest,
    Grant,
)
from api.authz.repository import SqlAuthorizationRepository
from api.authz.service import AuthorizationService
from api.db import get_db_session
from api.firm.switcher import ClientSwitcher, SwitcherEntry
from api.firm.switcher_repository import SqlSwitcherRepository
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context


class RequestScopedAuthorizationRepository(SqlAuthorizationRepository):
    """`SqlAuthorizationRepository`, remembering what it read for the length of one request.

    Only the three reads `authorize()` makes are remembered; every write, and every other read,
    is the parent's. A new instance per request (see `get_portfolio_authorizer`), so nothing is
    ever reused across requests or users.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._db = session
        self._grants: dict[tuple[uuid.UUID, str, str], Sequence[Grant]] = {}
        self._owners: dict[uuid.UUID, uuid.UUID | None] = {}
        self._is_owner: dict[tuple[uuid.UUID, uuid.UUID], bool] = {}

    async def prime_owners(self, administration_ids: Sequence[uuid.UUID]) -> None:
        """The owning organization of every administration in one query, under RLS - an
        administration the session cannot see reads as None, exactly as one at a time would."""
        missing = [a for a in administration_ids if a not in self._owners]
        if not missing:
            return
        result = await self._db.execute(
            text(
                "SELECT id, organization_id FROM administration "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [str(a) for a in missing]},
        )
        found = {row.id: row.organization_id for row in result}
        for administration_id in missing:
            self._owners[administration_id] = found.get(administration_id)

    async def live_grants(
        self, *, user_id: uuid.UUID, action: str, resource_type: str, now: datetime
    ) -> Sequence[Grant]:
        key = (user_id, action, resource_type)
        if key not in self._grants:
            self._grants[key] = await super().live_grants(
                user_id=user_id, action=action, resource_type=resource_type, now=now
            )
        return self._grants[key]

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        if administration_id not in self._owners:
            self._owners[administration_id] = await super().owning_organization(administration_id)
        return self._owners[administration_id]

    async def is_organization_owner(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> bool:
        key = (user_id, organization_id)
        if key not in self._is_owner:
            self._is_owner[key] = await super().is_organization_owner(
                user_id=user_id, organization_id=organization_id
            )
        return self._is_owner[key]


Authorize = Callable[[AuthorizationRequest], Awaitable[AuthorizationDecision]]
Prime = Callable[[Sequence[uuid.UUID]], Awaitable[None]]


class PortfolioAuthorizer:
    """Step 2 of the module docstring: `authorize()` once per administration."""

    def __init__(self, authorize: Authorize, *, prime: Prime | None = None) -> None:
        self._authorize = authorize
        self._prime = prime

    async def allowed(
        self,
        *,
        user_id: uuid.UUID,
        administration_ids: Iterable[uuid.UUID],
        action: str,
        resource_type: str,
    ) -> set[uuid.UUID]:
        ids = list(dict.fromkeys(administration_ids))
        if self._prime is not None and ids:
            await self._prime(ids)
        allowed: set[uuid.UUID] = set()
        for administration_id in ids:
            decision = await self._authorize(
                AuthorizationRequest(
                    user_id=user_id,
                    action=action,
                    resource_type=resource_type,
                    target=AdministrationScope(administration_id),
                )
            )
            if decision.allowed:
                allowed.add(administration_id)
        return allowed


async def get_portfolio_authorizer(
    session: AsyncSession = Depends(get_db_session),
) -> PortfolioAuthorizer:
    repository = RequestScopedAuthorizationRepository(session)
    service = AuthorizationService(repository)
    return PortfolioAuthorizer(service.authorize, prime=repository.prime_owners)


async def get_portfolio_switcher(
    session: AsyncSession = Depends(get_db_session),
) -> ClientSwitcher:
    return ClientSwitcher(SqlSwitcherRepository(session))


@dataclass(frozen=True, slots=True)
class Portfolio:
    """The administrations this request may report on, in switcher order."""

    user_id: uuid.UUID
    organization_id: uuid.UUID
    entries: tuple[SwitcherEntry, ...]

    @property
    def administration_ids(self) -> list[uuid.UUID]:
        return [entry.badge.administration_id for entry in self.entries]

    def names(self) -> dict[uuid.UUID, str]:
        return {entry.badge.administration_id: entry.badge.display_name for entry in self.entries}


#: The permission every firm-home READ is evaluated against, per administration. "View reports" is
#: what FR-UX-005's mobile home already rides (api.dashboard.routes): the firm home is the same
#: question asked across clients - how are these books doing - and Owner, Accountant, Bookkeeper
#: and Viewer hold it while Invoicer and Expense Submitter do not.
PORTFOLIO_ACTION = "view"
PORTFOLIO_RESOURCE = "report"


def require_portfolio_permission(
    action: str = PORTFOLIO_ACTION,
    resource_type: str = PORTFOLIO_RESOURCE,
    *,
    audit: AuditCategory | None = None,
) -> Callable[..., Awaitable[Portfolio]]:
    """The dependency a firm-home route declares. Resolves to the caller's `Portfolio`: the
    administrations they hold a live grant on AND are authorized for `action resource_type` on.

    An empty portfolio is an answer, not an error - a firm with no clients yet, or a person whose
    grants do not include this permission anywhere, gets empty results rather than a 403, the
    same way the switcher returns an empty list. Nothing is disclosed either way.
    """
    requirement = AuthorizationRequirement(
        action=action,
        resource_type=resource_type,
        scope=ScopeSpec("administration"),
        attributes=AttributeSources(),
        audit_category=audit,
    )

    async def dependency(
        request: Request,
        tenant: TenantContext = Depends(get_tenant_context),
        switcher: ClientSwitcher = Depends(get_portfolio_switcher),
        authorizer: PortfolioAuthorizer = Depends(get_portfolio_authorizer),
    ) -> Portfolio:
        if tenant.user_id is None:
            raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
        entries = await switcher.list(tenant.user_id)
        allowed = await authorizer.allowed(
            user_id=tenant.user_id,
            administration_ids=[entry.badge.administration_id for entry in entries],
            action=action,
            resource_type=resource_type,
        )
        return Portfolio(
            user_id=tenant.user_id,
            organization_id=tenant.organization_id,
            entries=tuple(e for e in entries if e.badge.administration_id in allowed),
        )

    setattr(dependency, PERMISSION_MARKER, requirement)
    return dependency


#: The read every firm-home GET declares - one module-level instance, so FastAPI resolves it once
#: per request however many routes share it, and no route builds it in an argument default.
PORTFOLIO_READ = require_portfolio_permission()

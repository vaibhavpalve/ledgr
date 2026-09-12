import uuid

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.audit.middleware import AuditMiddleware
from api.authz.dependencies import (
    administration_from_path,
    organization_scope,
    require_permission,
)
from api.authz.engagement_revocation import (
    EngagementRevocationService,
    NoSessionError,
    NoSuchEngagementError,
)
from api.authz.firm_access_repository import SqlEngagementRevocationRepository
from api.authz.model import AuthorizationDecision
from api.authz_middleware import AuthorizationEnforcementMiddleware
from api.customers import routes as customer_routes
from api.dashboard import routes as dashboard_routes
from api.db import get_db_session
from api.documents import routes as document_routes
from api.expenses import routes as expense_routes
from api.firm.switcher import ClientSwitcher, SwitcherEntry
from api.firm.switcher_repository import SqlSwitcherRepository
from api.i18n.http import problem
from api.i18n.language import SUPPORTED_LANGUAGE_CODES, parse_language
from api.idempotency_middleware import IdempotencyMiddleware
from api.invoicing import routes as invoice_routes
from api.mfa_middleware import MfaEnforcementMiddleware
from api.security.csrf import CsrfProtectionMiddleware
from api.security.headers import SecurityHeadersMiddleware
from api.templates import routes as template_routes
from api.tenancy import TenantContext, TenantContextMiddleware, get_tenant_context

app = FastAPI(title="LEDGR API")
# Order matters: the LAST middleware added runs FIRST (Starlette wraps
# outermost-last), verified empirically before relying on it - see
# api.mfa_middleware's module docstring. MfaEnforcementMiddleware reads
# request.state.tenant_context, which TenantContextMiddleware sets, so
# TenantContextMiddleware must be added second (outermost / runs first).
#
# AuthorizationEnforcementMiddleware is added FIRST, making it the innermost
# layer - it runs last, immediately before the router. The outer two answer
# "who is this and have they proven it"; this one answers "does the endpoint
# they are about to reach declare what it requires at all" (CLAUDE.md rule
# three). See its module docstring.
#
# AuditMiddleware sits between the two: outside MFA enforcement so a request
# refused there is still recorded as an attempt (IAM-090), and inside tenant
# context so it has an organization to attribute the entry to. It runs after
# the response with its own session, which is what lets a denied or failed
# request - whose transaction rolled back - still leave a durable record.
# IdempotencyMiddleware sits between MFA and authorization (NFR-032):
#   * inside MFA, so an unauthenticated or unverified request never claims a
#     key - a key burned by a request refused at the door would make the
#     caller's legitimate retry look like a duplicate.
#   * outside authorization, so a 403 IS stored and replayed. Deliberate: a
#     denial is a deterministic answer to this exact request, and a retry
#     would compute the same one.
# SecurityHeadersMiddleware and CsrfProtectionMiddleware are added LAST, below
# the other five - making SecurityHeadersMiddleware the outermost layer of
# all (SEC-003/SEC-004's CSP and HSTS land on every response, including a 401
# from TenantContextMiddleware or a 403 from AuthorizationEnforcementMiddleware,
# because outermost is the layer every response passes back through on the
# way out) and CsrfProtectionMiddleware the layer just inside it - before
# tenant context is even resolved, since the check depends only on cookies
# and the request method. See api.security.headers and api.security.csrf.
#
# PRD §6.10's archive. Added directly to the app rather than as an included
# router: this FastAPI version hides an included router's routes behind an
# opaque wrapper that the authorization middleware and all four coverage checks
# walk straight past. See api.documents.routes.register.
document_routes.register(app)
# PRD §6.7's receipt capture, registered the same way and for the same reason.
expense_routes.register(app)
invoice_routes.register(app)
# PRD §6.3's customer master (FR-AR-006). Registered before nothing in
# particular - route registration order carries no meaning here - but the same
# `register(app)` way, for the same wrapper reason.
customer_routes.register(app)
# FR-TPL-001..007's invoice template designer. Registered the same way and
# for the same reason; see api.templates.routes' module docstring for why its
# routes reuse invoicing's "create sales_invoice" permission rather than a
# permission of their own.
template_routes.register(app)
# FR-UX-005 / MOB-006's mobile home screen. Same registration reason again;
# see api.dashboard.routes' module docstring for why it reuses "View reports"
# rather than a permission of its own.
dashboard_routes.register(app)

app.add_middleware(AuthorizationEnforcementMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(MfaEnforcementMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(TenantContextMiddleware)
app.add_middleware(CsrfProtectionMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


async def get_switcher_service(
    session: AsyncSession = Depends(get_db_session),
) -> EngagementRevocationService:
    return EngagementRevocationService(SqlEngagementRevocationRepository(session))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/whoami")
def whoami(tenant: TenantContext = Depends(get_tenant_context)) -> dict[str, str]:
    return {"organization_id": str(tenant.organization_id)}


class LanguageChoice(BaseModel):
    """FR-LOC-001a's one click, as a request body."""

    language: str


@app.get("/v1/me/language")
async def get_my_language(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """FR-LOC-001b: the caller's own UI language.

    `language` is null when this person has never chosen one, which is a real
    state and not an error - IAM-010g's "applied to the account after first
    login" is the step that fills it in from the device's pre-login choice,
    and it needs to tell "never chosen" apart from "chose Dutch".

    `supported` is returned rather than hard-coded into every client, so that
    shipping a third language does not require a client release to make it
    selectable.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    # `users` carries no RLS - users are global to the platform (0003), not
    # tenant-scoped - so unlike every query against an administration-scoped
    # table, this one MUST name its own predicate. And the id comes from the
    # verified token, never from the path: there is no `/v1/users/{id}/language`
    # here precisely so that reading somebody else's preference has no route.
    result = await session.execute(
        text("SELECT language FROM users WHERE id = :user_id"),
        {"user_id": str(tenant.user_id)},
    )
    row = result.first()
    return {
        "language": None if row is None else row.language,
        "supported": list(SUPPORTED_LANGUAGE_CODES),
    }


@app.put("/v1/me/language")
async def set_my_language(
    request: Request,
    choice: LanguageChoice,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """FR-LOC-001a / FR-LOC-001b: change your own language, at any time.

    Nothing here makes the UI wait. The client switches language locally and
    calls this afterwards (see packages/i18n/src/react.tsx), so "taking effect
    immediately without reload or re-authentication" does not depend on this
    round trip - and a failure here costs the preference's persistence, never
    the switch the person just made.

    Writes only this caller's row. There is no route that sets another user's
    language: FR-LOC-001b makes it a property of the person, and an
    administrator changing what language a colleague reads in is not a
    capability the PRD grants anyone.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    language = parse_language(choice.language)
    if language is None:
        raise problem(
            request,
            422,
            "errors.language_unsupported",
            reason="unsupported_language",
            supported=", ".join(SUPPORTED_LANGUAGE_CODES),
        )

    await session.execute(
        text("UPDATE users SET language = :language, updated_at = now() WHERE id = :user_id"),
        {"language": language.value, "user_id": str(tenant.user_id)},
    )
    return {"language": language.value}


async def get_client_switcher(
    session: AsyncSession = Depends(get_db_session),
) -> ClientSwitcher:
    return ClientSwitcher(SqlSwitcherRepository(session))


def _entry_json(entry: SwitcherEntry) -> dict[str, object]:
    """One shape, produced once, so web and mobile cannot disagree about what
    identifies a client (FR-FRM-000a).
    """
    return {
        "administration_id": str(entry.badge.administration_id),
        "display_name": entry.badge.display_name,
        "legal_name": entry.badge.name,
        "trade_name": entry.badge.trade_name,
        "kvk_number": entry.badge.kvk_number,
        "colour": entry.badge.colour_token,
        "initials": entry.badge.initials,
        "colour_is_ambiguous": entry.colour_is_ambiguous,
        "role": entry.role_name,
        # FR-LOC-001. The NAME is the identifier and crosses the wire
        # untranslated - authorization matches on it, audit entries record it,
        # and a grant that read differently per reader would be a data defect,
        # not a feature. This flag is what lets the client translate the
        # LABEL for one of PRD §8.4's twelve system roles while showing a
        # custom role's name (ADR-013) verbatim, as tenant data.
        "role_is_system": entry.role_is_system,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
    }


@app.get("/v1/switcher/search")
async def search_switcher(
    request: Request,
    q: str | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    switcher: ClientSwitcher = Depends(get_client_switcher),
) -> list[dict[str, object]]:
    """FR-FRM-000: searchable by client name, KvK number or trade name,
    showing only granted administrations.

    "Only granted" is the repository's join, not a filter applied to a wider
    result - a switcher that fetched every administration and hid some would
    leak client names into a response body.

    Exempt from the permission check for the same reason /v1/switcher is:
    it returns only administrations the caller already holds a live grant
    on. Results are ordered exact-then-prefix-then-substring so the first
    one is what someone typing a specific name is reaching for, which is what
    makes type-then-Enter work.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    return [_entry_json(entry) for entry in await switcher.list(tenant.user_id, query=q)]


@app.get("/v1/switcher/active")
async def active_client(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    switcher: ClientSwitcher = Depends(get_client_switcher),
) -> dict[str, object] | None:
    """FR-FRM-000a: what the persistent header renders, on every screen.

    Derived from the SESSION's active client, never from a parameter the
    screen passes in. A header that took its name from the page's own state
    could drift from what the page posts to; one derived from the session
    cannot - and the active-client guard in require_permission refuses the
    write when they disagree anyway.

    Returns null when the session is not inside any client, which is a real
    state (at the switcher, or a non-interactive API caller) and not an
    error.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    if tenant.active_administration_id is None:
        return None

    entry = await switcher.find(tenant.user_id, tenant.active_administration_id)
    if entry is None:
        # The session points at a client the user can no longer reach -
        # revoked, expired, or switched away from elsewhere. The header must
        # show nothing rather than a stale name, because a stale name is
        # exactly the wrong-client failure FR-FRM-000a is about.
        return None

    return _entry_json(entry)


@app.get("/v1/switcher")
async def list_switcher(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    switcher: EngagementRevocationService = Depends(get_switcher_service),
) -> list[dict[str, str | None]]:
    """IAM-110: the firm switcher. Derived from the caller's live grants
    every time, never stored - which is what makes "the administration
    disappears from the firm switcher" on revocation a consequence rather
    than a step someone has to remember.

    In AUTHORIZATION_EXEMPT_PATHS, and the reason is not convenience: this
    returns only administrations the caller ALREADY holds a live grant on,
    so it discloses nothing they cannot already reach. Requiring a
    permission would also break it for exactly the users it exists for - a
    firm accountant's grants are administration-scoped (IAM-107), so an
    organization-scoped check at their own firm would find nothing.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    return [
        {
            "administration_id": str(entry.administration_id),
            "legal_name": entry.legal_name,
            "role": entry.role_name,
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        }
        for entry in await switcher.switchable_administrations(tenant.user_id)
    ]


@app.put("/v1/switcher/{administration_id}")
async def switch_administration(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    switcher: EngagementRevocationService = Depends(get_switcher_service),
    # The permission check the switcher listing does not need: entering an
    # administration is reaching into it, so it is authorized like any other
    # administration-scoped action. switch_to() then re-checks the switcher
    # itself, because holding `view administration` and appearing in the
    # switcher are not the same set - see its docstring.
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "administration",
            scope=administration_from_path("administration_id"),
            # The one route that legitimately writes to an administration
            # other than the session's current one: changing which that is
            # is its entire job (FR-FRM-000a).
            allows_cross_client=True,
            # IAM-090: which client someone was working in is the context an
            # auditor needs to read the rest of their activity, so switching
            # is recorded rather than exempted as "only a UI action".
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, str]:
    """Sets sessions.active_administration_id - the write that makes
    IAM-110's "firm sessions for that administration" name specific rows.

    Setting it grants nothing: every request still evaluates live grants
    (IAM-034), so a session pointing at an administration whose grants were
    revoked is denied exactly as if the column were empty.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    try:
        entry = await switcher.switch_to(
            session_id=tenant.session_id,
            user_id=tenant.user_id,
            administration_id=administration_id,
        )
    except NoSessionError as exc:
        # `str(exc)` stays out of the body: it is written for whoever reads a
        # log, and FR-UX-007 keeps developer-facing text away from users. The
        # exception still propagates its own text to the log through the
        # `from exc` chain.
        raise problem(request, 409, "errors.no_session", reason="no_session") from exc
    except NoSuchEngagementError as exc:
        raise problem(
            request, 404, "errors.engagement_not_found", reason="engagement_not_found"
        ) from exc

    return {
        "administration_id": str(entry.administration_id),
        "legal_name": entry.legal_name,
        "role": entry.role_name,
    }


@app.get("/v1/administrations")
async def list_administrations(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    # Organization-scoped: "may this caller view administrations in their own
    # tenant at all." Which specific rows come back is still RLS's answer,
    # not this check's - the two are different questions and both are needed
    # (IAM-002 for the tenant boundary, IAM-030 for the capability).
    _: AuthorizationDecision = Depends(
        require_permission("view", "administration", scope=organization_scope())
    ),
) -> list[dict[str, str]]:
    # No WHERE organization_id = ... here on purpose: RLS (ADR-003) is what
    # scopes this to the caller's tenant, not this query.
    result = await session.execute(
        text("SELECT id, legal_name, formatting_locale FROM administration ORDER BY legal_name")
    )
    return [
        {
            "id": str(row.id),
            "legal_name": row.legal_name,
            # FR-LOC-002: how figures in THESE books are written, for every
            # reader. Returned with the administration rather than fetched
            # separately, because a client that has an administration and not
            # its locale has to guess - and the guess it would make is the
            # reader's own language, which is the specific mistake the
            # requirement is written to prevent.
            "formatting_locale": row.formatting_locale,
        }
        for row in result
    ]


@app.get("/v1/administrations/{administration_id}")
async def get_administration(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    # Administration-scoped: the target is read from the path parameter, so
    # the decision is about the administration the request actually names.
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "administration",
            scope=administration_from_path("administration_id"),
            # IAM-090's "data reads of financial records". A GET is not
            # required to declare a category - see tests/test_audit_coverage.py
            # for why requiring one on every read would drown the log - but
            # reading into a client's administration is the read the
            # requirement names.
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, str]:
    result = await session.execute(
        text("SELECT id, legal_name, formatting_locale FROM administration WHERE id = :id"),
        {"id": str(administration_id)},
    )
    row = result.first()
    if row is None:
        # Indistinguishable from "does not exist" whether the row belongs to
        # another tenant or truly doesn't exist - RLS filters it out of the
        # query result either way, so this branch can't tell the difference,
        # and shouldn't try to. The message says both, in the caller's
        # language, for the same reason: it must not become the oracle that
        # tells them apart.
        raise problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        )
    return {
        "id": str(row.id),
        "legal_name": row.legal_name,
        # FR-LOC-002 - see the list endpoint above.
        "formatting_locale": row.formatting_locale,
    }

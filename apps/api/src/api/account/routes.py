"""`GET /v1/me`: the one call a client makes after sign-in to learn who it is
serving and where to send them (docs/founder-review-2026-09-14.md §4.1).

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register`'s docstring for why: this FastAPI version
hides an included router's routes behind a wrapper the authorization
middleware and the coverage checks walk straight past.

--- What it answers, and from where ---

    user             the caller's own `users` row (email, language)
    organization     the tenant the token names - kind decides the rest
    administrations  business: the administrations the organization owns
                     (RLS scopes the read) with the caller's own role on each;
                     firm: the client switcher's list (FR-FRM-000), which is
                     the administrations the caller holds a live grant on
    fiscal_years     per administration, through FiscalYearService
    active_administration_id
                     the SESSION's current client, read from its own row -
                     the value PUT /v1/switcher/{id} and POST
                     /v1/administrations write - so it is right the moment
                     either of those returns, whether or not the bearer token
                     restates it
    mfa              MfaEnrollmentChecker, the one place that decides what
                     counts as an enrolled factor (IAM-012)
    onboarding       `needs_administration`: true when there is nothing to
                     land on, which is the state a fresh signup is in

Authorization-exempt with its reason in
api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS: it returns only the
caller's own memberships, the same argument /v1/switcher makes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from fastapi import Depends, FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditLog
from api.audit.repository import SqlAuditRepository
from api.auth.mfa import MfaEnrollmentChecker
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.totp_repository import SqlTotpRepository
from api.authz.dependencies import get_authorization_service
from api.authz.service import AuthorizationService
from api.db import get_db_session
from api.firm.switcher import ClientBadge, ClientSwitcher, SwitcherEntry, initials_for
from api.firm.switcher_repository import SqlSwitcherRepository
from api.i18n.formatting import DEFAULT_FORMATTING_LOCALE
from api.i18n.http import problem
from api.ledger import FiscalYear, FiscalYearService, build_fiscal_year_service
from api.tenancy import TenantContext, get_tenant_context


def register(app: FastAPI) -> None:
    app.add_api_route("/v1/me", get_me, methods=["GET"], name="get_me")


async def get_fiscal_year_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> FiscalYearService:
    # `build_fiscal_year_service`, never `api.ledger.fiscal_repository`
    # directly - tests/ledger/test_bounded_context.py fails the build if this
    # module named the repository.
    return build_fiscal_year_service(session, authorization, AuditLog(SqlAuditRepository(session)))


async def get_client_switcher(
    session: AsyncSession = Depends(get_db_session),
) -> ClientSwitcher:
    return ClientSwitcher(SqlSwitcherRepository(session))


# ---------------------------------------------------------------------------
# The administration entry: one shape, produced once, shared with onboarding
# ---------------------------------------------------------------------------


def fiscal_year_json(year: FiscalYear, *, today: date) -> dict[str, object]:
    return {
        "id": str(year.id),
        "start_date": year.start_date.isoformat(),
        "end_date": year.end_date.isoformat(),
        "period_scheme": year.period_scheme.value,
        "status": year.status.value,
        # "Current" is a fact about today's date, not a stored flag: a year
        # becomes current at midnight on its first day without anyone
        # touching a row, and stops being current the same way.
        "is_current": year.start_date <= today <= year.end_date,
    }


def administration_entry_json(
    entry: SwitcherEntry,
    *,
    legal_form: str | None,
    vat_number: str | None,
    formatting_locale: str,
    fiscal_years: Sequence[FiscalYear],
    today: date,
) -> dict[str, object]:
    """The `administrations[]` entry of §4.1, also the body POST
    /v1/administrations answers with. Colour and initials come from the
    badge, the same place api.main._entry_json gets them, so the header,
    the switcher and this bootstrap cannot disagree about what identifies a
    client (FR-FRM-000a).
    """
    return {
        "id": str(entry.badge.administration_id),
        "legal_name": entry.badge.name,
        "trade_name": entry.badge.trade_name,
        "legal_form": legal_form,
        "kvk_number": entry.badge.kvk_number,
        "vat_number": vat_number,
        "formatting_locale": formatting_locale,
        "colour": entry.badge.colour_token,
        "initials": entry.badge.initials,
        "role": entry.role_name,
        # FR-LOC-001: see api.main._entry_json for why this travels with the
        # role name.
        "role_is_system": entry.role_is_system,
        "fiscal_years": [fiscal_year_json(year, today=today) for year in fiscal_years],
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

# A business's administrations, with the caller's own role on each. The
# organization predicate is belt and braces: RLS (ADR-003) already scopes
# `administration` to what this tenant may see, and a firm's engagements would
# otherwise surface client administrations here, which for a business kind
# cannot exist but is not worth relying on. The LATERAL picks the caller's
# most specific live grant reaching the row - an administration-scoped one
# first, else the organization-scoped one that cascades to it (ADR-011).
_OWNED_ADMINISTRATIONS_SQL = """
    SELECT a.id, a.legal_name, a.trade_name, a.legal_form, a.kvk_number, a.vat_number,
           a.formatting_locale, a.colour_token,
           g.role_name, g.role_is_system
    FROM administration a
    LEFT JOIN LATERAL (
        SELECT r.name AS role_name, r.is_system AS role_is_system
        FROM role_assignment ra
        JOIN "role" r ON r.id = ra.role_id
        WHERE ra.user_id = :user_id
          AND ra.revoked_at IS NULL
          AND (ra.expires_at IS NULL OR ra.expires_at > now())
          AND r.archived_at IS NULL
          AND ((ra.scope_type = 'administration' AND ra.scope_id = a.id)
               OR (ra.scope_type = 'organization' AND ra.scope_id = a.organization_id))
        ORDER BY (ra.scope_type = 'administration') DESC, ra.created_at DESC
        LIMIT 1
    ) g ON true
    WHERE a.organization_id = :organization_id
      AND a.status = 'active'
    ORDER BY a.legal_name
"""


class _AdministrationRow:
    """What both branches below reduce to before rendering: the badge plus
    the three columns the badge does not carry."""

    __slots__ = ("entry", "formatting_locale", "legal_form", "vat_number")

    def __init__(
        self,
        entry: SwitcherEntry,
        *,
        legal_form: str | None,
        vat_number: str | None,
        formatting_locale: str,
    ) -> None:
        self.entry = entry
        self.legal_form = legal_form
        self.vat_number = vat_number
        self.formatting_locale = formatting_locale


async def _owned_administrations(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> list[_AdministrationRow]:
    result = await session.execute(
        text(_OWNED_ADMINISTRATIONS_SQL),
        {"organization_id": str(organization_id), "user_id": str(user_id)},
    )
    rows: list[_AdministrationRow] = []
    for row in result:
        badge = ClientBadge(
            administration_id=row.id,
            name=row.legal_name,
            trade_name=row.trade_name,
            kvk_number=row.kvk_number,
            colour_token=row.colour_token,
            initials=initials_for(row.trade_name or row.legal_name),
        )
        rows.append(
            _AdministrationRow(
                SwitcherEntry(
                    badge=badge,
                    # A user with no grant reaching the row has no role to
                    # show. The empty string keeps the field a string for
                    # the client; `role_is_system` False renders it verbatim.
                    role_name=row.role_name or "",
                    role_is_system=bool(row.role_is_system),
                ),
                legal_form=row.legal_form,
                vat_number=row.vat_number,
                formatting_locale=row.formatting_locale,
            )
        )
    return rows


async def _administration_details(
    session: AsyncSession, administration_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[str | None, str | None, str]]:
    """legal_form, vat_number and formatting_locale for the switcher's
    entries, which carry a badge and not the whole row. RLS scopes the read.
    """
    if not administration_ids:
        return {}
    result = await session.execute(
        text(
            "SELECT id, legal_form, vat_number, formatting_locale FROM administration "
            "WHERE id = ANY(cast(:ids as uuid[]))"
        ),
        {"ids": [str(administration_id) for administration_id in administration_ids]},
    )
    return {row.id: (row.legal_form, row.vat_number, row.formatting_locale) for row in result}


async def _active_administration_id(
    session: AsyncSession, tenant: TenantContext
) -> uuid.UUID | None:
    """The session row is the source of truth (0017): it is what the switcher
    and onboarding write. The token's own `adm` claim is the fallback for a
    request that carries no `sid` at all. `sessions` has no RLS (users are
    global, 0003), so the predicate names the user as well as the session -
    a session id that reached the wrong hands must not read somebody else's.
    """
    if tenant.session_id is None or tenant.user_id is None:
        return tenant.active_administration_id
    result = await session.execute(
        text(
            "SELECT active_administration_id FROM sessions "
            "WHERE id = :session_id AND user_id = :user_id AND revoked_at IS NULL"
        ),
        {"session_id": str(tenant.session_id), "user_id": str(tenant.user_id)},
    )
    row = result.first()
    if row is None:
        return tenant.active_administration_id
    active: uuid.UUID | None = row.active_administration_id
    return active


async def get_me(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    switcher: ClientSwitcher = Depends(get_client_switcher),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    # `users` carries no RLS (users are global, 0003), so this query names
    # its own predicate, and the id comes from the verified token - the same
    # posture /v1/me/language takes.
    user_row = (
        await session.execute(
            text("SELECT id, email, language, email_verified_at FROM users WHERE id = :user_id"),
            {"user_id": str(tenant.user_id)},
        )
    ).first()
    if user_row is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    # organization_select (0001) shows a tenant its own row; the predicate is
    # here so a firm - which can also see engaged clients' organizations -
    # reads its own and not the first one the planner returns.
    organization_row = (
        await session.execute(
            text("SELECT id, name, kind, kvk_number FROM organization WHERE id = :id"),
            {"id": str(tenant.organization_id)},
        )
    ).first()
    if organization_row is None:
        # Structurally unreachable: the token's org_id was verified and the
        # session is scoped to it. Fails closed rather than assuming.
        raise problem(request, 403, "errors.not_authenticated", reason="no_organization")

    if organization_row.kind == "firm":
        # FR-FRM-000: the switcher's own list - the client administrations
        # this firm user holds a live grant on, and nothing wider.
        entries = await switcher.list(tenant.user_id)
        details = await _administration_details(
            session, [entry.badge.administration_id for entry in entries]
        )
        rows = []
        for entry in entries:
            legal_form, vat_number, formatting_locale = details.get(
                entry.badge.administration_id, (None, None, DEFAULT_FORMATTING_LOCALE)
            )
            rows.append(
                _AdministrationRow(
                    entry,
                    legal_form=legal_form,
                    vat_number=vat_number,
                    formatting_locale=formatting_locale,
                )
            )
    else:
        rows = await _owned_administrations(
            session, organization_id=tenant.organization_id, user_id=tenant.user_id
        )

    today = date.today()
    administrations: list[dict[str, object]] = []
    for row in rows:
        # A row with no role reaching it has no permission to read its years
        # with; asking would be answered with a denial and an audit entry
        # about a person who did nothing wrong.
        years: Sequence[FiscalYear] = (
            await fiscal.visible_years(
                administration_id=row.entry.badge.administration_id,
                actor_user_id=tenant.user_id,
            )
            if row.entry.role_name
            else ()
        )
        administrations.append(
            administration_entry_json(
                row.entry,
                legal_form=row.legal_form,
                vat_number=row.vat_number,
                formatting_locale=row.formatting_locale,
                fiscal_years=years,
                today=today,
            )
        )

    mfa = await MfaEnrollmentChecker(
        SqlPasskeyRepository(session), SqlTotpRepository(session)
    ).check(tenant.user_id)
    active = await _active_administration_id(session, tenant)

    return {
        "user": {
            "id": str(user_row.id),
            "email": user_row.email,
            "language": user_row.language,
            # IAM-010b (migration 0049, ADR-060): false until the link in the
            # verification mail is opened - the client shows its banner on
            # this, and posting refuses on it (errors.email_not_verified).
            "email_verified": user_row.email_verified_at is not None,
        },
        "organization": {
            "id": str(organization_row.id),
            "name": organization_row.name,
            "kind": organization_row.kind,
            "kvk_number": organization_row.kvk_number,
        },
        "administrations": administrations,
        "active_administration_id": str(active) if active is not None else None,
        "mfa": {"has_totp": mfa.has_totp, "has_passkey": mfa.has_passkey},
        "onboarding": {"needs_administration": not administrations},
    }

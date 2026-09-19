"""Onboarding's HTTP surface (docs/founder-review-2026-09-14.md §4.2).

    POST  /v1/administrations                          create, seed, open, switch
    GET   /v1/fiscal-years/preview                     FR-ONB-006's period preview
    GET   /v1/administrations/{id}/fiscal-years        the years an administration has
    POST  /v1/administrations/{id}/fiscal-years        open a further year
    PATCH /v1/administrations/{id}                     legal name, trade name, VAT, locale

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register`'s docstring for why.

--- One transaction ---

`POST /v1/administrations` does four things and they commit together or not
at all: the administration row, the chart of accounts (FR-ONB-005, through
`ChartOfAccountsService.seed`), the first fiscal year (FR-ONB-006, through
`FiscalYearService.open_year`) and the session's active administration
(through `EngagementRevocationService.switch_to`, the same write PUT
/v1/switcher/{id} makes). An administration with no chart is one nothing can
be posted into, and one with no fiscal year is one no posting can be dated
in - so a half-created administration is not a lesser administration, it is
a support ticket, and the transaction boundary is what makes it impossible.

--- Two account models, one route ---

A self-managed business (Model B) creates its own administration with a
plain INSERT under its own tenant context; RLS's administration_insert_own
policy is what makes `organization_id` the caller's own. A firm (Model A)
creates a CLIENT administration through `app.create_firm_client_administration`
(migration 0001), which creates the client organization, the administration
and an already-active engagement atomically - and then the creating firm user
is granted Accountant on the new administration in the same transaction. See
ADR-059 for why that founding grant is inserted directly rather than through
`FirmStaffAccessService`, and for why the seeding and year-opening steps then
run under the NEW client's tenant context.

--- Permissions ---

Appendix A's "Create/delete administrations" - ("manage", "administration"),
organization-scoped - is exactly what creating one is, and the Firm Manager
holds it too (§8.4: "client onboarding"). The same permission gates PATCH,
the closest fit the matrix has for editing an administration's identity; ADR-059
records the gap that leaves for firm staff. Listing and opening fiscal years
ride on the permissions `FiscalYearService` already evaluates.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.account.routes import (
    administration_entry_json,
    fiscal_year_json,
    get_client_switcher,
    get_fiscal_year_service,
)
from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.audit.trail import AuditTrail
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
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
from api.authz.service import AuthorizationService
from api.crypto.envelope import EnvelopeEncryptionService, generate_wrapped_dek
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import get_db_session
from api.firm.switcher import ClientSwitcher, SwitcherEntry
from api.i18n.formatting import DEFAULT_FORMATTING_LOCALE, LOCALES
from api.i18n.http import problem
from api.iban import parse as parse_iban
from api.iban import parse_creditor_id
from api.ledger import (
    ChartError,
    ChartNotAuthorized,
    ChartOfAccountsService,
    FiscalYear,
    FiscalYearService,
    InvalidFiscalYear,
    NotAuthorizedToDefineYear,
    PeriodScheme,
    SeedResult,
)
from api.ledger.chart import build_chart_service
from api.tenancy import TenantContext, get_tenant_context

#: The role the creating firm user is granted on a client administration it
#: just created. Accountant, because seeding a chart and opening a fiscal year
#: ARE bookkeeping - "Maintain the chart of accounts" and "Year-end close" are
#: its Appendix A capabilities - and because §8.4's "cannot post to a client's
#: ledger without also holding Accountant on it" is satisfied by exactly this
#: shape: an explicit, visible, administration-scoped grant. See ADR-059.
FOUNDING_FIRM_ROLE = "Accountant"

#: The five forms FR-ONB-004 names, spelled as a person would type them. The
#: database's `legal_form_alias` accepts more (KvK vocabulary, abbreviations);
#: this is what the error names when none of them matched.
ACCEPTED_LEGAL_FORMS: tuple[str, ...] = ("eenmanszaak", "vof", "bv", "stichting", "vereniging")


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/administrations",
        create_administration,
        methods=["POST"],
        name="create_administration",
    )
    app.add_api_route(
        "/v1/fiscal-years/preview",
        preview_fiscal_year,
        methods=["GET"],
        name="preview_fiscal_year",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/fiscal-years",
        list_fiscal_years,
        methods=["GET"],
        name="list_fiscal_years",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/fiscal-years",
        open_fiscal_year,
        methods=["POST"],
        name="open_fiscal_year",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}",
        update_administration,
        methods=["PATCH"],
        name="update_administration",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def get_chart_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ChartOfAccountsService:
    # `build_chart_service`, never `api.ledger.chart_repository` - CLAUDE.md's
    # first non-negotiable; tests/ledger/test_bounded_context.py enforces it.
    return build_chart_service(session, authorization, AuditLog(SqlAuditRepository(session)))


async def get_engagement_service(
    session: AsyncSession = Depends(get_db_session),
) -> EngagementRevocationService:
    return EngagementRevocationService(SqlEngagementRevocationRepository(session))


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class FiscalYearBody(BaseModel):
    start_date: date
    end_date: date
    period_scheme: Literal["monthly", "quarterly"] = "monthly"


class CreateAdministrationBody(BaseModel):
    legal_name: str
    trade_name: str | None = None
    legal_form: str
    kvk_number: str | None = None
    vat_number: str | None = None
    formatting_locale: str = DEFAULT_FORMATTING_LOCALE
    # Optional on purpose: FR-ONB-006's default is the calendar year the
    # business is being set up in, monthly - which is what nearly every Dutch
    # SMB has. A non-calendar or short first year is stated; the common case
    # is not.
    fiscal_year: FiscalYearBody | None = None


class UpdateAdministrationBody(BaseModel):
    legal_name: str | None = None
    trade_name: str | None = None
    vat_number: str | None = None
    formatting_locale: str | None = None
    #: SI-02. Validated (format AND mod-97 checksum - api.invoicing.iban) and
    #: normalised to its compact upper-case form before being stored. An
    #: empty string clears it, same convention as trade_name.
    iban: str | None = None
    #: SI-09. The SEPA creditor identifier (Incassant-ID); format AND check digits are
    #: validated (api.invoicing.sepa.parse_creditor_id). An empty string clears it.
    sepa_creditor_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_fiscal_year(today: date) -> FiscalYearBody:
    return FiscalYearBody(
        start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31),
        period_scheme="monthly",
    )


def _period_json(periods: Sequence[Any]) -> list[dict[str, object]]:
    return [
        {
            "period_number": period.period_number,
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "days": period.days,
            "is_stub": period.is_stub,
        }
        for period in periods
    ]


async def _canonical_legal_form(session: AsyncSession, raw: str) -> str | None:
    """The same `legal_form_alias` lookup `ledger.seed_chart_of_accounts`
    performs (migration 0024), asked BEFORE anything is written: a spelling
    the seed cannot place would otherwise fail three statements in, with a
    database error where a validation message belongs.
    """
    result = await session.execute(
        text("SELECT app.canonical_legal_form(:raw) AS legal_form"), {"raw": raw}
    )
    row = result.first()
    return None if row is None else row.legal_form


def _require_formatting_locale(request: Request, locale: str) -> str:
    """FR-LOC-002. Mirrors the CHECK constraint 0030 puts on the column, so
    the refusal is a sentence naming the supported set rather than a
    constraint violation.
    """
    if locale not in LOCALES:
        raise problem(
            request,
            422,
            "errors.formatting_locale_unsupported",
            reason="unsupported_formatting_locale",
            supported=", ".join(sorted(LOCALES)),
        )
    return locale


def _validate_year(request: Request, fiscal: FiscalYearService, body: FiscalYearBody) -> None:
    try:
        fiscal.preview(
            start_date=body.start_date,
            end_date=body.end_date,
            scheme=PeriodScheme(body.period_scheme),
        )
    except InvalidFiscalYear as exc:
        raise problem(
            request, 422, "errors.fiscal_year_invalid", reason="invalid_fiscal_year"
        ) from exc


async def _organization_kind(session: AsyncSession, organization_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT kind FROM organization WHERE id = :id"), {"id": str(organization_id)}
    )
    row = result.first()
    if row is None:
        # The token's organization was verified by the middleware and the
        # session is scoped to it; a missing row is a structural fault, not a
        # request error.
        raise RuntimeError(f"organization {organization_id} is not visible to its own session")
    return str(row.kind)


async def _set_tenant_context(session: AsyncSession, organization_id: uuid.UUID) -> None:
    # is_local=true: transaction-scoped, exactly as api.db.get_db_session and
    # api.auth.signup.SignupService set it. Nothing outlives this request.
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(organization_id)},
    )


async def _create_business_administration(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    body: CreateAdministrationBody,
    fiscal_year: FiscalYearBody,
) -> uuid.UUID:
    # No SECURITY DEFINER needed: administration_insert_own (0001) admits an
    # INSERT whose organization_id is the session's own, and that is the only
    # organization this branch ever names.
    result = await session.execute(
        text(
            "INSERT INTO administration "
            "(organization_id, legal_name, trade_name, legal_form, kvk_number, vat_number, "
            " formatting_locale, fiscal_year_start_month) "
            "VALUES (:organization_id, :legal_name, :trade_name, :legal_form, :kvk_number, "
            "        :vat_number, :formatting_locale, :start_month) "
            "RETURNING id"
        ),
        {
            "organization_id": str(organization_id),
            "legal_name": body.legal_name.strip(),
            "trade_name": body.trade_name.strip() if body.trade_name else None,
            "legal_form": body.legal_form.strip(),
            "kvk_number": body.kvk_number.strip() if body.kvk_number else None,
            "vat_number": body.vat_number.strip() if body.vat_number else None,
            "formatting_locale": body.formatting_locale,
            "start_month": fiscal_year.start_date.month,
        },
    )
    administration_id: uuid.UUID = result.scalar_one()

    # SEC-022/ADR-004, the half the firm branch gets from its bootstrap
    # function: an administration with no data-encryption key cannot store a
    # document or a template asset at all — `EnvelopeEncryptionService.encrypt`
    # raises "has no active encryption key" — so the key is provisioned in the
    # same transaction as the administration rather than lazily on first
    # upload. `insert_active` reads the owning organization from the row just
    # inserted, which is this session's own tenant.
    await EnvelopeEncryptionService(
        build_kms(), SqlAdministrationKeyRepository(session)
    ).provision_key(administration_id)
    return administration_id


async def _create_client_administration(
    session: AsyncSession,
    *,
    creator_user_id: uuid.UUID,
    body: CreateAdministrationBody,
    fiscal_year: FiscalYearBody,
) -> tuple[uuid.UUID, uuid.UUID]:
    """FR-MDL-004: the client organization, its administration and an
    already-active engagement, in one SECURITY DEFINER call whose firm is
    read from the session's own tenant context and cannot be spoofed.
    Returns (administration_id, client_organization_id).
    """
    # SEC-022/ADR-004: the client's data-encryption key is created in the SAME
    # statement as the administration itself. Migration 0002 widened this
    # function's signature to take the wrapped material for exactly that
    # reason, so there is no instant at which a client administration exists
    # without a key — which is why this passes eight arguments and not 0001's
    # five. The raw DEK never leaves `generate_wrapped_dek`.
    wrapped = await generate_wrapped_dek(build_kms())
    result = await session.execute(
        text(
            "SELECT id, organization_id FROM app.create_firm_client_administration("
            "  :legal_name, :legal_form, :kvk_number, :vat_number, :invited_by,"
            "  :wrapped_dek, :wrap_algorithm, :kek_key_id)"
        ),
        {
            "legal_name": body.legal_name.strip(),
            "legal_form": body.legal_form.strip(),
            "kvk_number": body.kvk_number.strip() if body.kvk_number else None,
            "vat_number": body.vat_number.strip() if body.vat_number else None,
            "invited_by": str(creator_user_id),
            "wrapped_dek": wrapped.ciphertext,
            "wrap_algorithm": wrapped.algorithm,
            "kek_key_id": wrapped.kek_key_id,
        },
    )
    row = result.one()
    # The bootstrap function's signature is 0001's and takes no trade name or
    # locale; the engagement it just created is what admits this UPDATE
    # (administration_update, "ownership or active engagement").
    await session.execute(
        text(
            "UPDATE administration SET trade_name = :trade_name, "
            "  formatting_locale = :formatting_locale, fiscal_year_start_month = :start_month "
            "WHERE id = :id"
        ),
        {
            "trade_name": body.trade_name.strip() if body.trade_name else None,
            "formatting_locale": body.formatting_locale,
            "start_month": fiscal_year.start_date.month,
            "id": str(row.id),
        },
    )
    return row.id, row.organization_id


async def _grant_founding_firm_role(
    session: AsyncSession, *, user_id: uuid.UUID, administration_id: uuid.UUID
) -> None:
    """The founding grant, inserted directly - the precedent is
    `SignupService._grant_founding_owner` (ADR-054), and the reasoning is in
    ADR-059. Runs under the FIRM's tenant context on purpose:
    role_assignment_firm_staff_guard_trg (0016) derives
    granted_by_organization_id from it, which is what records this as a firm
    staff grant the client can see (IAM-109) and revoke (IAM-110), and the
    same trigger verifies the active engagement the bootstrap function just
    created (IAM-107).
    """
    result = await session.execute(
        text(
            "INSERT INTO role_assignment "
            "(user_id, role_id, scope_type, scope_id, granted_by_user_id) "
            "SELECT :user_id, r.id, 'administration', :administration_id, :user_id "
            'FROM "role" r WHERE r.is_system AND r.name = :role_name '
            "RETURNING id"
        ),
        {
            "user_id": str(user_id),
            "administration_id": str(administration_id),
            "role_name": FOUNDING_FIRM_ROLE,
        },
    )
    if result.first() is None:
        raise RuntimeError(
            f"the system role {FOUNDING_FIRM_ROLE!r} does not exist; the role catalogue "
            "(migration 0010) has not been applied"
        )


async def _switch_session(
    request: Request,
    tenant: TenantContext,
    engagement: EngagementRevocationService,
    *,
    administration_id: uuid.UUID,
) -> None:
    """The side effect that makes the next screen the new administration.

    A request with no session identifier (a non-interactive API client) has
    no session to move, and that is not an error here the way it is for the
    switcher: the administration was asked for and exists; `GET /v1/me`
    reports `active_administration_id` honestly either way.
    """
    if tenant.session_id is None or tenant.user_id is None:
        return
    try:
        await engagement.switch_to(
            session_id=tenant.session_id,
            user_id=tenant.user_id,
            administration_id=administration_id,
        )
    except NoSessionError as exc:
        raise problem(request, 409, "errors.no_session", reason="no_session") from exc
    except NoSuchEngagementError as exc:
        # The grant that admits the creator was written in this very
        # transaction (the Owner's organization grant, or the founding firm
        # grant above), so the switcher not offering the administration means
        # a repository and a guard disagree - a bug to surface, not a state to
        # answer for.
        raise RuntimeError(
            f"administration {administration_id} was created but is not in its creator's switcher"
        ) from exc


async def _require_entry(
    switcher: ClientSwitcher, *, user_id: uuid.UUID, administration_id: uuid.UUID
) -> SwitcherEntry:
    entry = await switcher.find(user_id, administration_id)
    if entry is None:
        raise RuntimeError(
            f"administration {administration_id} was created but its creator holds no "
            "grant reaching it"
        )
    return entry


def _not_permitted(
    request: Request, exc: ChartNotAuthorized | NotAuthorizedToDefineYear
) -> Exception:
    return problem(
        request,
        403,
        "errors.not_permitted",
        reason="no_matching_grant",
        action=exc.action,
        resource_type=exc.resource_type,
        detail=exc.detail,
    )


async def _seed_and_open(
    request: Request,
    *,
    chart: ChartOfAccountsService,
    fiscal: FiscalYearService,
    administration_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    fiscal_year: FiscalYearBody,
) -> tuple[SeedResult, FiscalYear]:
    try:
        seeded = await chart.seed(administration_id=administration_id, actor_user_id=actor_user_id)
    except ChartNotAuthorized as exc:
        raise _not_permitted(request, exc) from exc
    except ChartError as exc:
        # No current RGS version loaded, or the legal form has no profile.
        # Both are the platform's problem, not the caller's: a 503 says "not
        # now" rather than sending them to correct data that is correct.
        raise problem(
            request, 503, "errors.chart_seed_unavailable", reason="chart_seed_unavailable"
        ) from exc
    try:
        year = await fiscal.open_year(
            administration_id=administration_id,
            actor_user_id=actor_user_id,
            start_date=fiscal_year.start_date,
            end_date=fiscal_year.end_date,
            scheme=PeriodScheme(fiscal_year.period_scheme),
        )
    except NotAuthorizedToDefineYear as exc:
        raise _not_permitted(request, exc) from exc
    except InvalidFiscalYear as exc:
        raise problem(
            request, 422, "errors.fiscal_year_invalid", reason="invalid_fiscal_year"
        ) from exc
    return seeded, year


# ---------------------------------------------------------------------------
# POST /v1/administrations
# ---------------------------------------------------------------------------


async def create_administration(
    request: Request,
    body: CreateAdministrationBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    chart: ChartOfAccountsService = Depends(get_chart_service),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    engagement: EngagementRevocationService = Depends(get_engagement_service),
    switcher: ClientSwitcher = Depends(get_client_switcher),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "administration",
            # Organization-scoped: creating an administration is an act at
            # the organization, and there is no administration in the path to
            # scope to yet.
            scope=organization_scope(),
            # IAM-090's "configuration changes": every later figure in these
            # books is expressed in the chart and dated in the year this
            # request creates.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    if not body.legal_name.strip():
        raise problem(request, 422, "errors.legal_name_required", reason="legal_name_required")

    # Validated before anything is written, in the order a person would fix
    # them: the form, the locale, the year.
    if await _canonical_legal_form(session, body.legal_form) is None:
        raise problem(
            request,
            422,
            "errors.legal_form_unknown",
            reason="unknown_legal_form",
            legal_form=body.legal_form,
            accepted=", ".join(ACCEPTED_LEGAL_FORMS),
        )
    _require_formatting_locale(request, body.formatting_locale)
    fiscal_year = body.fiscal_year or _default_fiscal_year(date.today())
    _validate_year(request, fiscal, fiscal_year)

    kind = await _organization_kind(session, tenant.organization_id)

    if kind == "firm":
        administration_id, client_organization_id = await _create_client_administration(
            session, creator_user_id=tenant.user_id, body=body, fiscal_year=fiscal_year
        )
        await _grant_founding_firm_role(
            session, user_id=tenant.user_id, administration_id=administration_id
        )
        # From here to the matching reset, the transaction runs as the client
        # tenant this request just created. ADR-059 explains why: the chart
        # and fiscal-year services file their audit entries under the
        # administration's OWNING organization (that is whose log "my
        # accountant seeded my chart" belongs in, IAM-094), and
        # audit_log_insert (0019) admits a row only for the session's own
        # tenant. The same bootstrap move SignupService makes for a
        # self-managed organization, for the same reason.
        await _set_tenant_context(session, client_organization_id)
        await AuditTrail(AuditLog(SqlAuditRepository(session))).permission_change(
            organization_id=client_organization_id,
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            action="grant",
            resource_type="firm_staff_access",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "firm_organization_id": str(tenant.organization_id),
                "staff_user_id": str(tenant.user_id),
                "role": FOUNDING_FIRM_ROLE,
                "founding": True,
            },
        )
        try:
            seeded, _year = await _seed_and_open(
                request,
                chart=chart,
                fiscal=fiscal,
                administration_id=administration_id,
                actor_user_id=tenant.user_id,
                fiscal_year=fiscal_year,
            )
        finally:
            await _set_tenant_context(session, tenant.organization_id)
    else:
        administration_id = await _create_business_administration(
            session,
            organization_id=tenant.organization_id,
            body=body,
            fiscal_year=fiscal_year,
        )
        seeded, _year = await _seed_and_open(
            request,
            chart=chart,
            fiscal=fiscal,
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            fiscal_year=fiscal_year,
        )

    await _switch_session(request, tenant, engagement, administration_id=administration_id)

    entry = await _require_entry(
        switcher, user_id=tenant.user_id, administration_id=administration_id
    )
    years = await fiscal.visible_years(
        administration_id=administration_id, actor_user_id=tenant.user_id
    )
    return {
        **administration_entry_json(
            entry,
            legal_form=body.legal_form.strip(),
            vat_number=body.vat_number.strip() if body.vat_number else None,
            formatting_locale=body.formatting_locale,
            fiscal_years=years,
            today=date.today(),
        ),
        "chart": {"seeded": seeded.seeded_count, "rgs_version": seeded.rgs_version},
    }


# ---------------------------------------------------------------------------
# Fiscal years
# ---------------------------------------------------------------------------


async def preview_fiscal_year(
    request: Request,
    start_date: date,
    end_date: date,
    period_scheme: Literal["monthly", "quarterly"] = "monthly",
    tenant: TenantContext = Depends(get_tenant_context),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
) -> dict[str, object]:
    """FR-ONB-006: what opening this year would produce, before it exists.

    Exempt from the permission check with its reason in
    api.authz.dependencies: arithmetic on two dates the caller typed, naming
    no administration and touching no table.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        periods = fiscal.preview(
            start_date=start_date, end_date=end_date, scheme=PeriodScheme(period_scheme)
        )
    except InvalidFiscalYear as exc:
        raise problem(
            request, 422, "errors.fiscal_year_invalid", reason="invalid_fiscal_year"
        ) from exc
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "period_scheme": period_scheme,
        "periods": _period_json(periods),
    }


async def list_fiscal_years(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "administration",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> list[dict[str, object]]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        years = await fiscal.visible_years(
            administration_id=administration_id, actor_user_id=tenant.user_id
        )
    except NotAuthorizedToDefineYear as exc:
        raise _not_permitted(request, exc) from exc
    today = date.today()
    return [fiscal_year_json(year, today=today) for year in years]


async def open_fiscal_year(
    administration_id: uuid.UUID,
    request: Request,
    body: FiscalYearBody,
    tenant: TenantContext = Depends(get_tenant_context),
    fiscal: FiscalYearService = Depends(get_fiscal_year_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # The permission FiscalYearService.open_year itself evaluates
            # (MANAGE_FISCAL_YEAR); declared here too so the route table says
            # what the service will say, and the audit category rides on it.
            "close",
            "fiscal_year",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    _validate_year(request, fiscal, body)
    try:
        year = await fiscal.open_year(
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            start_date=body.start_date,
            end_date=body.end_date,
            scheme=PeriodScheme(body.period_scheme),
        )
    except NotAuthorizedToDefineYear as exc:
        raise _not_permitted(request, exc) from exc
    except InvalidFiscalYear as exc:
        raise problem(
            request, 422, "errors.fiscal_year_invalid", reason="invalid_fiscal_year"
        ) from exc
    except IntegrityError as exc:
        # fiscal_year_no_overlap (0029): the only integrity rule a validated
        # year can still break is sitting on top of an existing one.
        raise problem(
            request, 409, "errors.fiscal_year_overlaps", reason="fiscal_year_overlaps"
        ) from exc
    return fiscal_year_json(year, today=date.today())


# ---------------------------------------------------------------------------
# PATCH /v1/administrations/{id}
# ---------------------------------------------------------------------------


async def update_administration(
    administration_id: uuid.UUID,
    request: Request,
    body: UpdateAdministrationBody,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "administration",
            # Organization-scoped, exactly as the POST above is, and NOT
            # administration_from_path: Appendix A grades "Create/delete
            # administrations" at the organization, and api.authz.service
            # refuses an organization-level capability aimed at a single
            # administration outright (reason: resource_scope_mismatch) rather
            # than promoting the target to its owner, which IAM-032 forbids.
            # Requesting it per-administration therefore denied EVERY caller,
            # including the owner of the books. Which administration this
            # request may actually touch is RLS's answer (administration_update
            # in 0001: the organization that owns it, or a firm with an active
            # engagement), not this check's - the same division of labour
            # api.main's /v1/administrations listing already uses. See ADR-059.
            scope=organization_scope(),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Legal name, trade name, VAT number, formatting locale - the fields a
    person corrects after onboarding. Only the fields sent are touched: a
    body naming `trade_name: null` clears it, a body omitting it leaves it.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    changes = body.model_dump(exclude_unset=True)
    if "legal_name" in changes and not (changes["legal_name"] or "").strip():
        raise problem(request, 422, "errors.legal_name_required", reason="legal_name_required")
    if "formatting_locale" in changes:
        _require_formatting_locale(request, changes["formatting_locale"] or "")
    if "iban" in changes and (changes["iban"] or "").strip():
        # SI-02. Checked HERE, at write time - not just format but the
        # mod-97 checksum, because this value later goes onto every invoice
        # PDF as a QR code a customer's banking app pays automatically. A
        # bad IBAN reaching that point is a payment sent to a stranger, not
        # a red squiggle under a form field. Stored in its canonical compact
        # form, never the sender's own spacing/casing.
        parsed_iban = parse_iban(changes["iban"])
        if parsed_iban is None:
            raise problem(request, 422, "errors.iban_invalid", reason="iban_invalid", field="iban")
        changes["iban"] = parsed_iban.value
    if "sepa_creditor_id" in changes and (changes["sepa_creditor_id"] or "").strip():
        # SI-09. A wrong creditor id gets every direct debit file refused by the bank, so it is
        # checked here, at write time, and stored in its compact upper-case form.
        creditor_id = parse_creditor_id(changes["sepa_creditor_id"])
        if creditor_id is None:
            raise problem(
                request,
                422,
                "errors.sepa_creditor_id_invalid",
                reason="sepa_creditor_id_invalid",
                field="sepa_creditor_id",
            )
        changes["sepa_creditor_id"] = creditor_id

    assignments = {
        "legal_name": "legal_name = :legal_name",
        "trade_name": "trade_name = :trade_name",
        "vat_number": "vat_number = :vat_number",
        "formatting_locale": "formatting_locale = :formatting_locale",
        "iban": "iban = :iban",
        "sepa_creditor_id": "sepa_creditor_id = :sepa_creditor_id",
    }
    set_clause = ", ".join(assignments[name] for name in changes)
    params: dict[str, object] = {"id": str(administration_id)}
    for name, value in changes.items():
        if name in ("iban", "sepa_creditor_id"):
            # Already normalised above (or explicitly cleared) - stripping
            # again would be harmless but re-deriving the same value twice
            # invites the two derivations drifting apart.
            params[name] = value if value else None
        else:
            params[name] = value.strip() if isinstance(value, str) else value

    if set_clause:
        # administration_update (0001) is the tenant boundary; the permission
        # above is the capability. RLS filtering the row out leaves nothing
        # updated, which the SELECT below reports as not found.
        await session.execute(
            text(f"UPDATE administration SET {set_clause}, updated_at = now() WHERE id = :id"),
            params,
        )

    row = (
        await session.execute(
            text(
                "SELECT id, legal_name, trade_name, legal_form, kvk_number, vat_number, "
                "       formatting_locale, iban, sepa_creditor_id "
                "  FROM administration WHERE id = :id"
            ),
            {"id": str(administration_id)},
        )
    ).first()
    if row is None:
        raise problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        )
    return {
        "id": str(row.id),
        "legal_name": row.legal_name,
        "trade_name": row.trade_name,
        "legal_form": row.legal_form,
        "kvk_number": row.kvk_number,
        "vat_number": row.vat_number,
        "formatting_locale": row.formatting_locale,
        "iban": row.iban,
        "sepa_creditor_id": row.sepa_creditor_id,
    }

"""The mobile home screen's HTTP surface: FR-UX-005, MOB-006.

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register`'s docstring for why: this FastAPI version
hides an included router's routes behind a wrapper the authorization
middleware and the coverage checks walk straight past.

--- Permission: "View reports", not a new one ---

Appendix A has two candidates for "a financial summary dashboard": "View
reports" (F for Owner/Accountant/Bookkeeper, C for Approver, R for Viewer) and
"Prepare VAT return" (F for Owner/Accountant/Bookkeeper only). A dashboard is a
report - it is read-only and asserts nothing to the tax authority - not a VAT
filing action, so "View reports" is the fit, and it is also the wider grant:
"Prepare VAT return" would deny an Approver or a Viewer who has every business
reason to see cash position and receivables. Reusing `("view", "report")`
exactly is ADR-012's rule: no permission is invented for this endpoint.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.dashboard.model import ActionItem, ActionItemKind, DashboardSummary
from api.dashboard.repository import DashboardRepository
from api.dashboard.service import DashboardService, FiscalYearNotFound
from api.db import get_db_session
from api.expenses.repository import SqlCaptureRepository
from api.i18n.catalogue import translate
from api.i18n.http import problem, request_language
from api.i18n.language import Language
from api.invoicing.repository import SqlInvoiceRepository
from api.ledger.chart import build_chart_service
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context


def register(app: FastAPI) -> None:
    app.add_api_route(
        "/v1/administrations/{administration_id}/dashboard",
        get_dashboard,
        methods=["GET"],
        name="get_dashboard",
    )


async def get_dashboard_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> DashboardService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return DashboardService(
        # `build_ledger_service`/`build_chart_service`, never `api.ledger.
        # repository`/`api.ledger.chart_repository` directly - CLAUDE.md's
        # first non-negotiable; tests/ledger/test_bounded_context.py fails the
        # build if this module named either repository instead.
        ledger=build_ledger_service(session, audit_log),
        chart=build_chart_service(session, authorization, audit_log),
        invoices=SqlInvoiceRepository(session),
        expenses=SqlCaptureRepository(session),
        dashboard=DashboardRepository(session),
    )


def _action_item_json(item: ActionItem, language: Language) -> dict[str, object]:
    """`description` is built HERE, at the HTTP boundary, from the item's raw
    facts - the same split `_view_json` makes for `statutory_failures` via
    `describe()`. The pure aggregator in `api.dashboard.model` never touches
    language.
    """
    if item.kind is ActionItemKind.OVERDUE_INVOICE:
        description = translate(
            "mobile.home.item.overdue_invoice",
            language,
            reference=item.invoice_reference,
            customer=item.customer_name,
            count=item.days_overdue,
        )
    elif item.kind is ActionItemKind.DRAFT_EXPENSE:
        description = (
            translate("mobile.home.item.draft_expense", language, supplier=item.supplier)
            if item.supplier
            else translate("mobile.home.item.draft_expense_untitled", language)
        )
    else:
        description = translate(
            "mobile.home.item.draft_invoice", language, customer=item.customer_name
        )

    return {
        # FR-UX-005's routing hint: enough for a client to switch to the right
        # tab. Neither `ApproveList` nor `ViewList` supports opening directly
        # to one record today, so a tap switches tabs only - see this
        # feature's ADR, Known gaps.
        "kind": item.kind.value,
        "id": str(item.id),
        "description": description,
    }


def _dashboard_json(summary: DashboardSummary, language: Language) -> dict[str, object]:
    return {
        # NFR-031: every amount is a Decimal-shaped STRING on the wire, the
        # same convention `api.customers.routes`'s `credit_limit` uses.
        "cash_position": str(summary.cash_position),
        "receivables": str(summary.receivables),
        "vat_estimate": str(summary.vat_estimate),
        "vat_period_start": summary.vat_period_start.isoformat(),
        "vat_period_end": summary.vat_period_end.isoformat(),
        "items_needing_action": [
            _action_item_json(item, language) for item in summary.items_needing_action
        ],
    }


async def get_dashboard(
    administration_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: DashboardService = Depends(get_dashboard_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view",
            "report",
            scope=administration_from_path("administration_id"),
            # IAM-090's "data reads of financial records" - a GET may declare
            # a category rather than must (tests/test_audit_coverage.py), and
            # this one reads exactly that.
            audit=AuditCategory.FINANCIAL_READ,
        )
    ),
) -> dict[str, object]:
    """FR-UX-005 / MOB-006: the prioritised home-screen summary.

    `fiscal_year_id` is a required query parameter, the same shape `ledger.
    trial_balance()` already takes it - this dashboard has no opinion about
    which year is "current" (there is no session/active-fiscal-year concept
    yet, the same gap `MobileShell`'s own props already document) and asks the
    caller to say.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    try:
        summary = await service.summary(
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            actor_user_id=tenant.user_id,
            today=date.today(),
        )
    except FiscalYearNotFound as exc:
        raise problem(
            request, 404, "errors.fiscal_year_not_found", reason="fiscal_year_not_found"
        ) from exc

    return _dashboard_json(summary, request_language(request))

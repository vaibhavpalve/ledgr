"""The auto-bookings review's HTTP surface (FR-BNK-004, FR-FRM-002, ADR-110).

    GET  /v1/firm/proposals?administration_ids=a,b     pending proposals, grouped, across clients
    POST /v1/firm/proposals/decide                      approve / reject in bulk
    GET  /v1/administrations/{id}/proposals             the same groups, one client

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

--- Permissions: none new ---

Reads take "View bank transactions" (`view bank_transaction`), decisions "Reconcile bank"
(`reconcile bank_transaction`) - approving a proposal IS reconciling the line, through the same
service call the Bank screen makes.

The two /v1/firm routes name no administration, so they declare
`require_portfolio_permission` (api.firm.worklist_access, ADR-109) - the caller's live grants,
then `authorize()` per administration - instead of an administration-scoped `require_permission`.
The listing reads only that portfolio. `decide` refuses any proposal outside it and still calls
`authorize()` for every proposal, against that proposal's own administration and current state,
before anything changes (ADR-110).

--- The active-client guard (FR-FRM-000a) ---

`decide` is cross-client by design, like the switcher's own `allows_cross_client`: the review
sheet names the client on every row and each decision names one proposal, so there is no "header
says one client, request writes to another" to refuse. The per-client route is a read.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory, AuditLog
from api.audit.repository import SqlAuditRepository
from api.auth.email_verification import require_verified_email
from api.authz.dependencies import (
    administration_from_path,
    get_authorization_service,
    require_permission,
)
from api.authz.model import AuthorizationDecision
from api.authz.service import AuthorizationService
from api.bank.compose import build_bank_service
from api.db import get_db_session
from api.firm.proposals_repository import SqlProposalRepository
from api.firm.proposals_service import (
    MAX_DECISIONS,
    DecideOutcome,
    Decision,
    DecisionRequest,
    ProposalGroup,
    ProposalReviewService,
)
from api.firm.worklist_access import Portfolio, require_portfolio_permission
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

FIRM_PROPOSALS = "/v1/firm/proposals"
FIRM_DECIDE = "/v1/firm/proposals/decide"
ADMINISTRATION_PROPOSALS = "/v1/administrations/{administration_id}/proposals"


def register(app: FastAPI) -> None:
    app.add_api_route(
        FIRM_PROPOSALS, list_firm_proposals, methods=["GET"], name="list_firm_proposals"
    )
    app.add_api_route(FIRM_DECIDE, decide_proposals, methods=["POST"], name="decide_proposals")
    app.add_api_route(
        ADMINISTRATION_PROPOSALS,
        list_administration_proposals,
        methods=["GET"],
        name="list_administration_proposals",
    )


async def get_proposal_review_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ProposalReviewService:
    return ProposalReviewService(
        store=SqlProposalRepository(session),
        authorization=authorization,
        bank=build_bank_service(session, authorization),
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _group_json(group: ProposalGroup) -> dict[str, object]:
    return {
        "group_key": group.group_key,
        "counterparty": group.counterparty,
        "account_code": group.account_code,
        "account_name": group.account_name,
        "count": group.count,
        "client_count": group.client_count,
        # NFR-031: a decimal string, never a float.
        "total_amount": str(group.total_amount),
        "proposals": [
            {
                "id": str(p.id),
                "administration_id": str(p.administration_id),
                "display_name": p.display_name,
                "date": p.date.isoformat(),
                "amount": str(p.amount),
                "description": p.description,
            }
            for p in group.proposals
        ],
    }


def _review_json(total: int, groups: list[ProposalGroup]) -> dict[str, object]:
    return {"total": total, "groups": [_group_json(g) for g in groups]}


def _outcome_json(outcome: DecideOutcome) -> dict[str, object]:
    return {
        "approved": outcome.approved,
        "rejected": outcome.rejected,
        "failed": [{"proposal_id": str(f.proposal_id), "reason": f.reason} for f in outcome.failed],
    }


def _invalid(request: Request, field: str) -> Exception:
    return problem(
        request, 422, "errors.booking_proposals_invalid", reason="proposals_invalid", field=field
    )


def _user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class DecisionBody(BaseModel):
    proposal_id: str
    decision: str


class DecideBody(BaseModel):
    decisions: list[DecisionBody]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


#: The review sheet's read: the Bank screen's "View bank transactions", per portfolio client.
PROPOSALS_READ = require_portfolio_permission("view", "bank_transaction")
#: Deciding: "Reconcile bank", per portfolio client. POSTING because an approval books; the
#: request-level entry this declares sits beside the per-proposal entries decide() records.
PROPOSALS_DECIDE = require_portfolio_permission(
    "reconcile", "bank_transaction", audit=AuditCategory.POSTING
)


async def list_firm_proposals(
    request: Request,
    administration_ids: str | None = None,
    service: ProposalReviewService = Depends(get_proposal_review_service),
    portfolio: Portfolio = Depends(PROPOSALS_READ),
) -> dict[str, object]:
    """Every pending proposal across the caller's portfolio, grouped by counterparty and target
    account. `administration_ids` (comma-separated) narrows it - never widens it."""
    shown = portfolio.administration_ids
    if administration_ids is not None and administration_ids.strip():
        try:
            wanted = {uuid.UUID(part.strip()) for part in administration_ids.split(",")}
        except ValueError as exc:
            raise _invalid(request, "administration_ids") from exc
        shown = [a for a in shown if a in wanted]
    total, groups = await service.review(administration_ids=shown)
    return _review_json(total, groups)


async def list_administration_proposals(
    administration_id: uuid.UUID,
    service: ProposalReviewService = Depends(get_proposal_review_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    total, groups = await service.review(administration_ids=[administration_id])
    return _review_json(total, groups)


async def decide_proposals(
    body: DecideBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ProposalReviewService = Depends(get_proposal_review_service),
    portfolio: Portfolio = Depends(PROPOSALS_DECIDE),
    # IAM-010b: an approval posts to the ledger.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Approve or reject proposals, each against its own administration. 200 with a per-item
    `failed[]` - a refusal or failure of one never fails the rest, and never becomes a 500."""
    user_id = _user(request, tenant)
    if len(body.decisions) > MAX_DECISIONS:
        raise _invalid(request, "decisions")
    decisions: list[DecisionRequest] = []
    for entry in body.decisions:
        try:
            decisions.append(
                DecisionRequest(
                    proposal_id=uuid.UUID(entry.proposal_id), decision=Decision(entry.decision)
                )
            )
        except ValueError as exc:
            raise _invalid(request, "decisions") from exc
    outcome = await service.decide(
        user_id=user_id,
        acting_organization_id=tenant.organization_id,
        decisions=decisions,
        portfolio=frozenset(portfolio.administration_ids),
    )
    return _outcome_json(outcome)

"""A client's approval rules, and what they booked (FR-BNK-004/006, ADR-113).

    GET  /v1/administrations/{id}/rules                            the client's rules
    POST /v1/administrations/{id}/rules/{rule_id}/retire           switch one off, for good
    POST /v1/administrations/{id}/rules/{rule_id}/max-amount       {"max_amount": "250.00"|null}
    GET  /v1/administrations/{id}/rule-postings?limit=             every booking a rule made

Rules are made by `remember: true` on `POST /v1/firm/proposals/decide`, never here.

There is no `rule-postings/{proposal_id}/undo`: the bank module has no un-match path
(0065's trigger refuses reconciled -> unmatched; a correction is a reversing entry), so contract
decision 4 says undo is not built in this wave. Every item says `undoable: false`.

--- Permissions: none new ---

Reads take "View bank transactions" (`view bank_transaction`), changes "Reconcile bank"
(`reconcile bank_transaction`) - a rule is standing reconciliation, and retiring or capping one is
deciding what gets reconciled. Every route is per client: `require_permission` on the path's
administration, and every query also names that administration, so a rule of another client is
not found (FR-BNK-006).

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.authz.dependencies import administration_from_path, require_permission
from api.authz.model import AuthorizationDecision
from api.db import get_db_session
from api.firm.rules import RuleStatus
from api.firm.rules_repository import RulePosting, RuleView, SqlRuleRepository
from api.i18n.http import problem
from api.tenancy import TenantContext, get_tenant_context

RULES = "/v1/administrations/{administration_id}/rules"
RULE_RETIRE = "/v1/administrations/{administration_id}/rules/{rule_id}/retire"
RULE_MAX_AMOUNT = "/v1/administrations/{administration_id}/rules/{rule_id}/max-amount"
RULE_POSTINGS = "/v1/administrations/{administration_id}/rule-postings"

DEFAULT_POSTINGS_LIMIT = 50
MAX_POSTINGS_LIMIT = 200
#: numeric(18, 2): sixteen digits before the point.
_MAX_AMOUNT_CEILING = Decimal("9999999999999999.99")


def register(app: FastAPI) -> None:
    app.add_api_route(RULES, list_rules, methods=["GET"], name="list_booking_rules")
    app.add_api_route(RULE_RETIRE, retire_rule, methods=["POST"], name="retire_booking_rule")
    app.add_api_route(
        RULE_MAX_AMOUNT, set_rule_max_amount, methods=["POST"], name="set_booking_rule_max_amount"
    )
    app.add_api_route(
        RULE_POSTINGS, list_rule_postings, methods=["GET"], name="list_booking_rule_postings"
    )


_SCOPE = administration_from_path("administration_id")
RULES_READ = require_permission("view", "bank_transaction", scope=_SCOPE)
RULES_CHANGE = require_permission(
    "reconcile", "bank_transaction", scope=_SCOPE, audit=AuditCategory.CONFIGURATION
)


async def get_rule_repository(
    session: AsyncSession = Depends(get_db_session),
) -> SqlRuleRepository:
    return SqlRuleRepository(session)


async def get_rule_audit(session: AsyncSession = Depends(get_db_session)) -> AuditLog:
    return AuditLog(SqlAuditRepository(session))


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _rule_json(rule: RuleView) -> dict[str, object]:
    return {
        "id": str(rule.id),
        "counterparty_key": rule.counterparty_key,
        "counterparty_label": rule.counterparty_label,
        "account_code": rule.account_code,
        "account_name": rule.account_name,
        # NFR-031: a decimal string, never a float.
        "max_amount": None if rule.max_amount is None else str(rule.max_amount),
        "status": rule.status.value,
        "suspended_reason": rule.suspended_reason,
        "created_by_name": rule.created_by_name,
        "created_at": rule.created_at.isoformat(),
        "postings_count": rule.postings_count,
        "last_posted_at": None if rule.last_posted_at is None else rule.last_posted_at.isoformat(),
    }


def _posting_json(posting: RulePosting) -> dict[str, object]:
    return {
        "proposal_id": str(posting.proposal_id),
        "rule_id": str(posting.rule_id),
        "date": posting.date.isoformat(),
        "amount": str(posting.amount),
        "counterparty": posting.counterparty,
        "account_code": posting.account_code,
        "posted_at": posting.posted_at.isoformat(),
        # Contract decision 4: no un-match path exists in the bank module, so nothing is undoable.
        "undoable": False,
    }


def _invalid(request: Request, field: str) -> Exception:
    return problem(
        request, 422, "errors.booking_rules_invalid", reason="booking_rules_invalid", field=field
    )


def _not_found(request: Request) -> Exception:
    return problem(request, 404, "errors.booking_rule_not_found", reason="booking_rule_not_found")


def _user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


def parse_max_amount(raw: str | None) -> Decimal | None:
    """A positive decimal string with at most two decimals, or None to lift the cap. Raises
    ValueError otherwise. Never a float (NFR-031)."""
    if raw is None:
        return None
    try:
        amount = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("not a decimal") from exc
    if not amount.is_finite() or amount <= 0 or amount > _MAX_AMOUNT_CEILING:
        raise ValueError("out of range")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("more than two decimals")
    return amount.quantize(Decimal("0.01"))


async def _audit(
    audit: AuditLog,
    tenant: TenantContext,
    user_id: uuid.UUID,
    administration_id: uuid.UUID,
    rule_id: uuid.UUID,
    action: str,
    detail: dict[str, str],
) -> None:
    await audit.record(
        AuditEvent(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            category=AuditCategory.CONFIGURATION,
            action=action,
            resource_type="booking_rule",
            resource_id=rule_id,
            outcome=AuditOutcome.SUCCESS,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            detail=detail,
        )
    )


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class MaxAmountBody(BaseModel):
    max_amount: str | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def list_rules(
    administration_id: uuid.UUID,
    rules: SqlRuleRepository = Depends(get_rule_repository),
    _: AuthorizationDecision = Depends(RULES_READ),
) -> dict[str, object]:
    views = await rules.views(administration_id=administration_id)
    return {"rules": [_rule_json(v) for v in views]}


async def retire_rule(
    administration_id: uuid.UUID,
    rule_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    rules: SqlRuleRepository = Depends(get_rule_repository),
    audit: AuditLog = Depends(get_rule_audit),
    _: AuthorizationDecision = Depends(RULES_CHANGE),
) -> dict[str, object]:
    """Final: a retired rule never books again. Retiring a retired rule answers it as it is."""
    user_id = _user(request, tenant)
    rule = await rules.get(administration_id=administration_id, rule_id=rule_id)
    if rule is None:
        raise _not_found(request)
    if rule.status is not RuleStatus.RETIRED and await rules.retire(
        rule_id=rule.id, user_id=user_id
    ):
        await _audit(
            audit,
            tenant,
            user_id,
            administration_id,
            rule.id,
            "retire_booking_rule",
            {"previous_status": rule.status.value},
        )
    view = await rules.view(administration_id=administration_id, rule_id=rule.id)
    assert view is not None
    return _rule_json(view)


async def set_rule_max_amount(
    administration_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: MaxAmountBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    rules: SqlRuleRepository = Depends(get_rule_repository),
    audit: AuditLog = Depends(get_rule_audit),
    _: AuthorizationDecision = Depends(RULES_CHANGE),
) -> dict[str, object]:
    user_id = _user(request, tenant)
    try:
        amount = parse_max_amount(body.max_amount)
    except ValueError as exc:
        raise _invalid(request, "max_amount") from exc
    rule = await rules.get(administration_id=administration_id, rule_id=rule_id)
    if rule is None:
        raise _not_found(request)
    if rule.status is RuleStatus.RETIRED:
        raise problem(request, 409, "errors.booking_rule_retired", reason="booking_rule_retired")
    await rules.set_max_amount(
        administration_id=administration_id, rule_id=rule.id, max_amount=amount
    )
    await _audit(
        audit,
        tenant,
        user_id,
        administration_id,
        rule.id,
        "set_booking_rule_max_amount",
        {
            "max_amount": "" if amount is None else str(amount),
            "previous_max_amount": "" if rule.max_amount is None else str(rule.max_amount),
        },
    )
    view = await rules.view(administration_id=administration_id, rule_id=rule.id)
    assert view is not None
    return _rule_json(view)


async def list_rule_postings(
    administration_id: uuid.UUID,
    request: Request,
    limit: str | None = None,
    rules: SqlRuleRepository = Depends(get_rule_repository),
    _: AuthorizationDecision = Depends(RULES_READ),
) -> dict[str, object]:
    size = DEFAULT_POSTINGS_LIMIT
    if limit is not None:
        try:
            size = int(limit)
        except ValueError as exc:
            raise _invalid(request, "limit") from exc
        if not 1 <= size <= MAX_POSTINGS_LIMIT:
            raise _invalid(request, "limit")
    postings = await rules.postings(administration_id=administration_id, limit=size)
    return {"items": [_posting_json(p) for p in postings]}

"""The Bank screen's HTTP surface (`/bank` in the web app).

    POST /v1/administrations/{id}/bank-accounts
    GET  /v1/administrations/{id}/bank-accounts
    POST /v1/administrations/{id}/bank-accounts/{account_id}/import
    GET  /v1/administrations/{id}/bank-accounts/{account_id}/transactions
    GET  /v1/administrations/{id}/bank-transactions/{transaction_id}/match-candidates
    POST /v1/administrations/{id}/bank-transactions/{transaction_id}/reconcile-with-invoice
    POST /v1/administrations/{id}/bank-transactions/{transaction_id}/reconcile

Registered via `register(app)`, not `include_router` - see
`api.documents.routes.register` for why.

--- Permissions: none new ---

Every route here rides an Appendix A row that already existed before this
module did: "Connect / revoke bank consent" (`manage bank_consent`),
"View bank transactions" (`view bank_transaction`), "Reconcile bank"
(`reconcile bank_transaction`). The authorization matrix anticipated this
domain; this module is the first thing that reaches it.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
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
from api.bank.csv_parser import CsvStatementError, InvalidRow, MissingColumns
from api.bank.matching import ScoredCandidate
from api.bank.model import (
    BankAccount,
    BankAccountNotFound,
    BankError,
    BankTransaction,
    ImportResult,
    InvalidBankField,
    MatchCandidate,
    NoActiveBankJournal,
    NoOpenPeriod,
    TransactionAlreadyReconciled,
    TransactionNotFound,
    TransactionStatus,
)
from api.bank.repository import SqlBankRepository
from api.bank.service import BankService, StatementForAnotherAccount
from api.bank.statement_formats import (
    AmbiguousAccount,
    UnknownStatementFormat,
    UnsafeStatement,
)
from api.db import get_db_session
from api.i18n.http import problem
from api.invoicing.payments import SalesPaymentService
from api.invoicing.payments_repository import SqlPaymentRepository
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}"
_ACCOUNTS = f"{_BASE}/bank-accounts"
_ACCOUNT = f"{_ACCOUNTS}/{{bank_account_id}}"
_TRANSACTIONS = f"{_BASE}/bank-transactions"
_TRANSACTION = f"{_TRANSACTIONS}/{{transaction_id}}"


def register(app: FastAPI) -> None:
    app.add_api_route(_ACCOUNTS, create_bank_account, methods=["POST"], name="create_bank_account")
    app.add_api_route(_ACCOUNTS, list_bank_accounts, methods=["GET"], name="list_bank_accounts")
    app.add_api_route(
        f"{_ACCOUNT}/import", import_statement, methods=["POST"], name="import_bank_statement"
    )
    app.add_api_route(
        f"{_ACCOUNT}/transactions",
        list_transactions,
        methods=["GET"],
        name="list_bank_transactions",
    )
    app.add_api_route(
        f"{_TRANSACTION}/match-candidates",
        get_match_candidates,
        methods=["GET"],
        name="get_bank_match_candidates",
    )
    app.add_api_route(
        f"{_TRANSACTION}/reconcile-with-invoice",
        reconcile_with_invoice,
        methods=["POST"],
        name="reconcile_bank_transaction_with_invoice",
    )
    app.add_api_route(
        f"{_TRANSACTION}/reconcile",
        reconcile_generic,
        methods=["POST"],
        name="reconcile_bank_transaction",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def get_bank_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> BankService:
    audit_log = AuditLog(SqlAuditRepository(session))
    return BankService(
        SqlBankRepository(session),
        build_ledger_service(session, audit_log),
        SalesPaymentService(
            repository=SqlPaymentRepository(session),
            ledger=build_ledger_service(session, audit_log),
            authorization=authorization,
            audit_log=audit_log,
        ),
        audit_log,
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _account_json(account: BankAccount) -> dict[str, object]:
    return {
        "id": str(account.id),
        "name": account.name,
        "iban": account.iban,
        "currency": account.currency,
        "ledger_account_id": str(account.ledger_account_id),
        "status": account.status.value,
    }


def _transaction_json(transaction: BankTransaction) -> dict[str, object]:
    return {
        "id": str(transaction.id),
        "bank_account_id": str(transaction.bank_account_id),
        "booking_date": transaction.booking_date.isoformat(),
        "value_date": transaction.value_date.isoformat() if transaction.value_date else None,
        "amount": str(transaction.amount),
        "currency": transaction.currency,
        "counterparty_name": transaction.counterparty_name,
        "counterparty_iban": transaction.counterparty_iban,
        "description": transaction.description,
        "status": transaction.status.value,
        "matched_sales_invoice_id": (
            str(transaction.matched_sales_invoice_id)
            if transaction.matched_sales_invoice_id
            else None
        ),
        "journal_entry_id": str(transaction.journal_entry_id)
        if transaction.journal_entry_id
        else None,
        "reconciled_at": transaction.reconciled_at.isoformat()
        if transaction.reconciled_at
        else None,
    }


def _candidate_json(scored: ScoredCandidate) -> dict[str, object]:
    candidate: MatchCandidate = scored.candidate
    return {
        "invoice_id": str(candidate.invoice_id),
        "invoice_reference": candidate.invoice_reference,
        "customer_name": candidate.customer_name,
        "outstanding": str(candidate.outstanding),
        "invoice_date": candidate.invoice_date.isoformat(),
        # FR-BNK-003's confidence, and why (ADR-091).
        "confidence": scored.confidence.value,
        "reasons": list(scored.reasons),
    }


def _import_json(result: ImportResult) -> dict[str, object]:
    return {
        "import_id": str(result.import_id),
        "transaction_count": result.transaction_count,
        "duplicate_count": result.duplicate_count,
    }


def _refuse(request: Request, exc: Exception) -> Exception:
    if isinstance(exc, BankAccountNotFound):
        return problem(
            request, 404, "errors.bank_account_not_found", reason="bank_account_not_found"
        )
    if isinstance(exc, TransactionNotFound):
        return problem(
            request, 404, "errors.bank_transaction_not_found", reason="bank_transaction_not_found"
        )
    if isinstance(exc, TransactionAlreadyReconciled):
        return problem(
            request,
            409,
            "errors.bank_transaction_already_reconciled",
            reason="bank_transaction_already_reconciled",
        )
    if isinstance(exc, NoOpenPeriod):
        return problem(request, 409, "errors.period_invalid", reason="period_invalid")
    if isinstance(exc, NoActiveBankJournal):
        return problem(
            request, 409, "errors.bank_no_active_journal", reason="bank_no_active_journal"
        )
    if isinstance(exc, InvalidBankField):
        return problem(
            request, 422, "errors.bank_field_invalid", reason="bank_field_invalid", field=exc.field
        )
    if isinstance(exc, StatementForAnotherAccount):
        return problem(
            request,
            422,
            "errors.bank_statement_other_account",
            reason="bank_statement_other_account",
            iban=exc.iban,
        )
    if isinstance(exc, (UnknownStatementFormat, MissingColumns)):
        return problem(
            request,
            422,
            "errors.bank_statement_unknown_format",
            reason="bank_statement_unknown_format",
        )
    if isinstance(exc, AmbiguousAccount):
        return problem(
            request,
            422,
            "errors.bank_statement_ambiguous_account",
            reason="bank_statement_ambiguous_account",
        )
    if isinstance(exc, UnsafeStatement):
        return problem(request, 422, "errors.bank_statement_unsafe", reason="bank_statement_unsafe")
    if isinstance(exc, InvalidRow):
        return problem(
            request,
            422,
            "errors.bank_statement_invalid",
            reason="bank_statement_invalid",
            row=exc.row_number,
            field=exc.field,
        )
    if isinstance(exc, CsvStatementError):
        return problem(
            request,
            422,
            "errors.bank_statement_unknown_format",
            reason="bank_statement_unknown_format",
        )
    if isinstance(exc, BankError):
        return problem(request, 422, "errors.bank_field_invalid", reason="bank_field_invalid")
    return exc


def _parse_uuid(request: Request, field: str, value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise problem(
            request, 422, "errors.bank_field_invalid", reason="bank_field_invalid", field=field
        ) from exc


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class CreateBankAccountBody(BaseModel):
    name: str
    iban: str | None = None
    currency: str = "EUR"
    ledger_account_id: str


class ImportStatementBody(BaseModel):
    filename: str | None = None
    csv: str


class ReconcileWithInvoiceBody(BaseModel):
    invoice_id: str


class ReconcileGenericBody(BaseModel):
    offset_account_id: str
    description: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def create_bank_account(
    administration_id: uuid.UUID,
    body: CreateBankAccountBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "manage",
            "bank_consent",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    ledger_account_id = _parse_uuid(request, "ledger_account_id", body.ledger_account_id)
    try:
        account = await service.create_account(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            actor_user_id=tenant.user_id,
            name=body.name,
            iban=body.iban,
            currency=body.currency,
            ledger_account_id=ledger_account_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return _account_json(account)


async def list_bank_accounts(
    administration_id: uuid.UUID,
    request: Request,
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    del request
    accounts = await service.list_accounts(administration_id=administration_id)
    return {"bank_accounts": [_account_json(account) for account in accounts]}


async def import_statement(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    body: ImportStatementBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reconcile",
            "bank_transaction",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        result = await service.import_statement(
            organization_id=tenant.organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            csv_text=body.csv,
            filename=body.filename,
            actor_user_id=tenant.user_id,
        )
    # A statement refusal is not a BankError; uncaught, a file the parser refused was a 500.
    except (BankError, CsvStatementError) as exc:
        raise _refuse(request, exc) from exc
    return _import_json(result)


async def list_transactions(
    administration_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    request: Request,
    status: str | None = None,
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    parsed_status: TransactionStatus | None = None
    if status is not None:
        try:
            parsed_status = TransactionStatus(status)
        except ValueError as exc:
            raise problem(
                request,
                422,
                "errors.bank_field_invalid",
                reason="bank_field_invalid",
                field="status",
            ) from exc
    transactions = await service.list_transactions(
        administration_id=administration_id, bank_account_id=bank_account_id, status=parsed_status
    )
    suggestions = await service.suggestions(
        administration_id=administration_id, transactions=transactions
    )
    return {
        "transactions": [
            {
                **_transaction_json(t),
                "suggestion": (_candidate_json(suggestions[t.id]) if t.id in suggestions else None),
            }
            for t in transactions
        ]
    }


async def get_match_candidates(
    administration_id: uuid.UUID,
    transaction_id: uuid.UUID,
    request: Request,
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "view", "bank_transaction", scope=administration_from_path("administration_id")
        )
    ),
) -> dict[str, object]:
    try:
        transaction = await service.get_transaction(
            administration_id=administration_id, transaction_id=transaction_id
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    candidates = await service.match_candidates(
        administration_id=administration_id, transaction=transaction
    )
    return {"candidates": [_candidate_json(c) for c in candidates]}


async def reconcile_with_invoice(
    administration_id: uuid.UUID,
    transaction_id: uuid.UUID,
    body: ReconcileWithInvoiceBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reconcile",
            "bank_transaction",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    invoice_id = _parse_uuid(request, "invoice_id", body.invoice_id)
    try:
        transaction = await service.reconcile_with_invoice(
            administration_id=administration_id,
            transaction_id=transaction_id,
            invoice_id=invoice_id,
            actor_user_id=tenant.user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return _transaction_json(transaction)


async def reconcile_generic(
    administration_id: uuid.UUID,
    transaction_id: uuid.UUID,
    body: ReconcileGenericBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: BankService = Depends(get_bank_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "reconcile",
            "bank_transaction",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
) -> dict[str, object]:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    offset_account_id = _parse_uuid(request, "offset_account_id", body.offset_account_id)
    try:
        transaction = await service.reconcile_generic(
            administration_id=administration_id,
            transaction_id=transaction_id,
            offset_account_id=offset_account_id,
            description=body.description,
            actor_user_id=tenant.user_id,
        )
    except BankError as exc:
        raise _refuse(request, exc) from exc
    return _transaction_json(transaction)

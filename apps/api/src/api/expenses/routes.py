"""Receipt capture's HTTP surface - FR-EXP-001, FR-EXP-001a.

--- One intake endpoint, because both paths land in the same place ---

`POST .../capture-sessions/{id}/pages` is where a camera frame and a
drag-and-dropped file both arrive. There is no `/camera` and no `/upload`. The
`source` query parameter is recorded on the row and read by nothing, so the two
paths cannot drift: a second endpoint would be a second pipeline, and the one
built later would be the one that forgot the malware scan.

The body is the raw bytes with `Content-Type` describing them, exactly as
`api.documents.routes.upload_document` takes them and for the same reasons -
no multipart parser over untrusted input, and a `File` is a `Blob` so a file
picker sends it directly.

--- The one parameter that distinguishes the two "several" ---

    ?item=<uuid>   absent  -> a NEW receipt: new item, new draft expense
                   present -> ANOTHER original for a receipt already captured

That is the whole difference between FR-EXP-001's multi-page and
FR-EXP-001a's batch, and it is explicit because nothing in an image says which
it is. A heuristic on elapsed time or similarity would be wrong silently, in
the direction that either multiplies or merges somebody's claim.

--- Registered directly on the app ---

Via `register(app)`, not `include_router`. This FastAPI version hides an
included router's routes behind an opaque wrapper that the authorization
middleware and all four coverage checks walk straight past - see
`api.documents.routes.register`, which explains it at length.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import Depends, FastAPI, Query, Request
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
from api.db import get_db_session
from api.documents.content_type import ContentTypeError
from api.documents.model import (
    MAX_ANY_BYTES,
    DocumentInfected,
    DocumentTooLarge,
    ScanUnavailable,
)
from api.documents.routes import get_document_service
from api.documents.service import DocumentService
from api.expenses.capture import CaptureService
from api.expenses.categories import UnknownExpenseCategory
from api.expenses.form import ExpenseFormService, ExpenseView
from api.expenses.model import (
    CaptureSession,
    CaptureSource,
    EmptySession,
    Expense,
    ExpenseAlreadyReady,
    ExpenseNotFound,
    ExpenseStatus,
    IncompleteExpense,
    ItemNotFound,
    ItemWithoutPages,
    PaymentMethod,
    ReviewEntry,
    SessionAlreadyFinalised,
    SessionNotFound,
    VatRateUnavailable,
    VatTreatment,
)
from api.expenses.posting import (
    ExpenseAlreadyPosted,
    ExpensePostingService,
    NoOpenPeriod,
    NoPurchaseJournal,
    PostingConfigurationMissing,
)
from api.expenses.repository import SqlCaptureRepository
from api.expenses.vat import VatError
from api.i18n.http import problem
from api.ledger.service import build_ledger_service
from api.tenancy import TenantContext, get_tenant_context


def register(app: FastAPI) -> None:
    """See api.documents.routes.register for why this is not include_router."""
    app.add_api_route(
        "/v1/administrations/{administration_id}/capture-sessions",
        open_capture_session,
        methods=["POST"],
        name="open_capture_session",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/capture-sessions/{session_id}/pages",
        capture_page,
        methods=["POST"],
        name="capture_page",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/capture-sessions/{session_id}",
        review_capture_session,
        methods=["GET"],
        name="review_capture_session",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/capture-sessions/{session_id}/finalise",
        finalise_capture_session,
        methods=["POST"],
        name="finalise_capture_session",
    )
    # MOB-004: the Approve/View tabs' list. A distinct path shape from
    # {expense_id} below - "/expenses" with no further segment - so the two
    # never compete for the same request.
    app.add_api_route(
        "/v1/administrations/{administration_id}/expenses",
        list_expenses,
        methods=["GET"],
        name="list_expenses",
    )
    # FR-EXP-001b / FR-EXP-001e: the form.
    app.add_api_route(
        "/v1/administrations/{administration_id}/expenses/{expense_id}",
        get_expense,
        methods=["GET"],
        name="get_expense",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/expenses/{expense_id}",
        update_expense,
        methods=["PATCH"],
        name="update_expense",
    )
    app.add_api_route(
        "/v1/administrations/{administration_id}/expenses/{expense_id}/ready",
        mark_expense_ready,
        methods=["POST"],
        name="mark_expense_ready",
    )
    # FR-EXP-001d: confirmation into the ledger.
    app.add_api_route(
        "/v1/administrations/{administration_id}/expenses/{expense_id}/posting",
        post_expense,
        methods=["POST"],
        name="post_expense",
    )


async def get_capture_service(
    session: AsyncSession = Depends(get_db_session),
    documents: DocumentService = Depends(get_document_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> CaptureService:
    """Built per request, over the SAME document service the archive endpoints
    use - so a captured receipt goes through SEC-005's content-type check,
    malware scan and per-tenant encryption on exactly the path a direct upload
    does. A capture pipeline with its own document handling would be a second
    place for those to be got right.
    """
    return CaptureService(
        repository=SqlCaptureRepository(session),
        documents=documents,
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


def _review_json(entry: ReviewEntry) -> dict[str, object]:
    return {
        "item_id": str(entry.item_id),
        "position": entry.position,
        "expense_id": str(entry.expense_id) if entry.expense_id else None,
        # The distinction FR-EXP-001 and FR-EXP-001a turn on, made visible: how
        # many ORIGINALS this receipt holds. A four-page PDF reports 1.
        "page_count": entry.page_count,
        "content_types": list(entry.content_types),
        "total_bytes": entry.total_bytes,
        "discarded": entry.discarded,
        # An exact byte-for-byte duplicate captured earlier in this session.
        # NOT FR-EXP-001g, which matches on supplier/date/amount and needs
        # extraction - see api.expenses.model.ReviewEntry.
        "duplicate_of": str(entry.duplicate_of) if entry.duplicate_of else None,
    }


def _session_json(session: CaptureSession) -> dict[str, object]:
    return {
        "id": str(session.id),
        "open": session.is_open,
        "opened_at": session.opened_at.isoformat() if session.opened_at else None,
        "finalised_at": session.finalised_at.isoformat() if session.finalised_at else None,
        "items": [_review_json(entry) for entry in session.items],
        # What finalising would produce, so a review screen can say "6 expenses"
        # before the person commits. One per live item - pages do not count.
        "expenses_to_create": len(session.live_items),
    }


async def open_capture_session(
    administration_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CaptureService = Depends(get_capture_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
            # IAM-090: a batch of claims entering the books is context an
            # auditor reads the individual expenses in.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-EXP-001a: open a sitting."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    session = await service.open_session(
        administration_id=administration_id, actor_user_id=tenant.user_id
    )
    return _session_json(session)


async def capture_page(
    administration_id: uuid.UUID,
    session_id: uuid.UUID,
    request: Request,
    fiscal_year_id: uuid.UUID,
    source: CaptureSource = CaptureSource.UPLOAD,
    item: uuid.UUID | None = None,
    filename: str | None = None,
    category: str | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CaptureService = Depends(get_capture_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """FR-EXP-001's single landing place, for camera and upload alike.

    `source` defaults to `upload` rather than being required: a client that
    forgets it is describing a file that arrived some way, and the value
    changes nothing about the handling. Making it mandatory would fail a
    capture over a label.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_ANY_BYTES:
        raise problem(
            request,
            413,
            "errors.document_too_large",
            reason="document_too_large",
            limit_bytes=MAX_ANY_BYTES,
        )

    data = await request.body()

    try:
        item_id, document, page_number = await service.capture(
            administration_id=administration_id,
            session_id=session_id,
            actor_user_id=tenant.user_id,
            fiscal_year_id=fiscal_year_id,
            data=data,
            source=source,
            filename=filename,
            declared_content_type=request.headers.get("content-type"),
            item_id=item,
            category=category,
        )
    except UnknownExpenseCategory as exc:
        # The picker only offers keys from the shared list, so this is a stale or
        # hand-built client. Refused rather than filed under "Other": a receipt
        # in the wrong category is a wrong ledger account later.
        raise problem(
            request,
            422,
            "errors.expense_category_unknown",
            reason="unknown_expense_category",
        ) from exc
    except SessionNotFound as exc:
        raise problem(
            request, 404, "errors.capture_session_not_found", reason="capture_session_not_found"
        ) from exc
    except SessionAlreadyFinalised as exc:
        raise problem(
            request, 409, "errors.capture_session_closed", reason="capture_session_closed"
        ) from exc
    except ItemNotFound as exc:
        raise problem(
            request, 404, "errors.capture_item_not_found", reason="capture_item_not_found"
        ) from exc
    except DocumentTooLarge as exc:
        raise problem(
            request,
            413,
            "errors.document_too_large",
            reason="document_too_large",
            limit_bytes=exc.limit,
        ) from exc
    except ContentTypeError as exc:
        # FR-EXP-001's five formats are the document archive's allowlist; a
        # sixth is refused there rather than here, so capture and direct upload
        # cannot disagree about what a receipt may be.
        raise problem(
            request, 415, "errors.document_type_rejected", reason="unsupported_document_type"
        ) from exc
    except DocumentInfected as exc:
        raise problem(request, 422, "errors.document_infected", reason="document_infected") from exc
    except ScanUnavailable as exc:
        raise problem(
            request, 503, "errors.document_scan_unavailable", reason="scan_unavailable"
        ) from exc

    return {
        "item_id": str(item_id),
        "document_id": str(document.id),
        "page_number": page_number,
        "content_type": document.content_type.value,
        # True when this started a new receipt, so a batch client can show the
        # item it just opened rather than guessing from the page number.
        "started_new_receipt": page_number == 1,
    }


async def review_capture_session(
    administration_id: uuid.UUID,
    session_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CaptureService = Depends(get_capture_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """FR-EXP-001a's "review list before posting"."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        session = await service.review(
            administration_id=administration_id,
            session_id=session_id,
            actor_user_id=tenant.user_id,
        )
    except SessionNotFound as exc:
        raise problem(
            request, 404, "errors.capture_session_not_found", reason="capture_session_not_found"
        ) from exc
    return _session_json(session)


async def finalise_capture_session(
    administration_id: uuid.UUID,
    session_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: CaptureService = Depends(get_capture_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Accept the review list.

    Capture ends here. The expenses become `ready`, which is a handoff to
    FR-EXP-002's approval workflow and FR-EXP-003's reimbursement run - neither
    of which is built. Nothing is posted to the ledger by this endpoint.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        expenses = await service.finalise(
            administration_id=administration_id,
            session_id=session_id,
            actor_user_id=tenant.user_id,
        )
    except SessionNotFound as exc:
        raise problem(
            request, 404, "errors.capture_session_not_found", reason="capture_session_not_found"
        ) from exc
    except SessionAlreadyFinalised as exc:
        raise problem(
            request, 409, "errors.capture_session_closed", reason="capture_session_closed"
        ) from exc
    except EmptySession as exc:
        raise problem(
            request, 409, "errors.capture_session_empty", reason="capture_session_empty"
        ) from exc
    except ItemWithoutPages as exc:
        raise problem(
            request,
            409,
            "errors.capture_item_without_evidence",
            reason="capture_item_without_evidence",
            item_ids=[str(i) for i in exc.item_ids],
        ) from exc

    return {
        "session_id": str(session_id),
        "expenses": [
            {"id": str(e.id), "capture_item_id": str(e.capture_item_id), "status": e.status.value}
            for e in expenses
        ],
    }


# ===========================================================================
# FR-EXP-001b / FR-EXP-001e: the expense form
# ===========================================================================


async def get_expense_form_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ExpenseFormService:
    return ExpenseFormService(
        repository=SqlCaptureRepository(session),
        authorization=authorization,
        audit_log=AuditLog(SqlAuditRepository(session)),
    )


class ExpenseFormBody(BaseModel):
    """Any subset of the form. Every field optional, because FR-EXP-001c makes
    a partly-filled expense the normal state - "the product never blocks on
    extraction being available".

    `gross_amount` is a STRING on the wire and a Decimal here. JSON has one
    number type and it is a double: a client sending `1234.56` unquoted would
    have already lost the value before pydantic saw it, and NFR-031 covers the
    whole calculation path. Pydantic parses a quoted decimal exactly.

    There is no `vat_rate`, `vat_amount` or `net_amount` field. The rate comes
    from the treatment and the date (CMP-014) and the amounts are computed -
    accepting any of them would let a caller assert a figure the database is
    about to disagree with.
    """

    expense_date: date | None = None
    supplier: str | None = None
    gross_amount: Decimal | None = None
    vat_treatment: VatTreatment | None = None
    category: str | None = None
    payment_method: PaymentMethod | None = None


def _expense_json(view: ExpenseView) -> dict[str, object]:
    """One shape, produced once, so web and mobile render the same form.

    Amounts cross the wire as STRINGS for the reason the request body takes
    them as strings: a client that did `JSON.parse` on an unquoted 1234.56
    would hold a double, and every figure derived from it downstream would be
    off by amounts too small to notice and too large to ignore.
    """
    expense = view.expense
    return {
        "id": str(expense.id),
        "status": expense.status.value,
        "capture_item_id": str(expense.capture_item_id),
        "expense_date": expense.expense_date.isoformat() if expense.expense_date else None,
        "supplier": expense.supplier,
        "gross_amount": str(expense.gross_amount) if expense.gross_amount is not None else None,
        "vat_treatment": expense.vat_treatment.value if expense.vat_treatment else None,
        # Shown, never accepted. FR-EXP-001b's "VAT rate" is what the form
        # DISPLAYS; what it stores is the treatment it came from.
        "vat_rate": str(expense.vat_rate) if expense.vat_rate is not None else None,
        "vat_amount": str(expense.vat_amount) if expense.vat_amount is not None else None,
        "net_amount": str(expense.net_amount) if expense.net_amount is not None else None,
        "category": expense.category,
        "payment_method": expense.payment_method.value if expense.payment_method else None,
        # FR-EXP-001b's "defaulting from the user's history". Null once the
        # person has chosen a category - a default does not correct a choice.
        "suggested_category": view.suggested_category,
        # What marking ready would still refuse for, so a form can show the
        # outstanding fields instead of the person discovering them one at a
        # time.
        "missing_fields": list(view.missing_fields),
        "can_be_marked_ready": view.can_be_marked_ready,
        # FR-EXP-001g. Present alongside `can_be_marked_ready: true` on
        # purpose: these WARN and never block, so a client showing them must
        # not gate the submit button on them being empty.
        "duplicate_warnings": [
            {
                "expense_id": str(warning.expense_id),
                "strength": warning.strength.value,
                "supplier": warning.supplier,
                "expense_date": warning.on.isoformat(),
                "gross_amount": str(warning.gross_amount),
                "status": warning.status,
                # Their own double entry is a mistake to fix; somebody else's
                # is a conversation to have. WHO the other person is stays out
                # of the response - a duplicate check is a poor place to learn
                # what one's colleagues have been spending.
                "same_submitter": warning.same_submitter,
                "similarity": warning.similarity,
            }
            for warning in view.duplicate_warnings
        ],
    }


def _expense_summary_json(expense: Expense) -> dict[str, object]:
    """A list row - MOB-004's Approve tab (drafts to finish) and View tab
    (posted/ready expenses already submitted).

    Deliberately lighter than `_expense_json`: no suggested category, no
    FR-EXP-001g duplicate warnings. Both are per-expense reads a person needs
    only once they open a specific item in `ExpenseForm` - which calls
    `get_expense` for that full view - and computing them for every row of a
    list would be a query fan-out proportional to how many are listed.
    """
    return {
        "id": str(expense.id),
        "status": expense.status.value,
        "capture_item_id": str(expense.capture_item_id),
        "expense_date": expense.expense_date.isoformat() if expense.expense_date else None,
        "supplier": expense.supplier,
        "gross_amount": str(expense.gross_amount) if expense.gross_amount is not None else None,
        "category": expense.category,
        "missing_fields": list(expense.missing_fields),
        "can_be_marked_ready": expense.is_complete,
    }


async def list_expenses(
    administration_id: uuid.UUID,
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    tenant: TenantContext = Depends(get_tenant_context),
    service: ExpenseFormService = Depends(get_expense_form_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> list[dict[str, object]]:
    """MOB-004's Approve tab (`?status=draft`) and View tab.

    No cursor pagination - `limit` alone, bounded and defaulted exactly like
    `api.customers.routes.list_customers`, the one other list endpoint this
    codebase has; nothing here paginates yet.

    Reuses `submit expense`, the identical permission `get_expense` and
    `update_expense` already require (ADR-012: no invented permission for
    "view your own submitted expenses" - filling in and reviewing the form
    already IS submitting it).
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    parsed_status: ExpenseStatus | None = None
    if status is not None:
        try:
            parsed_status = ExpenseStatus(status)
        except ValueError as exc:
            raise problem(
                request,
                422,
                "errors.customer_field_invalid",
                reason="customer_field_invalid",
                field="status",
                accepted=[member.value for member in ExpenseStatus],
            ) from exc

    expenses = await service.list_by_status(
        administration_id=administration_id,
        actor_user_id=tenant.user_id,
        status=parsed_status,
        limit=limit,
    )
    return [_expense_summary_json(expense) for expense in expenses]


async def get_expense(
    administration_id: uuid.UUID,
    expense_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ExpenseFormService = Depends(get_expense_form_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
        )
    ),
) -> dict[str, object]:
    """The form as it should be drawn, including the category to default to."""
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.view(
            administration_id=administration_id,
            expense_id=expense_id,
            actor_user_id=tenant.user_id,
        )
    except ExpenseNotFound as exc:
        raise problem(request, 404, "errors.expense_not_found", reason="expense_not_found") from exc
    return _expense_json(view)


async def update_expense(
    administration_id: uuid.UUID,
    expense_id: uuid.UUID,
    body: ExpenseFormBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ExpenseFormService = Depends(get_expense_form_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Save any subset of the form.

    PATCH rather than PUT, and the distinction is load-bearing: a PUT would
    make an omitted field mean "clear it", so a client saving one changed field
    would wipe the other five. Only fields PRESENT in the body are written -
    `fields_set` is what separates "not mentioned" from an explicit null.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")

    supplied = {
        name: getattr(body, name)
        for name in body.model_fields_set
        if name in ExpenseFormBody.model_fields
    }

    try:
        view = await service.update(
            administration_id=administration_id,
            expense_id=expense_id,
            actor_user_id=tenant.user_id,
            **supplied,
        )
    except ExpenseNotFound as exc:
        raise problem(request, 404, "errors.expense_not_found", reason="expense_not_found") from exc
    except ExpenseAlreadyReady as exc:
        raise problem(
            request, 409, "errors.expense_already_ready", reason="expense_already_ready"
        ) from exc
    except VatRateUnavailable as exc:
        raise problem(
            request, 422, "errors.vat_rate_unavailable", reason="vat_rate_unavailable"
        ) from exc
    except VatError as exc:
        raise problem(
            request, 422, "errors.expense_amount_invalid", reason="expense_amount_invalid"
        ) from exc
    return _expense_json(view)


async def mark_expense_ready(
    administration_id: uuid.UUID,
    expense_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ExpenseFormService = Depends(get_expense_form_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            "submit",
            "expense",
            scope=administration_from_path("administration_id"),
            # IAM-090: submitting a claim is the act an approver later answers.
            audit=AuditCategory.CONFIGURATION,
        )
    ),
) -> dict[str, object]:
    """Release the claim. Requires FR-EXP-001b's minimum and FR-EXP-001e's
    payment method - checked here for a message naming what is missing, and
    again by migration 0033's CHECK, which is what binds every writer.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        view = await service.mark_ready(
            administration_id=administration_id,
            expense_id=expense_id,
            actor_user_id=tenant.user_id,
        )
    except ExpenseNotFound as exc:
        raise problem(request, 404, "errors.expense_not_found", reason="expense_not_found") from exc
    except IncompleteExpense as exc:
        raise problem(
            request,
            422,
            "errors.expense_incomplete",
            reason="expense_incomplete",
            missing_fields=list(exc.missing),
        ) from exc
    return _expense_json(view)


# ===========================================================================
# FR-EXP-001d: confirming a claim into the ledger
# ===========================================================================


async def get_expense_posting_service(
    session: AsyncSession = Depends(get_db_session),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> ExpensePostingService:
    """Built over the real LedgerService.

    Not a private path into the posting tables: CLAUDE.md's first
    non-negotiable puts every write behind the ledger's own API, so an expense
    is posted by the same object a manual journal goes through and inherits
    every guarantee it enforces.
    """
    audit = AuditLog(SqlAuditRepository(session))
    return ExpensePostingService(
        repository=SqlCaptureRepository(session),
        ledger=build_ledger_service(session, audit),
        authorization=authorization,
        audit_log=audit,
    )


async def post_expense(
    administration_id: uuid.UUID,
    expense_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: ExpensePostingService = Depends(get_expense_posting_service),
    _: AuthorizationDecision = Depends(
        require_permission(
            # Appendix A's "Post journal entries", not "Submit expenses".
            # §8.4 lets an Expense Submitter submit a claim and not post one,
            # and keeping that separation is the point (SoD).
            "post",
            "journal_entry",
            scope=administration_from_path("administration_id"),
            audit=AuditCategory.POSTING,
        )
    ),
    # IAM-010b: posting is the one action an unverified address cannot
    # perform - see api.auth.email_verification for where that line is drawn.
    __: None = Depends(require_verified_email),
) -> dict[str, object]:
    """Turn a confirmed claim into a ledger entry.

    The shape of the entry follows FR-EXP-001e's payment method - a personally
    paid receipt credits the reimbursement liability, because the business owes
    that person money and FR-EXP-003's run is what pays it.
    """
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    try:
        posted = await service.post(
            administration_id=administration_id,
            expense_id=expense_id,
            actor_user_id=tenant.user_id,
        )
    except ExpenseNotFound as exc:
        raise problem(request, 404, "errors.expense_not_found", reason="expense_not_found") from exc
    except ExpenseAlreadyPosted as exc:
        raise problem(
            request, 409, "errors.expense_already_posted", reason="expense_already_posted"
        ) from exc
    except IncompleteExpense as exc:
        raise problem(
            request,
            422,
            "errors.expense_incomplete",
            reason="expense_incomplete",
            missing_fields=list(exc.missing),
        ) from exc
    except PostingConfigurationMissing as exc:
        # 409 rather than 422: the CLAIM is fine, the administration is not
        # configured to post it. The reason names which mapping is missing so
        # a client can say what an administrator has to do.
        raise problem(
            request,
            409,
            "errors.expense_posting_unconfigured",
            reason="expense_posting_unconfigured",
            purpose=exc.purpose,
        ) from exc
    except NoPurchaseJournal as exc:
        raise problem(
            request, 409, "errors.no_purchase_journal", reason="no_purchase_journal"
        ) from exc
    except NoOpenPeriod as exc:
        raise problem(request, 409, "errors.no_open_period", reason="no_open_period") from exc

    return {
        "expense_id": str(expense_id),
        "journal_entry_id": str(posted.id),
        "entry_number": posted.entry_number,
        "entry_date": posted.entry_date.isoformat(),
        "status": "posted",
    }

"""FR-FRM-005's HTTP surface: question threads between a firm and its client (ADR-111).

    POST /v1/administrations/{id}/questions                              open a thread
    GET  /v1/administrations/{id}/questions?status=open|resolved|all     its threads
    GET  /v1/administrations/{id}/questions/{thread_id}                  thread + messages, read
    POST /v1/administrations/{id}/questions/{thread_id}/messages         reply (flips `awaiting`)
    POST /v1/administrations/{id}/questions/{thread_id}/resolve          resolve
    GET  /v1/firm/inbox?unread=true&limit=20&awaiting=firm|client|any    across every granted client

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

--- Why the thread routes name the administration (a deviation from the build contract) ---

The contract sketched `/v1/questions/{thread_id}`. The authorization library scopes a check to an
administration read from the PATH (api.authz.dependencies.administration_from_path) - so a route
that does not name one cannot be authorized against it without a second, hand-written check, and
it would also slip past FR-FRM-000a's active-client guard and IAM-109's access record. Naming the
administration keeps every thread route on the one library, exactly like every other
administration-scoped route; the thread is then looked up WITHIN that administration, so a thread
id from elsewhere is a 404.

--- Which permission ---

`view administration`: every role on an administration holds it (the baseline in
api.authz.matrix), firm staff through their engagement grant and the client's own users through
theirs. A question thread is the conversation between exactly those people, so "may reach this
administration" is the honest gate - there is no question-specific permission in PRD Appendix A
to require, and inventing one means a new role-catalogue row (ADR-111 lists it as the follow-up).

--- The inbox ---

`/v1/firm/inbox` names no administration: it spans every client the caller may see. It declares
`require_portfolio_permission("view", "administration")` (api.firm.worklist_access, ADR-109) like
every other firm-home route: the switcher's grant join, then authorize() once per administration,
and the inbox reads threads only in the administrations that survive both.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditCategory
from api.authz.dependencies import administration_from_path, require_permission
from api.authz.model import AuthorizationDecision
from api.db import get_db_session
from api.firm.worklist_access import Portfolio, require_portfolio_permission
from api.i18n.http import problem
from api.questions.model import InboxItem, Message, ResourceType, Thread, awaiting_filter
from api.questions.repository import SqlQuestionRepository
from api.questions.service import (
    AdministrationNotFound,
    EmptyText,
    QuestionError,
    QuestionService,
    ResourceIncomplete,
    ResourceNotFound,
    TextTooLong,
    ThreadNotFound,
    ThreadResolved,
)
from api.tenancy import TenantContext, get_tenant_context

_BASE = "/v1/administrations/{administration_id}/questions"
INBOX_PATH = "/v1/firm/inbox"


def register(app: FastAPI) -> None:
    app.add_api_route(_BASE, create_question, methods=["POST"], name="create_question")
    app.add_api_route(_BASE, list_questions, methods=["GET"], name="list_questions")
    app.add_api_route(f"{_BASE}/{{thread_id}}", get_question, methods=["GET"], name="get_question")
    app.add_api_route(
        f"{_BASE}/{{thread_id}}/messages",
        post_question_message,
        methods=["POST"],
        name="post_question_message",
    )
    app.add_api_route(
        f"{_BASE}/{{thread_id}}/resolve",
        resolve_question,
        methods=["POST"],
        name="resolve_question",
    )
    app.add_api_route(INBOX_PATH, firm_inbox, methods=["GET"], name="firm_inbox")


async def get_question_service(
    session: AsyncSession = Depends(get_db_session),
) -> QuestionService:
    return QuestionService(SqlQuestionRepository(session))


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def thread_json(thread: Thread) -> dict[str, object]:
    return {
        "id": str(thread.id),
        "administration_id": str(thread.administration_id),
        "subject": thread.subject,
        "status": thread.status.value,
        "awaiting": thread.awaiting.value,
        "resource_type": thread.resource_type.value if thread.resource_type else None,
        "resource_id": str(thread.resource_id) if thread.resource_id else None,
        "created_by_user_id": str(thread.created_by_user_id),
        "created_at": thread.created_at.isoformat(),
        "last_message_at": thread.last_message_at.isoformat(),
        "resolved_at": thread.resolved_at.isoformat() if thread.resolved_at else None,
        "resolved_by_user_id": (
            str(thread.resolved_by_user_id) if thread.resolved_by_user_id else None
        ),
        "unread": thread.unread,
    }


def message_json(message: Message) -> dict[str, object]:
    return {
        "id": str(message.id),
        "thread_id": str(message.thread_id),
        "author_user_id": str(message.author_user_id),
        "author_email": message.author_email,
        "author_side": message.author_side.value,
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }


def inbox_item_json(item: InboxItem) -> dict[str, object]:
    return {
        "thread_id": str(item.thread_id),
        "administration_id": str(item.administration_id),
        "display_name": item.display_name,
        "subject": item.subject,
        "excerpt": item.excerpt,
        "last_message_at": item.last_message_at.isoformat(),
        "awaiting": item.awaiting.value,
        "unread": item.unread,
    }


def _require_user(request: Request, tenant: TenantContext) -> uuid.UUID:
    if tenant.user_id is None:
        raise problem(request, 403, "errors.not_authenticated", reason="no_authenticated_user")
    return tenant.user_id


def _refusal(request: Request, exc: QuestionError) -> Exception:
    if isinstance(exc, AdministrationNotFound):
        return problem(
            request, 404, "errors.administration_not_found", reason="administration_not_found"
        )
    if isinstance(exc, ThreadNotFound):
        return problem(
            request, 404, "errors.question_thread_not_found", reason="question_thread_not_found"
        )
    if isinstance(exc, ThreadResolved):
        return problem(
            request, 409, "errors.question_thread_resolved", reason="question_thread_resolved"
        )
    if isinstance(exc, EmptyText):
        return problem(request, 422, "errors.question_text_empty", reason="question_text_empty")
    if isinstance(exc, TextTooLong):
        return problem(
            request, 422, "errors.question_text_too_long", reason="question_text_too_long"
        )
    if isinstance(exc, ResourceIncomplete):
        return problem(
            request,
            422,
            "errors.question_resource_incomplete",
            reason="question_resource_incomplete",
        )
    if isinstance(exc, ResourceNotFound):
        return problem(
            request,
            422,
            "errors.question_resource_not_found",
            reason="question_resource_not_found",
        )
    raise exc  # pragma: no cover - every QuestionError is mapped above


def _participate(
    audit: AuditCategory | None = None,
) -> Callable[..., Awaitable[AuthorizationDecision]]:
    return require_permission(
        "view",
        "administration",
        scope=administration_from_path("administration_id"),
        audit=audit,
    )


# Built once at import rather than in each route's argument defaults.
_READ = _participate()
# The inbox: every administration the caller holds a live grant on and is authorized to take part
# in, one authorize() per administration (ADR-109).
_INBOX = require_portfolio_permission("view", "administration")
# A question is about a transaction or document: reading one is reading about the books.
_READ_THREAD = _participate(AuditCategory.FINANCIAL_READ)
# IAM-090 has no "conversation" category; a question changes no books, settings or access, and
# CONFIGURATION is the category this codebase uses for tenant-data changes of that kind (bank
# reconciliation, the switcher). ADR-111.
_WRITE = _participate(AuditCategory.CONFIGURATION)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class CreateQuestionBody(BaseModel):
    subject: str
    body: str
    resource_type: ResourceType | None = None
    resource_id: uuid.UUID | None = None


async def create_question(
    administration_id: uuid.UUID,
    payload: CreateQuestionBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuestionService = Depends(get_question_service),
    _: AuthorizationDecision = Depends(_WRITE),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    try:
        thread, message = await service.create_thread(
            administration_id=administration_id,
            session_organization_id=tenant.organization_id,
            user_id=user_id,
            subject=payload.subject,
            body=payload.body,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
        )
    except QuestionError as exc:
        raise _refusal(request, exc) from exc
    return {**thread_json(thread), "messages": [message_json(message)]}


async def list_questions(
    administration_id: uuid.UUID,
    request: Request,
    status: Literal["open", "resolved", "all"] = Query(default="open"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuestionService = Depends(get_question_service),
    _: AuthorizationDecision = Depends(_READ),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    try:
        threads = await service.list_threads(
            administration_id=administration_id,
            session_organization_id=tenant.organization_id,
            user_id=user_id,
            status=status,
        )
    except QuestionError as exc:
        raise _refusal(request, exc) from exc
    return {"threads": [thread_json(thread) for thread in threads]}


async def get_question(
    administration_id: uuid.UUID,
    thread_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuestionService = Depends(get_question_service),
    _: AuthorizationDecision = Depends(_READ_THREAD),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    try:
        thread, messages = await service.open_thread(
            administration_id=administration_id,
            thread_id=thread_id,
            session_organization_id=tenant.organization_id,
            user_id=user_id,
        )
    except QuestionError as exc:
        raise _refusal(request, exc) from exc
    return {**thread_json(thread), "messages": [message_json(m) for m in messages]}


class PostMessageBody(BaseModel):
    body: str


async def post_question_message(
    administration_id: uuid.UUID,
    thread_id: uuid.UUID,
    payload: PostMessageBody,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuestionService = Depends(get_question_service),
    _: AuthorizationDecision = Depends(_WRITE),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    try:
        thread, message = await service.post_message(
            administration_id=administration_id,
            thread_id=thread_id,
            session_organization_id=tenant.organization_id,
            user_id=user_id,
            body=payload.body,
        )
    except QuestionError as exc:
        raise _refusal(request, exc) from exc
    return {**message_json(message), "thread": thread_json(thread)}


async def resolve_question(
    administration_id: uuid.UUID,
    thread_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    service: QuestionService = Depends(get_question_service),
    _: AuthorizationDecision = Depends(_WRITE),
) -> dict[str, object]:
    user_id = _require_user(request, tenant)
    try:
        thread = await service.resolve(
            administration_id=administration_id,
            thread_id=thread_id,
            session_organization_id=tenant.organization_id,
            user_id=user_id,
        )
    except QuestionError as exc:
        raise _refusal(request, exc) from exc
    return thread_json(thread)


async def firm_inbox(
    request: Request,
    unread: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    awaiting: Literal["firm", "client", "any"] = Query(default="any"),
    service: QuestionService = Depends(get_question_service),
    portfolio: Portfolio = Depends(_INBOX),
) -> dict[str, object]:
    """`awaiting=firm` is "replies to you" (the firm home's panel and badge); `client` is the
    firm's own questions still waiting on the client; `any` (default) is both."""
    inbox = await service.inbox(
        user_id=portfolio.user_id,
        session_organization_id=portfolio.organization_id,
        administration_ids=portfolio.administration_ids,
        unread_only=unread,
        limit=limit,
        awaiting=awaiting_filter(awaiting),
    )
    return {
        "unread_count": inbox.unread_count,
        "items": [inbox_item_json(item) for item in inbox.items],
    }

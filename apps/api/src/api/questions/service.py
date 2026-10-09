"""FR-FRM-005: opening, answering, reading and resolving question threads (ADR-111).

The routes authorize each request against the administration it names (require_permission,
`view administration` - see api.questions.routes for why that permission). This service adds the
rules of the conversation itself:

  * the writer's side comes from the session, never from the request (api.questions.model);
  * a record a thread is about must exist in the same administration;
  * a reply flips `awaiting` to the other side; a resolved thread takes no more messages;
  * the cross-client inbox lists threads only in the administrations it is handed - the route's
    `Portfolio` (api.firm.worklist_access, ADR-109): the switcher's grant join, then authorize()
    per administration, RLS having already narrowed the session to the firm's engaged clients.

Nothing here e-mails anybody. A firm message is picked up by the notification sweep
(api.questions.notifications), which needs to see the client organization's own grants - rows a
firm's session cannot read, by design.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from api.questions.model import (
    BODY_MAX,
    INBOX_LIMIT_MAX,
    SUBJECT_MAX,
    Inbox,
    Message,
    ResourceType,
    Side,
    Thread,
    ThreadStatus,
    awaiting_after,
    clean_text,
    side_for,
)
from api.questions.repository import QuestionRepository

StatusFilter = Literal["open", "resolved", "all"]


class QuestionError(Exception):
    """Base for the refusals the routes turn into problem responses."""


class AdministrationNotFound(QuestionError):
    pass


class ThreadNotFound(QuestionError):
    pass


class ThreadResolved(QuestionError):
    pass


class EmptyText(QuestionError):
    pass


class TextTooLong(QuestionError):
    pass


class ResourceIncomplete(QuestionError):
    pass


class ResourceNotFound(QuestionError):
    pass


def _statuses(status: StatusFilter) -> tuple[ThreadStatus, ...]:
    if status == "all":
        return (ThreadStatus.OPEN, ThreadStatus.RESOLVED)
    return (ThreadStatus(status),)


def _text(value: str, *, limit: int) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        raise EmptyText()
    if len(cleaned) > limit:
        raise TextTooLong()
    return cleaned


class QuestionService:
    def __init__(self, repository: QuestionRepository) -> None:
        self._repository = repository

    async def _side(
        self, *, administration_id: uuid.UUID, session_organization_id: uuid.UUID
    ) -> tuple[Side, uuid.UUID]:
        owner = await self._repository.owning_organization(administration_id)
        if owner is None:
            raise AdministrationNotFound()
        return (
            side_for(session_organization_id=session_organization_id, owning_organization_id=owner),
            owner,
        )

    async def create_thread(
        self,
        *,
        administration_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        user_id: uuid.UUID,
        subject: str,
        body: str,
        resource_type: ResourceType | None,
        resource_id: uuid.UUID | None,
    ) -> tuple[Thread, Message]:
        subject = _text(subject, limit=SUBJECT_MAX)
        body = _text(body, limit=BODY_MAX)
        if (resource_type is None) != (resource_id is None):
            raise ResourceIncomplete()
        side, owner = await self._side(
            administration_id=administration_id, session_organization_id=session_organization_id
        )
        if (
            resource_type is not None
            and resource_id is not None
            and not await self._repository.resource_exists(
                administration_id=administration_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        ):
            raise ResourceNotFound()

        thread = await self._repository.insert_thread(
            organization_id=owner,
            administration_id=administration_id,
            subject=subject,
            awaiting=awaiting_after(side),
            resource_type=resource_type,
            resource_id=resource_id,
            created_by_user_id=user_id,
        )
        message = await self._repository.insert_message(
            thread=thread, author_user_id=user_id, author_side=side, body=body
        )
        # Writing is reading: the author has seen everything in the thread so far.
        await self._repository.mark_read(thread=thread, user_id=user_id)
        return thread, message

    async def list_threads(
        self,
        *,
        administration_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        user_id: uuid.UUID,
        status: StatusFilter,
    ) -> list[Thread]:
        side, _ = await self._side(
            administration_id=administration_id, session_organization_id=session_organization_id
        )
        return await self._repository.list_threads(
            administration_id=administration_id,
            statuses=_statuses(status),
            user_id=user_id,
            caller_side=side,
        )

    async def open_thread(
        self,
        *,
        administration_id: uuid.UUID,
        thread_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[Thread, list[Message]]:
        """The thread and its messages, and a read receipt for the caller (contract: "marks
        read"). The returned thread reports unread=False - it has just been read."""
        side, _ = await self._side(
            administration_id=administration_id, session_organization_id=session_organization_id
        )
        thread = await self._repository.get_thread(
            administration_id=administration_id,
            thread_id=thread_id,
            user_id=user_id,
            caller_side=side,
        )
        if thread is None:
            raise ThreadNotFound()
        messages = await self._repository.messages(thread_id=thread.id)
        await self._repository.mark_read(thread=thread, user_id=user_id)
        return _as_read(thread), messages

    async def post_message(
        self,
        *,
        administration_id: uuid.UUID,
        thread_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        user_id: uuid.UUID,
        body: str,
    ) -> tuple[Thread, Message]:
        body = _text(body, limit=BODY_MAX)
        side, _ = await self._side(
            administration_id=administration_id, session_organization_id=session_organization_id
        )
        thread = await self._repository.get_thread(
            administration_id=administration_id,
            thread_id=thread_id,
            user_id=user_id,
            caller_side=side,
            for_update=True,
        )
        if thread is None:
            raise ThreadNotFound()
        if thread.status is ThreadStatus.RESOLVED:
            raise ThreadResolved()

        message = await self._repository.insert_message(
            thread=thread, author_user_id=user_id, author_side=side, body=body
        )
        awaiting = awaiting_after(side)
        await self._repository.record_message_on_thread(
            thread_id=thread.id, awaiting=awaiting, at=message.created_at
        )
        await self._repository.mark_read(thread=thread, user_id=user_id)
        updated = await self._repository.get_thread(
            administration_id=administration_id,
            thread_id=thread_id,
            user_id=user_id,
            caller_side=side,
        )
        return (_as_read(updated) if updated is not None else thread), message

    async def resolve(
        self,
        *,
        administration_id: uuid.UUID,
        thread_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Thread:
        """Either side may resolve. Resolving a thread that is already resolved changes nothing
        and returns it as it is - the caller's intent already holds."""
        side, _ = await self._side(
            administration_id=administration_id, session_organization_id=session_organization_id
        )
        thread = await self._repository.get_thread(
            administration_id=administration_id,
            thread_id=thread_id,
            user_id=user_id,
            caller_side=side,
            for_update=True,
        )
        if thread is None:
            raise ThreadNotFound()
        if thread.status is ThreadStatus.OPEN:
            await self._repository.resolve(thread_id=thread.id, user_id=user_id)
            refreshed = await self._repository.get_thread(
                administration_id=administration_id,
                thread_id=thread_id,
                user_id=user_id,
                caller_side=side,
            )
            if refreshed is not None:
                thread = refreshed
        return thread

    async def inbox(
        self,
        *,
        user_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        administration_ids: Sequence[uuid.UUID],
        unread_only: bool,
        limit: int,
    ) -> Inbox:
        """`administration_ids` is the caller's authorized portfolio; nothing outside it is read."""
        limit = max(1, min(limit, INBOX_LIMIT_MAX))
        return await self._repository.inbox(
            user_id=user_id,
            session_organization_id=session_organization_id,
            administration_ids=administration_ids,
            unread_only=unread_only,
            limit=limit,
        )


def _as_read(thread: Thread) -> Thread:
    return replace(thread, unread=False)

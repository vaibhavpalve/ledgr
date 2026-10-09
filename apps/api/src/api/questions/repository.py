"""SQL for question threads (migration 0080), on the request's tenant-scoped session.

Every statement runs under RLS (`app.has_administration_access`), and every statement that names
a thread also names its administration, so a thread id from another administration - even one the
session can reach - is simply not found.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.questions.model import (
    Inbox,
    InboxItem,
    Message,
    ResourceType,
    Side,
    Thread,
    ThreadStatus,
    excerpt,
)

_THREAD_COLUMNS = (
    "t.id, t.organization_id, t.administration_id, t.subject, t.status, t.awaiting, "
    "t.resource_type, t.resource_id, t.created_by_user_id, t.created_at, t.last_message_at, "
    "t.resolved_at, t.resolved_by_user_id"
)

# The caller's last read of thread t, or -infinity.
_LAST_READ = (
    "COALESCE((SELECT max(r.read_at) FROM question_read r "
    "          WHERE r.thread_id = t.id AND r.user_id = :user_id), '-infinity'::timestamptz)"
)


def _unread(side_sql: str) -> str:
    """A message from the OTHER side newer than the caller's last read. A colleague's message on
    the caller's own side is not something waiting for them."""
    return (
        "EXISTS (SELECT 1 FROM question_message um "
        "         WHERE um.thread_id = t.id "
        f"          AND um.author_side <> {side_sql} "
        f"          AND um.created_at > {_LAST_READ})"
    )


# The caller's side for each row of the inbox, which spans administrations: the session's own
# organization owns it (client) or reaches it through an engagement (firm). See api.questions.model.
_SIDE_PER_ROW = (
    "(CASE WHEN a.organization_id = cast(:org_id as uuid) THEN 'client' ELSE 'firm' END)"
)

# Each record kind a thread may be about, and its table. A closed mapping: the table name is
# never taken from the request.
_RESOURCE_TABLES: dict[ResourceType, str] = {
    ResourceType.BANK_TRANSACTION: "bank_transaction",
    ResourceType.DOCUMENT: "document",
    ResourceType.EXPENSE: "expense",
    ResourceType.SALES_INVOICE: "sales_invoice",
}


def _thread(row: Any, *, unread: bool = False) -> Thread:
    return Thread(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        subject=row.subject,
        status=ThreadStatus(row.status),
        awaiting=Side(row.awaiting),
        resource_type=ResourceType(row.resource_type) if row.resource_type else None,
        resource_id=row.resource_id,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        last_message_at=row.last_message_at,
        resolved_at=row.resolved_at,
        resolved_by_user_id=row.resolved_by_user_id,
        unread=unread,
    )


class QuestionRepository(Protocol):
    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def resource_exists(
        self,
        *,
        administration_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
    ) -> bool: ...

    async def insert_thread(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        subject: str,
        awaiting: Side,
        resource_type: ResourceType | None,
        resource_id: uuid.UUID | None,
        created_by_user_id: uuid.UUID,
    ) -> Thread: ...

    async def insert_message(
        self,
        *,
        thread: Thread,
        author_user_id: uuid.UUID,
        author_side: Side,
        body: str,
    ) -> Message: ...

    async def record_message_on_thread(
        self, *, thread_id: uuid.UUID, awaiting: Side, at: datetime
    ) -> None: ...

    async def mark_read(self, *, thread: Thread, user_id: uuid.UUID) -> None: ...

    async def get_thread(
        self,
        *,
        administration_id: uuid.UUID,
        thread_id: uuid.UUID,
        user_id: uuid.UUID,
        caller_side: Side,
        for_update: bool = False,
    ) -> Thread | None: ...

    async def list_threads(
        self,
        *,
        administration_id: uuid.UUID,
        statuses: Sequence[ThreadStatus],
        user_id: uuid.UUID,
        caller_side: Side,
    ) -> list[Thread]: ...

    async def messages(self, *, thread_id: uuid.UUID) -> list[Message]: ...

    async def resolve(self, *, thread_id: uuid.UUID, user_id: uuid.UUID) -> None: ...

    async def inbox(
        self,
        *,
        user_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        administration_ids: Sequence[uuid.UUID],
        unread_only: bool,
        limit: int,
        awaiting: Side | None = None,
    ) -> Inbox: ...


class SqlQuestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        value = result.scalar_one_or_none()
        return value if value is None or isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    async def resource_exists(
        self,
        *,
        administration_id: uuid.UUID,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
    ) -> bool:
        table = _RESOURCE_TABLES[resource_type]
        result = await self._session.execute(
            text(f"SELECT 1 FROM {table} WHERE id = :id AND administration_id = :admin"),  # noqa: S608
            {"id": str(resource_id), "admin": str(administration_id)},
        )
        return result.first() is not None

    async def insert_thread(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        subject: str,
        awaiting: Side,
        resource_type: ResourceType | None,
        resource_id: uuid.UUID | None,
        created_by_user_id: uuid.UUID,
    ) -> Thread:
        result = await self._session.execute(
            text(
                "INSERT INTO question_thread AS t (organization_id, administration_id, subject, "
                "  awaiting, resource_type, resource_id, created_by_user_id) "
                "VALUES (:org, :admin, :subject, :awaiting, :resource_type, :resource_id, :user) "
                f"RETURNING {_THREAD_COLUMNS}"
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "subject": subject,
                "awaiting": awaiting.value,
                "resource_type": resource_type.value if resource_type else None,
                "resource_id": str(resource_id) if resource_id else None,
                "user": str(created_by_user_id),
            },
        )
        return _thread(result.one())

    async def insert_message(
        self,
        *,
        thread: Thread,
        author_user_id: uuid.UUID,
        author_side: Side,
        body: str,
    ) -> Message:
        result = await self._session.execute(
            text(
                "INSERT INTO question_message (thread_id, organization_id, administration_id, "
                "  author_user_id, author_side, body) "
                "VALUES (:thread, :org, :admin, :user, :side, :body) "
                "RETURNING id, thread_id, author_user_id, author_side, body, created_at, "
                "  (SELECT email FROM users WHERE id = :user) AS author_email"
            ),
            {
                "thread": str(thread.id),
                "org": str(thread.organization_id),
                "admin": str(thread.administration_id),
                "user": str(author_user_id),
                "side": author_side.value,
                "body": body,
            },
        )
        return _message(result.one())

    async def record_message_on_thread(
        self, *, thread_id: uuid.UUID, awaiting: Side, at: datetime
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE question_thread SET awaiting = :awaiting, "
                "  last_message_at = greatest(last_message_at, :at) "
                "WHERE id = :id"
            ),
            {"awaiting": awaiting.value, "at": at, "id": str(thread_id)},
        )

    async def mark_read(self, *, thread: Thread, user_id: uuid.UUID) -> None:
        await self._session.execute(
            text(
                "INSERT INTO question_read "
                "  (thread_id, organization_id, administration_id, user_id) "
                "VALUES (:thread, :org, :admin, :user)"
            ),
            {
                "thread": str(thread.id),
                "org": str(thread.organization_id),
                "admin": str(thread.administration_id),
                "user": str(user_id),
            },
        )

    async def get_thread(
        self,
        *,
        administration_id: uuid.UUID,
        thread_id: uuid.UUID,
        user_id: uuid.UUID,
        caller_side: Side,
        for_update: bool = False,
    ) -> Thread | None:
        # FOR UPDATE serialises two writers on one thread (two replies, or a reply racing a
        # resolve), so `awaiting` always reflects the message that really came last.
        lock = " FOR UPDATE OF t" if for_update else ""
        result = await self._session.execute(
            text(
                f"SELECT {_THREAD_COLUMNS}, {_unread(':side')} AS unread "
                "FROM question_thread t WHERE t.id = :id AND t.administration_id = :admin" + lock
            ),
            {
                "id": str(thread_id),
                "admin": str(administration_id),
                "user_id": str(user_id),
                "side": caller_side.value,
            },
        )
        row = result.first()
        return None if row is None else _thread(row, unread=bool(row.unread))

    async def list_threads(
        self,
        *,
        administration_id: uuid.UUID,
        statuses: Sequence[ThreadStatus],
        user_id: uuid.UUID,
        caller_side: Side,
    ) -> list[Thread]:
        result = await self._session.execute(
            text(
                f"SELECT {_THREAD_COLUMNS}, {_unread(':side')} AS unread "
                "FROM question_thread t "
                "WHERE t.administration_id = :admin AND t.status = ANY(cast(:statuses as text[])) "
                "ORDER BY t.last_message_at DESC, t.id"
            ),
            {
                "admin": str(administration_id),
                "statuses": [s.value for s in statuses],
                "user_id": str(user_id),
                "side": caller_side.value,
            },
        )
        return [_thread(row, unread=bool(row.unread)) for row in result]

    async def messages(self, *, thread_id: uuid.UUID) -> list[Message]:
        result = await self._session.execute(
            text(
                "SELECT m.id, m.thread_id, m.author_user_id, m.author_side, m.body, m.created_at, "
                "       u.email AS author_email "
                "FROM question_message m LEFT JOIN users u ON u.id = m.author_user_id "
                "WHERE m.thread_id = :thread ORDER BY m.created_at, m.id"
            ),
            {"thread": str(thread_id)},
        )
        return [_message(row) for row in result]

    async def resolve(self, *, thread_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._session.execute(
            text(
                "UPDATE question_thread SET status = 'resolved', resolved_at = now(), "
                "  resolved_by_user_id = :user "
                "WHERE id = :id AND status = 'open'"
            ),
            {"id": str(thread_id), "user": str(user_id)},
        )

    async def inbox(
        self,
        *,
        user_id: uuid.UUID,
        session_organization_id: uuid.UUID,
        administration_ids: Sequence[uuid.UUID],
        unread_only: bool,
        limit: int,
        awaiting: Side | None = None,
    ) -> Inbox:
        """`awaiting` narrows both the items and `unread_count`, so a badge read with
        `awaiting=firm` counts only client replies, never the firm's own questions still out."""
        if not administration_ids:
            return Inbox(unread_count=0, items=())
        unread = _unread(_SIDE_PER_ROW)
        params: dict[str, object] = {
            "user_id": str(user_id),
            "org_id": str(session_organization_id),
            "ids": [str(i) for i in administration_ids],
            "limit": limit,
        }
        base = (
            "FROM question_thread t JOIN administration a ON a.id = t.administration_id "
            "WHERE t.status = 'open' AND t.administration_id = ANY(cast(:ids as uuid[]))"
        )
        if awaiting is not None:
            base += " AND t.awaiting = :awaiting"
            params["awaiting"] = awaiting.value
        count = await self._session.execute(text(f"SELECT count(*) {base} AND {unread}"), params)
        unread_count = int(count.scalar_one())

        filter_sql = f" AND {unread}" if unread_only else ""
        rows = await self._session.execute(
            text(
                "SELECT t.id, t.administration_id, coalesce(a.trade_name, a.legal_name) AS name, "
                "       t.subject, t.last_message_at, t.awaiting, "
                f"      {unread} AS unread, "
                "       (SELECT m.body FROM question_message m WHERE m.thread_id = t.id "
                "         ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_body "
                f"{base}{filter_sql} "
                "ORDER BY t.last_message_at DESC, t.id LIMIT :limit"
            ),
            params,
        )
        items = tuple(
            InboxItem(
                thread_id=row.id,
                administration_id=row.administration_id,
                display_name=row.name,
                subject=row.subject,
                excerpt=excerpt(row.last_body or ""),
                last_message_at=row.last_message_at,
                awaiting=Side(row.awaiting),
                unread=bool(row.unread),
            )
            for row in rows
        )
        return Inbox(unread_count=unread_count, items=items)


def _message(row: Any) -> Message:
    return Message(
        id=row.id,
        thread_id=row.thread_id,
        author_user_id=row.author_user_id,
        author_email=row.author_email,
        author_side=Side(row.author_side),
        body=row.body,
        created_at=row.created_at,
    )

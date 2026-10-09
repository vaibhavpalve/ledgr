"""FR-FRM-005's question threads, as values and pure rules (ADR-111).

    FR-FRM-005  Question/answer threads attached to a specific transaction or document,
                resolvable, with client notification.

--- Two sides, decided by the session, never by the request ---

A thread is a conversation between the people who keep an administration's books for a living
(the FIRM, reaching the administration through an active engagement) and the people whose books
they are (the CLIENT: users of the organization that owns the administration). Which side a
writer is on is derived from the tenant session the request runs in:

  * the session's organization OWNS the administration      -> client side
  * the session's organization is an engaged firm            -> firm side

Those are exactly the two ways `app.has_administration_access` (0001) admits a session to an
administration's rows, so there is no third case to guess about, and a request body cannot
claim to be the other side. It mirrors the split api.authz.service's profile cap already draws
between a client's own grants and a firm's (IAM-100, IAM-107).

--- Whose move it is ---

`awaiting` is always the side that did NOT write the last message. Opening a thread is writing
its first message, so a firm question awaits the client and a client question awaits the firm.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

#: Longest subject and body accepted (mirrored by CHECK constraints in 0080).
SUBJECT_MAX = 200
BODY_MAX = 5000
#: The inbox's excerpt length (contract: "first 120 chars").
EXCERPT_LENGTH = 120
INBOX_LIMIT_MAX = 100


class Side(enum.Enum):
    FIRM = "firm"
    CLIENT = "client"

    @property
    def other(self) -> Side:
        return Side.CLIENT if self is Side.FIRM else Side.FIRM


class ThreadStatus(enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class ResourceType(enum.Enum):
    """What a thread may be about. Each names a tenant-scoped table with `id` and
    `administration_id`; the repository checks the record is in the thread's administration.
    """

    BANK_TRANSACTION = "bank_transaction"
    DOCUMENT = "document"
    EXPENSE = "expense"
    SALES_INVOICE = "sales_invoice"


def side_for(*, session_organization_id: uuid.UUID, owning_organization_id: uuid.UUID) -> Side:
    """The writer's side: the owning organization's own session is the client; any other
    session that can reach the administration at all is an engaged firm (see module docstring).
    """
    return Side.CLIENT if session_organization_id == owning_organization_id else Side.FIRM


def awaiting_after(author: Side) -> Side:
    return author.other


def excerpt(body: str, length: int = EXCERPT_LENGTH) -> str:
    """The first `length` characters, whitespace collapsed so a multi-line message reads as one
    line in a list."""
    flat = " ".join(body.split())
    return flat[:length]


def clean_text(value: str) -> str:
    """Leading and trailing whitespace removed; an all-whitespace text becomes empty."""
    return value.strip()


@dataclass(frozen=True, slots=True)
class Thread:
    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    subject: str
    status: ThreadStatus
    awaiting: Side
    resource_type: ResourceType | None
    resource_id: uuid.UUID | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    last_message_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: uuid.UUID | None
    #: For the caller: a message from the other side newer than their last read.
    unread: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    id: uuid.UUID
    thread_id: uuid.UUID
    author_user_id: uuid.UUID
    author_email: str | None
    author_side: Side
    body: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InboxItem:
    thread_id: uuid.UUID
    administration_id: uuid.UUID
    display_name: str
    subject: str
    excerpt: str
    last_message_at: datetime
    awaiting: Side
    unread: bool


@dataclass(frozen=True, slots=True)
class Inbox:
    unread_count: int
    items: tuple[InboxItem, ...]

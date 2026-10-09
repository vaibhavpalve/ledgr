"""ADR-111: QuestionService against an in-memory repository.

The repository fake keeps the same promises the SQL one does - a thread is found only within its
own administration, messages are append-only, the latest read wins - so these tests are about
the conversation's rules: sides, whose move it is, resolution, resource checks and the inbox's
portfolio the route hands it (authorization itself is api.firm.worklist_access's, ADR-109).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from api.questions.model import (
    Inbox,
    InboxItem,
    Message,
    ResourceType,
    Side,
    Thread,
    ThreadStatus,
    awaiting_filter,
    excerpt,
)
from api.questions.routes import CreateQuestionBody
from api.questions.service import (
    AdministrationNotFound,
    EmptyText,
    QuestionService,
    ResourceIncomplete,
    ResourceNotFound,
    TextTooLong,
    ThreadNotFound,
    ThreadResolved,
)

CLIENT_ORG = uuid.uuid4()
OTHER_CLIENT_ORG = uuid.uuid4()
FIRM_ORG = uuid.uuid4()
ADMIN = uuid.uuid4()
OTHER_ADMIN = uuid.uuid4()
ACCOUNTANT = uuid.uuid4()
OWNER = uuid.uuid4()


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

    def tick(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class FakeQuestionRepository:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        # administration -> owning organization, as RLS would show it to the session.
        self.owners: dict[uuid.UUID, uuid.UUID] = {ADMIN: CLIENT_ORG, OTHER_ADMIN: OTHER_CLIENT_ORG}
        self.names: dict[uuid.UUID, str] = {ADMIN: "Bakker B.V.", OTHER_ADMIN: "De Vries"}
        self.resources: set[tuple[uuid.UUID, ResourceType, uuid.UUID]] = set()
        self.threads: dict[uuid.UUID, Thread] = {}
        self.message_log: list[Message] = []
        self.reads: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []
        self.inbox_awaiting: list[Side | None] = []

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.owners.get(administration_id)

    async def resource_exists(
        self, *, administration_id: uuid.UUID, resource_type: ResourceType, resource_id: uuid.UUID
    ) -> bool:
        return (administration_id, resource_type, resource_id) in self.resources

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
        now = self.clock.tick()
        thread = Thread(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            subject=subject,
            status=ThreadStatus.OPEN,
            awaiting=awaiting,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by_user_id=created_by_user_id,
            created_at=now,
            last_message_at=now,
            resolved_at=None,
            resolved_by_user_id=None,
        )
        self.threads[thread.id] = thread
        return thread

    async def insert_message(
        self, *, thread: Thread, author_user_id: uuid.UUID, author_side: Side, body: str
    ) -> Message:
        assert self.threads[thread.id].status is ThreadStatus.OPEN  # the 0080 trigger
        message = Message(
            id=uuid.uuid4(),
            thread_id=thread.id,
            author_user_id=author_user_id,
            author_email=None,
            author_side=author_side,
            body=body,
            created_at=self.clock.now,
        )
        self.message_log.append(message)
        return message

    async def record_message_on_thread(
        self, *, thread_id: uuid.UUID, awaiting: Side, at: datetime
    ) -> None:
        thread = self.threads[thread_id]
        self.threads[thread_id] = replace(
            thread, awaiting=awaiting, last_message_at=max(thread.last_message_at, at)
        )

    async def mark_read(self, *, thread: Thread, user_id: uuid.UUID) -> None:
        self.reads.append((thread.id, user_id, self.clock.now))

    def _unread(self, thread: Thread, user_id: uuid.UUID, side: Side) -> bool:
        last = max(
            (at for tid, uid, at in self.reads if tid == thread.id and uid == user_id),
            default=None,
        )
        return any(
            m.thread_id == thread.id
            and m.author_side is not side
            and (last is None or m.created_at > last)
            for m in self.message_log
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
        thread = self.threads.get(thread_id)
        if thread is None or thread.administration_id != administration_id:
            return None
        return replace(thread, unread=self._unread(thread, user_id, caller_side))

    async def list_threads(
        self,
        *,
        administration_id: uuid.UUID,
        statuses: Sequence[ThreadStatus],
        user_id: uuid.UUID,
        caller_side: Side,
    ) -> list[Thread]:
        return [
            replace(t, unread=self._unread(t, user_id, caller_side))
            for t in sorted(self.threads.values(), key=lambda t: t.last_message_at, reverse=True)
            if t.administration_id == administration_id and t.status in statuses
        ]

    async def messages(self, *, thread_id: uuid.UUID) -> list[Message]:
        return [m for m in self.message_log if m.thread_id == thread_id]

    async def resolve(self, *, thread_id: uuid.UUID, user_id: uuid.UUID) -> None:
        thread = self.threads[thread_id]
        self.threads[thread_id] = replace(
            thread,
            status=ThreadStatus.RESOLVED,
            resolved_at=self.clock.now,
            resolved_by_user_id=user_id,
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
        self.inbox_awaiting.append(awaiting)
        items = []
        for t in sorted(self.threads.values(), key=lambda t: t.last_message_at, reverse=True):
            if t.status is not ThreadStatus.OPEN or t.administration_id not in administration_ids:
                continue
            if awaiting is not None and t.awaiting is not awaiting:
                continue
            side = (
                Side.CLIENT
                if self.owners[t.administration_id] == session_organization_id
                else Side.FIRM
            )
            unread = self._unread(t, user_id, side)
            last = [m for m in self.message_log if m.thread_id == t.id][-1]
            items.append(
                InboxItem(
                    thread_id=t.id,
                    administration_id=t.administration_id,
                    display_name=self.names[t.administration_id],
                    subject=t.subject,
                    excerpt=excerpt(last.body),
                    last_message_at=t.last_message_at,
                    awaiting=t.awaiting,
                    unread=unread,
                )
            )
        unread_count = sum(1 for i in items if i.unread)
        if unread_only:
            items = [i for i in items if i.unread]
        return Inbox(unread_count=unread_count, items=tuple(items[:limit]))


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def repo(clock: _Clock) -> FakeQuestionRepository:
    return FakeQuestionRepository(clock)


@pytest.fixture
def service(repo: FakeQuestionRepository) -> QuestionService:
    return QuestionService(repo)


async def _firm_asks(service: QuestionService, **overrides: object) -> Thread:
    args: dict[str, object] = {
        "administration_id": ADMIN,
        "session_organization_id": FIRM_ORG,
        "user_id": ACCOUNTANT,
        "subject": "Betaling KPN",
        "body": "Waar is deze betaling van 12 september voor?",
        "resource_type": None,
        "resource_id": None,
    }
    args.update(overrides)
    thread, _ = await service.create_thread(**args)  # type: ignore[arg-type]
    return thread


async def test_a_firm_question_awaits_the_client_and_is_written_by_the_firm(
    service: QuestionService, repo: FakeQuestionRepository
) -> None:
    thread = await _firm_asks(service)
    assert thread.awaiting is Side.CLIENT
    assert thread.organization_id == CLIENT_ORG  # the owner, not the firm
    [message] = repo.message_log
    assert message.author_side is Side.FIRM
    # Writing is reading.
    assert (thread.id, ACCOUNTANT) in {(t, u) for t, u, _ in repo.reads}


async def test_a_client_question_awaits_the_firm(service: QuestionService) -> None:
    thread = await _firm_asks(service, session_organization_id=CLIENT_ORG, user_id=OWNER)
    assert thread.awaiting is Side.FIRM


def test_the_side_cannot_be_claimed_in_the_body() -> None:
    body = CreateQuestionBody.model_validate(
        {"subject": "s", "body": "b", "author_side": "client", "awaiting": "firm"}
    )
    assert not hasattr(body, "author_side")
    assert not hasattr(body, "awaiting")


async def test_a_reply_flips_whose_move_it_is(
    service: QuestionService, repo: FakeQuestionRepository, clock: _Clock
) -> None:
    thread = await _firm_asks(service)
    clock.tick()
    replied, message = await service.post_message(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=CLIENT_ORG,
        user_id=OWNER,
        body="Dat is het abonnement.",
    )
    assert message.author_side is Side.CLIENT
    assert replied.awaiting is Side.FIRM
    assert replied.last_message_at > thread.last_message_at

    clock.tick()
    again, _ = await service.post_message(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=FIRM_ORG,
        user_id=ACCOUNTANT,
        body="Dank u.",
    )
    assert again.awaiting is Side.CLIENT


async def test_the_other_sides_reply_is_unread_until_opened(
    service: QuestionService, clock: _Clock
) -> None:
    thread = await _firm_asks(service)
    clock.tick()
    await service.post_message(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=CLIENT_ORG,
        user_id=OWNER,
        body="Antwoord",
    )
    [listed] = await service.list_threads(
        administration_id=ADMIN, session_organization_id=FIRM_ORG, user_id=ACCOUNTANT, status="open"
    )
    assert listed.unread

    clock.tick()
    opened, messages = await service.open_thread(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=FIRM_ORG,
        user_id=ACCOUNTANT,
    )
    assert not opened.unread
    assert [m.body for m in messages] == [
        "Waar is deze betaling van 12 september voor?",
        "Antwoord",
    ]
    [listed] = await service.list_threads(
        administration_id=ADMIN, session_organization_id=FIRM_ORG, user_id=ACCOUNTANT, status="open"
    )
    assert not listed.unread


async def test_a_colleagues_message_on_my_own_side_is_not_unread_for_me(
    service: QuestionService,
) -> None:
    await _firm_asks(service)
    colleague = uuid.uuid4()
    [listed] = await service.list_threads(
        administration_id=ADMIN, session_organization_id=FIRM_ORG, user_id=colleague, status="open"
    )
    assert not listed.unread
    [for_client] = await service.list_threads(
        administration_id=ADMIN, session_organization_id=CLIENT_ORG, user_id=OWNER, status="open"
    )
    assert for_client.unread


async def test_a_resolved_thread_takes_no_more_messages(service: QuestionService) -> None:
    thread = await _firm_asks(service)
    resolved = await service.resolve(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=CLIENT_ORG,
        user_id=OWNER,
    )
    assert resolved.status is ThreadStatus.RESOLVED
    assert resolved.resolved_by_user_id == OWNER

    with pytest.raises(ThreadResolved):
        await service.post_message(
            administration_id=ADMIN,
            thread_id=thread.id,
            session_organization_id=FIRM_ORG,
            user_id=ACCOUNTANT,
            body="Nog één ding",
        )

    # Resolving again changes nothing.
    again = await service.resolve(
        administration_id=ADMIN,
        thread_id=thread.id,
        session_organization_id=FIRM_ORG,
        user_id=ACCOUNTANT,
    )
    assert again.resolved_by_user_id == OWNER

    assert (
        await service.list_threads(
            administration_id=ADMIN,
            session_organization_id=FIRM_ORG,
            user_id=ACCOUNTANT,
            status="open",
        )
        == []
    )
    assert (
        len(
            await service.list_threads(
                administration_id=ADMIN,
                session_organization_id=FIRM_ORG,
                user_id=ACCOUNTANT,
                status="all",
            )
        )
        == 1
    )


async def test_a_thread_is_found_only_within_its_own_administration(
    service: QuestionService,
) -> None:
    thread = await _firm_asks(service)
    with pytest.raises(ThreadNotFound):
        await service.open_thread(
            administration_id=OTHER_ADMIN,
            thread_id=thread.id,
            session_organization_id=OTHER_CLIENT_ORG,
            user_id=OWNER,
        )
    for call in (service.post_message,):
        with pytest.raises(ThreadNotFound):
            await call(
                administration_id=OTHER_ADMIN,
                thread_id=thread.id,
                session_organization_id=OTHER_CLIENT_ORG,
                user_id=OWNER,
                body="x",
            )
    with pytest.raises(ThreadNotFound):
        await service.resolve(
            administration_id=OTHER_ADMIN,
            thread_id=thread.id,
            session_organization_id=OTHER_CLIENT_ORG,
            user_id=OWNER,
        )


async def test_an_administration_the_session_cannot_see_is_not_found(
    service: QuestionService,
) -> None:
    with pytest.raises(AdministrationNotFound):
        await _firm_asks(service, administration_id=uuid.uuid4())


async def test_the_record_a_question_is_about_must_be_in_the_same_administration(
    service: QuestionService, repo: FakeQuestionRepository
) -> None:
    transaction = uuid.uuid4()
    repo.resources.add((OTHER_ADMIN, ResourceType.BANK_TRANSACTION, transaction))
    with pytest.raises(ResourceNotFound):
        await _firm_asks(
            service, resource_type=ResourceType.BANK_TRANSACTION, resource_id=transaction
        )

    repo.resources.add((ADMIN, ResourceType.BANK_TRANSACTION, transaction))
    thread = await _firm_asks(
        service, resource_type=ResourceType.BANK_TRANSACTION, resource_id=transaction
    )
    assert thread.resource_type is ResourceType.BANK_TRANSACTION
    assert thread.resource_id == transaction


async def test_half_a_resource_reference_is_refused(service: QuestionService) -> None:
    with pytest.raises(ResourceIncomplete):
        await _firm_asks(service, resource_type=ResourceType.DOCUMENT)
    with pytest.raises(ResourceIncomplete):
        await _firm_asks(service, resource_id=uuid.uuid4())


async def test_empty_and_overlong_text_is_refused(service: QuestionService) -> None:
    with pytest.raises(EmptyText):
        await _firm_asks(service, subject="   ")
    with pytest.raises(EmptyText):
        await _firm_asks(service, body="\n\t ")
    with pytest.raises(TextTooLong):
        await _firm_asks(service, subject="x" * 201)
    with pytest.raises(TextTooLong):
        await _firm_asks(service, body="x" * 5001)


async def test_the_inbox_reads_only_the_portfolio_it_is_given(
    service: QuestionService, clock: _Clock
) -> None:
    mine = await _firm_asks(service)
    clock.tick()
    revoked = await _firm_asks(service, administration_id=OTHER_ADMIN)
    # The client answers in both.
    for thread, org, admin in ((mine, CLIENT_ORG, ADMIN), (revoked, OTHER_CLIENT_ORG, OTHER_ADMIN)):
        clock.tick()
        await service.post_message(
            administration_id=admin,
            thread_id=thread.id,
            session_organization_id=org,
            user_id=OWNER,
            body="Zie bijlage " + "y" * 200,
        )

    # OTHER_ADMIN is not in the portfolio (no grant, or authorize() said no): never read.
    inbox = await service.inbox(
        user_id=ACCOUNTANT,
        session_organization_id=FIRM_ORG,
        administration_ids=[ADMIN],
        unread_only=False,
        limit=20,
    )
    assert [item.thread_id for item in inbox.items] == [mine.id]
    assert inbox.unread_count == 1
    assert inbox.items[0].unread
    assert inbox.items[0].display_name == "Bakker B.V."
    assert len(inbox.items[0].excerpt) == 120


async def test_an_empty_portfolio_is_an_empty_inbox(service: QuestionService) -> None:
    await _firm_asks(service)
    inbox = await service.inbox(
        user_id=ACCOUNTANT,
        session_organization_id=FIRM_ORG,
        administration_ids=[],
        unread_only=False,
        limit=20,
    )
    assert inbox == Inbox(unread_count=0, items=())


async def test_unread_only_and_the_limit(
    service: QuestionService, repo: FakeQuestionRepository, clock: _Clock
) -> None:
    first = await _firm_asks(service)
    clock.tick()
    await _firm_asks(service, subject="Tweede")
    clock.tick()
    await service.post_message(
        administration_id=ADMIN,
        thread_id=first.id,
        session_organization_id=CLIENT_ORG,
        user_id=OWNER,
        body="Antwoord",
    )
    unread = await service.inbox(
        user_id=ACCOUNTANT,
        session_organization_id=FIRM_ORG,
        administration_ids=[ADMIN],
        unread_only=True,
        limit=20,
    )
    assert [i.thread_id for i in unread.items] == [first.id]
    capped = await service.inbox(
        user_id=ACCOUNTANT,
        session_organization_id=FIRM_ORG,
        administration_ids=[ADMIN],
        unread_only=False,
        limit=1,
    )
    assert len(capped.items) == 1 and capped.unread_count == 1


async def test_the_inbox_splits_replies_from_questions_still_out(
    service: QuestionService, repo: FakeQuestionRepository, clock: _Clock
) -> None:
    """`awaiting=firm` is "replies to you": the firm's own unanswered questions are not in it,
    nor in its unread count (the sidebar badge)."""
    answered = await _firm_asks(service)
    clock.tick()
    still_out = await _firm_asks(service, subject="Nog open")
    clock.tick()
    await service.post_message(
        administration_id=ADMIN,
        thread_id=answered.id,
        session_organization_id=CLIENT_ORG,
        user_id=OWNER,
        body="Antwoord",
    )

    async def inbox(awaiting: Side | None) -> Inbox:
        return await service.inbox(
            user_id=ACCOUNTANT,
            session_organization_id=FIRM_ORG,
            administration_ids=[ADMIN],
            unread_only=False,
            limit=20,
            awaiting=awaiting,
        )

    replies = await inbox(Side.FIRM)
    assert [i.thread_id for i in replies.items] == [answered.id]
    assert replies.unread_count == 1
    waiting = await inbox(Side.CLIENT)
    assert [i.thread_id for i in waiting.items] == [still_out.id]
    assert waiting.unread_count == 0
    both = await inbox(None)
    assert {i.thread_id for i in both.items} == {answered.id, still_out.id}
    assert repo.inbox_awaiting == [Side.FIRM, Side.CLIENT, None]


def test_the_inbox_awaiting_parameter() -> None:
    assert awaiting_filter("firm") is Side.FIRM
    assert awaiting_filter("client") is Side.CLIENT
    assert awaiting_filter("any") is None

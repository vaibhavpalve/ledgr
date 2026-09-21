"""Receipt capture - FR-EXP-001, FR-EXP-001a.

The tests that matter most are the ones separating the two "several". A
three-page invoice that became three expenses is claimed three times; an
afternoon's receipts that collapsed into one is claimed once. Both look
plausible on a screen, so both are asserted here by counting expenses rather
than by inspecting anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from api.audit.log import AuditLog, AuditOutcome
from api.authz.service import AuthorizationService
from api.documents.content_type import ContentTypeError
from api.documents.model import DocumentInfected
from api.documents.scanning import EICAR, LocalPatternScanner
from api.documents.service import DocumentService
from api.documents.storage import InMemoryBlobStore
from api.expenses.capture import (
    CaptureService,
    duplicates_in,
    expenses_that_would_be_created,
    status_after_finalisation,
)
from api.expenses.model import (
    CaptureSession,
    CaptureSource,
    EmptySession,
    Expense,
    ExpenseStatus,
    ItemNotFound,
    ItemWithoutPages,
    NotAuthorizedToCapture,
    ReviewEntry,
    SessionAlreadyFinalised,
    SessionNotFound,
)
from tests.authz.helpers import build_world
from tests.documents.test_service import FakeDocumentRepository
from tests.support.fake_audit_repository import InMemoryAuditRepository

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"a" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"b" * 64
PDF = b"%PDF-1.7\nreceipt\n" + b"c" * 64
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"d" * 64
#: FR-EXP-001 names multi-page PDF explicitly. It is still ONE file, so it is
#: one stored original and one page - see the model's docstring.
MULTIPAGE_PDF = b"%PDF-1.7\npage1\npage2\npage3\npage4\n" + b"e" * 64


@dataclass
class FakeCaptureRepository:
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    documents: FakeDocumentRepository
    sessions: dict[uuid.UUID, CaptureSession] = field(default_factory=dict)
    #: item_id -> (session_id, position, discarded)
    items: dict[uuid.UUID, tuple[uuid.UUID, int, bool]] = field(default_factory=dict)
    #: item_id -> [document_id]
    pages: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    expenses: dict[uuid.UUID, Expense] = field(default_factory=dict)

    async def open_session(
        self, *, organization_id: uuid.UUID, administration_id: uuid.UUID, user_id: uuid.UUID
    ) -> CaptureSession:
        session = CaptureSession(
            id=uuid.uuid4(),
            administration_id=administration_id,
            opened_by_user_id=user_id,
            opened_at=datetime.now(),
        )
        self.sessions[session.id] = session
        return session

    async def get_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> CaptureSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.administration_id != administration_id:
            return None
        return session

    async def review_list(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[ReviewEntry]:
        entries: list[ReviewEntry] = []
        seen: dict[bytes, uuid.UUID] = {}
        for item_id, (sid, position, discarded) in sorted(
            self.items.items(), key=lambda kv: kv[1][1]
        ):
            if sid != session_id:
                continue
            document_ids = self.pages.get(item_id, [])
            docs = [self.documents.documents[d] for d in document_ids]
            duplicate_of = None
            if docs:
                first = docs[0].content_hash
                if first in seen:
                    duplicate_of = seen[first]
                else:
                    seen[first] = item_id
            expense = next(
                (e for e in self.expenses.values() if e.capture_item_id == item_id), None
            )
            entries.append(
                ReviewEntry(
                    item_id=item_id,
                    position=position,
                    expense_id=expense.id if expense else None,
                    page_count=len(docs),
                    content_types=tuple(d.content_type.value for d in docs),
                    total_bytes=sum(d.byte_size for d in docs),
                    discarded=discarded,
                    duplicate_of=duplicate_of,
                )
            )
        return entries

    async def add_item(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        category: str | None = None,
    ) -> tuple[uuid.UUID, Expense]:
        item_id = uuid.uuid4()
        position = 1 + sum(1 for s, _, _ in self.items.values() if s == session_id)
        self.items[item_id] = (session_id, position, False)
        expense = Expense(
            id=uuid.uuid4(),
            administration_id=administration_id,
            capture_item_id=item_id,
            status=ExpenseStatus.DRAFT,
            submitted_by_user_id=user_id,
            category=category,
        )
        self.expenses[expense.id] = expense
        return item_id, expense

    async def add_page(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        document_id: uuid.UUID,
        source: str,
    ) -> int:
        self.pages.setdefault(item_id, []).append(document_id)
        return len(self.pages[item_id])

    async def item_belongs_to_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, item_id: uuid.UUID
    ) -> bool:
        entry = self.items.get(item_id)
        return entry is not None and entry[0] == session_id and not entry[2]

    async def items_without_pages(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[uuid.UUID]:
        return [
            item_id
            for item_id, (sid, _, discarded) in self.items.items()
            if sid == session_id and not discarded and not self.pages.get(item_id)
        ]

    async def discard_item(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID, user_id: uuid.UUID, reason: str
    ) -> None:
        sid, position, _ = self.items[item_id]
        self.items[item_id] = (sid, position, True)

    async def finalise(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Expense]:
        from dataclasses import replace

        session = self.sessions[session_id]
        self.sessions[session_id] = replace(session, finalised_at=datetime.now())
        # Status is NOT changed. Finalising closes the sitting; an expense
        # becomes ready when its form is complete (FR-EXP-001b/e, migration
        # 0033's expense_ready_is_complete).
        return [
            expense
            for expense in self.expenses.values()
            if self.items[expense.capture_item_id][0] == session_id
            and not self.items[expense.capture_item_id][2]
        ]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id if administration_id == self.administration_id else None


@dataclass
class Harness:
    service: CaptureService
    repository: FakeCaptureRepository
    audit: InMemoryAuditRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID
    fiscal_year: uuid.UUID


def harness(*, role: str = "Bookkeeper") -> Harness:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)

    documents = FakeDocumentRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    audit = InMemoryAuditRepository()
    authorization = AuthorizationService(world.repository)
    document_service = DocumentService(
        repository=documents,
        blobs=InMemoryBlobStore(),
        scanner=LocalPatternScanner(),
        authorization=authorization,
        audit_log=AuditLog(audit),
    )
    repository = FakeCaptureRepository(
        organization_id=world.acme,
        administration_id=world.acme_books,
        documents=documents,
    )
    return Harness(
        service=CaptureService(
            repository=repository,
            documents=document_service,
            authorization=authorization,
            audit_log=AuditLog(audit),
        ),
        repository=repository,
        audit=audit,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
        fiscal_year=uuid.uuid4(),
    )


async def open_session(h: Harness) -> uuid.UUID:
    session = await h.service.open_session(administration_id=h.administration, actor_user_id=h.user)
    return session.id


async def capture(
    h: Harness, session_id: uuid.UUID, data: bytes = JPEG, **kwargs: Any
) -> tuple[uuid.UUID, Any, int]:
    kwargs.setdefault("source", CaptureSource.CAMERA)
    return await h.service.capture(
        administration_id=h.administration,
        session_id=session_id,
        actor_user_id=h.user,
        fiscal_year_id=h.fiscal_year,
        data=data,
        **kwargs,
    )


# ===========================================================================
# FR-EXP-001: both paths land in the same place
# ===========================================================================


async def test_camera_and_upload_produce_the_same_result() -> None:
    """ "Both paths land in the same place" - asserted by giving the same bytes
    through each source and comparing everything except the label.
    """
    h = harness()
    session = await open_session(h)

    camera_item, camera_doc, camera_page = await capture(
        h, session, JPEG, source=CaptureSource.CAMERA
    )
    upload_item, upload_doc, upload_page = await capture(
        h, session, PNG, source=CaptureSource.UPLOAD, filename="receipt.png"
    )

    assert camera_page == upload_page == 1
    assert camera_item != upload_item, "each is its own receipt"
    # Both went through the archive: stored, hashed, scanned, retained.
    assert camera_doc.content_hash and upload_doc.content_hash
    assert camera_doc.retention_until == upload_doc.retention_until
    # And both produced a draft expense.
    assert len(h.repository.expenses) == 2


async def test_there_is_one_capture_method_for_both_paths() -> None:
    """The structural form of the requirement. Two entry points would be two
    pipelines, and the second built would be the one that forgot the scan.
    """
    assert not hasattr(h_service := CaptureService, "capture_from_camera")
    assert not hasattr(h_service, "capture_from_upload")
    assert hasattr(h_service, "capture")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(JPEG, "image/jpeg", id="jpeg"),
        pytest.param(PNG, "image/png", id="png"),
        pytest.param(HEIC, "image/heic", id="heic"),
        pytest.param(PDF, "application/pdf", id="pdf"),
        pytest.param(MULTIPAGE_PDF, "application/pdf", id="multi-page-pdf"),
    ],
)
async def test_the_five_formats_fr_exp_001_names_are_accepted(data: bytes, expected: str) -> None:
    h = harness()
    session = await open_session(h)

    _, document, _ = await capture(h, session, data)

    assert document.content_type.value == expected


async def test_a_chosen_category_files_the_new_expense() -> None:
    """The capture screen asks for the category first, so the receipt arrives
    filed. The stored value is the category's English label, taken from the
    server's list rather than from whatever the client sent.
    """
    h = harness()
    session = await open_session(h)

    await capture(h, session, PDF, category="office_supplies")

    (expense,) = h.repository.expenses.values()
    assert expense.category == "Office supplies"


async def test_a_receipt_captured_without_a_category_has_none() -> None:
    h = harness()
    session = await open_session(h)

    await capture(h, session, JPEG)

    (expense,) = h.repository.expenses.values()
    assert expense.category is None


async def test_an_unknown_category_is_refused_before_anything_is_stored() -> None:
    from api.expenses.categories import UnknownExpenseCategory

    h = harness()
    session = await open_session(h)

    with pytest.raises(UnknownExpenseCategory):
        await capture(h, session, PDF, category="not_a_category")

    # Checked first: a refused capture leaves neither a receipt nor a stored
    # original behind it.
    assert h.repository.expenses == {}
    assert h.repository.pages == {}


async def test_a_further_page_never_re_files_the_receipt() -> None:
    """The category applies where an expense is CREATED. A later page joins an
    expense the person may already have corrected in the form.
    """
    h = harness()
    session = await open_session(h)
    item, _, _ = await capture(h, session, JPEG, category="lunch")

    await capture(h, session, PNG, item_id=item, category="utilities")

    (expense,) = h.repository.expenses.values()
    assert expense.category == "Lunch"


async def test_a_format_nobody_named_is_refused_by_the_archive() -> None:
    """The allowlist lives in the document archive (SEC-005), not here, so
    capture and a direct upload cannot disagree about what a receipt may be.
    """
    h = harness()
    session = await open_session(h)

    with pytest.raises(ContentTypeError):
        await capture(h, session, b"<svg xmlns='http://www.w3.org/2000/svg'/>")

    assert h.repository.items == {}, "no receipt was opened for a refused file"
    assert h.repository.expenses == {}


async def test_an_infected_receipt_creates_no_expense() -> None:
    """The scan happens inside the archive, before anything is stored - so a
    refused capture leaves no item and no claim.
    """
    h = harness()
    session = await open_session(h)

    with pytest.raises(DocumentInfected):
        await capture(h, session, PDF + EICAR)

    assert h.repository.expenses == {}


# ===========================================================================
# The distinction: multi-page vs batch
# ===========================================================================


async def test_multi_page_is_one_receipt_and_one_expense() -> None:
    """FR-EXP-001's "multi-page". Three photographs of one invoice.

    The failure this prevents is the claim being made three times.
    """
    h = harness()
    session = await open_session(h)

    item, _, first = await capture(h, session, JPEG)
    _, _, second = await capture(h, session, PNG, item_id=item)
    _, _, third = await capture(h, session, PDF, item_id=item)

    assert (first, second, third) == (1, 2, 3)
    assert len(h.repository.expenses) == 1, "one receipt is one expense"
    assert len(h.repository.pages[item]) == 3


async def test_batch_is_several_receipts_and_several_expenses() -> None:
    """FR-EXP-001a's "each becoming a separate expense". A shoebox.

    The failure this prevents is an afternoon's receipts collapsing into one.
    """
    h = harness()
    session = await open_session(h)

    items = [(await capture(h, session, data))[0] for data in (JPEG, PNG, PDF, HEIC)]

    assert len(set(items)) == 4
    assert len(h.repository.expenses) == 4


async def test_a_multi_page_pdf_is_one_page() -> None:
    """The counter-intuitive case, and it follows from FR-DOC-001.

    A four-page PDF is one file. The original is stored unaltered, so it is one
    document and one page - splitting it would mean storing four things the
    user never gave us. What matters is that it is still one expense, which it
    is either way.
    """
    h = harness()
    session = await open_session(h)

    item, _, page_number = await capture(h, session, MULTIPAGE_PDF, source=CaptureSource.UPLOAD)

    assert page_number == 1
    assert len(h.repository.pages[item]) == 1
    assert len(h.repository.expenses) == 1


async def test_the_two_are_distinguished_only_by_the_item_parameter() -> None:
    """Nothing in an image says whether it is the next page or the next
    receipt, so the caller says. A heuristic would be wrong silently, in the
    direction that either multiplies or merges a claim.
    """
    h = harness()
    session = await open_session(h)

    first, _, _ = await capture(h, session, JPEG)
    # Same bytes, same moment, same source - and a different answer, because
    # the parameter is different.
    same_receipt, _, page = await capture(h, session, JPEG, item_id=first)
    new_receipt, _, _ = await capture(h, session, JPEG)

    assert same_receipt == first and page == 2
    assert new_receipt != first
    assert len(h.repository.expenses) == 2


async def test_an_item_from_another_session_is_refused() -> None:
    """An item id from elsewhere would attach this original to a receipt the
    caller is not reviewing.
    """
    h = harness()
    first_session = await open_session(h)
    other_session = await open_session(h)
    item, _, _ = await capture(h, first_session, JPEG)

    with pytest.raises(ItemNotFound):
        await capture(h, other_session, PNG, item_id=item)


# ===========================================================================
# FR-EXP-001a: the review list, before posting
# ===========================================================================


async def test_the_review_list_reports_receipts_not_pages() -> None:
    h = harness()
    session = await open_session(h)
    item, _, _ = await capture(h, session, JPEG)
    await capture(h, session, PNG, item_id=item)
    await capture(h, session, PDF)

    review = await h.service.review(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    assert len(review.items) == 2, "two receipts, not three images"
    assert [entry.page_count for entry in review.items] == [2, 1]
    assert expenses_that_would_be_created(review.items) == 2
    assert all(entry.expense_id is not None for entry in review.items)


async def test_an_exact_duplicate_is_flagged_in_the_list() -> None:
    """The same receipt photographed twice in one sitting - which a batch
    makes common, and which needs no extraction to spot.

    NOT FR-EXP-001g: that matches on supplier, date and amount and needs
    extraction (FR-EXP-001c, P1). This is the byte-identical case.
    """
    h = harness()
    session = await open_session(h)
    first, _, _ = await capture(h, session, JPEG)
    second, _, _ = await capture(h, session, JPEG)

    review = await h.service.review(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    assert duplicates_in(review.items) == {second: first}
    # Flagged, not refused: two identical receipts can be legitimate, and the
    # review list is where a person decides.
    assert expenses_that_would_be_created(review.items) == 2


async def test_a_discarded_receipt_becomes_no_expense() -> None:
    h = harness()
    session = await open_session(h)
    keep, _, _ = await capture(h, session, JPEG)
    drop, _, _ = await capture(h, session, PNG)

    await h.service.discard(
        administration_id=h.administration,
        session_id=session,
        item_id=drop,
        actor_user_id=h.user,
        reason="blurred, retaken",
    )
    released = await h.service.finalise(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    assert [e.capture_item_id for e in released] == [keep]


# ===========================================================================
# Finalisation
# ===========================================================================


async def test_finalising_closes_the_session_and_leaves_the_drafts_draft() -> None:
    """Corrected when the expense form landed (FR-EXP-001b/e).

    Finalisation used to move every expense to `ready`, which made sense only
    while `ready` meant nothing. It now means FR-EXP-001b's minimum has been
    given, so readying a freshly captured expense would release a claim with no
    amount and no payment method - one FR-EXP-002 cannot approve and
    FR-EXP-003 cannot pay.

    Closing the sitting and filling the forms afterwards is also the only shape
    that works on a phone: photograph six receipts on the train, complete them
    later.
    """
    h = harness()
    session = await open_session(h)
    for data in (JPEG, PNG, PDF):
        await capture(h, session, data)

    released = await h.service.finalise(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    assert len(released) == 3
    assert all(e.status is ExpenseStatus.DRAFT for e in released)
    assert status_after_finalisation() is ExpenseStatus.DRAFT


async def test_an_empty_session_is_refused() -> None:
    """Somebody who has just photographed six receipts and is told "done"
    cannot tell that from six uploads that failed.
    """
    h = harness()
    session = await open_session(h)

    with pytest.raises(EmptySession):
        await h.service.finalise(
            administration_id=h.administration, session_id=session, actor_user_id=h.user
        )


async def test_an_item_with_no_evidence_blocks_finalisation() -> None:
    """It would become an expense resting on nothing - the state FR-DOC-003's
    completeness report finds after the fact, refused here while the person is
    still holding the receipt.
    """
    h = harness()
    session = await open_session(h)
    await capture(h, session, JPEG)
    # An item whose upload failed after the row was created.
    orphan, _ = await h.repository.add_item(
        organization_id=h.organization,
        administration_id=h.administration,
        session_id=session,
        user_id=h.user,
    )

    with pytest.raises(ItemWithoutPages) as raised:
        await h.service.finalise(
            administration_id=h.administration, session_id=session, actor_user_id=h.user
        )
    assert raised.value.item_ids == (orphan,)


async def test_nothing_can_be_captured_after_the_list_is_accepted() -> None:
    """FR-EXP-001a's boundary. A receipt arriving later would not have been in
    the list that was accepted.
    """
    h = harness()
    session = await open_session(h)
    await capture(h, session, JPEG)
    await h.service.finalise(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    with pytest.raises(SessionAlreadyFinalised):
        await capture(h, session, PNG)
    with pytest.raises(SessionAlreadyFinalised):
        await h.service.finalise(
            administration_id=h.administration, session_id=session, actor_user_id=h.user
        )


async def test_capture_posts_nothing_to_the_ledger() -> None:
    """Capture stops well short of posting. FR-EXP-002's approval and
    FR-EXP-003's reimbursement take it from there, and neither is built - so
    nothing here may claim to have been posted.
    """
    h = harness()
    session = await open_session(h)
    await capture(h, session, JPEG)

    released = await h.service.finalise(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    assert {e.status for e in released} == {ExpenseStatus.DRAFT}
    # Capture leaves a claim two steps short of the books: the form completes
    # it (`ready`) and confirmation posts it (`posted`). Neither happens here.
    assert [s.value for s in ExpenseStatus] == ["draft", "ready", "posted"]


# ===========================================================================
# Drafts, tenancy and authorization
# ===========================================================================


async def test_a_draft_expense_starts_empty() -> None:
    """FR-EXP-001c: "the product never blocks on extraction being available".

    A receipt that has just been photographed has an amount nobody has read
    yet, and requiring one at capture would make the camera path block on OCR
    or on typing.
    """
    h = harness()
    session = await open_session(h)
    await capture(h, session, JPEG)

    expense = next(iter(h.repository.expenses.values()))
    assert expense.status is ExpenseStatus.DRAFT
    assert expense.expense_date is None
    assert expense.gross_amount is None
    assert expense.supplier is None
    assert expense.payment_method is None


def test_a_money_field_is_decimal_never_float() -> None:
    """NFR-031 / CLAUDE.md rule four, at the type that holds an amount."""
    expense = Expense(
        id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        capture_item_id=uuid.uuid4(),
        status=ExpenseStatus.DRAFT,
        submitted_by_user_id=uuid.uuid4(),
        gross_amount=Decimal("12.35"),
    )
    assert isinstance(expense.gross_amount, Decimal)
    assert expense.gross_amount == Decimal("12.35")


async def test_a_role_without_submit_expense_cannot_capture() -> None:
    """Appendix A's "Submit expenses" row, reused rather than invented
    (ADR-012). A Viewer does not hold it.
    """
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await h.service.open_session(administration_id=h.administration, actor_user_id=h.user)


async def test_a_denied_capture_is_audited() -> None:
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToCapture):
        await h.service.open_session(administration_id=h.administration, actor_user_id=h.user)

    entries = await h.audit.search(organization_id=h.organization)
    denied = [e for e in entries if e.outcome is AuditOutcome.DENIED]
    assert denied and denied[0].action == "submit_expense"


async def test_another_administrations_session_is_not_found() -> None:
    h = harness()
    session = await open_session(h)

    with pytest.raises(SessionNotFound):
        await h.service.review(
            administration_id=uuid.uuid4(),
            session_id=session,
            actor_user_id=h.user,
        )


async def test_the_session_is_audited_from_opening_to_finalisation() -> None:
    """IAM-090: the sitting is the context an auditor reads a batch of claims
    in, so the boundaries are recorded and the count is on the last entry.
    """
    h = harness()
    session = await open_session(h)
    await capture(h, session, JPEG)
    await capture(h, session, PNG)
    await h.service.finalise(
        administration_id=h.administration, session_id=session, actor_user_id=h.user
    )

    actions = [e.action for e in await h.audit.search(organization_id=h.organization)]
    assert "open_capture_session" in actions
    assert actions.count("capture_receipt_page") == 2
    assert "finalise_capture_session" in actions

    final = [
        e
        for e in await h.audit.search(organization_id=h.organization)
        if e.action == "finalise_capture_session"
    ]
    assert final[0].detail["expenses"] == 2

"""Receipt capture - FR-EXP-001, FR-EXP-001a.

--- "Both paths land in the same place" is one method ---

    FR-EXP-001  Receipt capture by CAMERA (single tap from the home screen,
                multi-page, auto edge detection, deskew, glare and blur warning
                with retake prompt) and by UPLOAD (drag-and-drop or file
                picker, accepting JPEG, PNG, HEIC, PDF and multi-page PDF).
                Both paths land in the same place.

`capture_page` is that place. There is no `capture_from_camera` and no
`capture_from_upload`; there is one method taking bytes, and `source` is
recorded for support and MOB-003's queue display without anything reading it
back. Two entry points would be two pipelines, and the second one built would
be the one that forgot the malware scan.

The camera-side processing FR-EXP-001 names - edge detection, deskew, glare and
blur warnings with a retake prompt - is CLIENT work, and deliberately so. It
happens before the shutter closes, where a retake is one tap; a server that
judged blur could only reject an upload the person has already walked away
from. What lands here is the image they accepted.

--- Accepting the five formats is not this module's decision ---

FR-EXP-001's list - JPEG, PNG, HEIC, PDF, multi-page PDF - is already
`api.documents.content_type.DocumentContentType`, verified from the bytes
(SEC-005). Capture calls the document archive and takes its answer. Restating
the list here would be a second allowlist to keep in step, and the one that
drifted would be the one a receipt was refused by.

A multi-page PDF needs no special handling at all: it is one file, so it is one
document and one page row. `pages` counts stored originals, not sheets.

--- What "before posting" means ---

FR-EXP-001a's review list sits between capture and posting, and finalisation is
the boundary. After it, nothing can be added to the session: a receipt arriving
later would not have been in the list that was accepted, so the acceptance
would cover something nobody saw. Migration 0032 enforces that with triggers
rather than leaving it here, because a request racing a finalisation would
otherwise slip past a check made in Python.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.documents.model import Document
from api.documents.retention import RetentionBasis
from api.documents.service import DocumentService
from api.expenses.categories import category_for_key
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

#: Appendix A's "Submit expenses" row (matrix.py). Reused rather than invented,
#: per ADR-012 - capturing a receipt IS submitting an expense, and §8.4 gives
#: the Expense Submitter exactly this and no more.
SUBMIT_EXPENSE = ("submit", "expense")


class CaptureRepository(Protocol):
    async def open_session(
        self, *, organization_id: uuid.UUID, administration_id: uuid.UUID, user_id: uuid.UUID
    ) -> CaptureSession: ...

    async def get_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> CaptureSession | None: ...

    async def review_list(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> Sequence[ReviewEntry]: ...

    async def add_item(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        category: str | None = None,
    ) -> tuple[uuid.UUID, Expense]:
        """A new receipt, and the expense it becomes. One statement, because
        FR-EXP-001a's "each becoming a separate expense" must not have a window
        in which an item exists without one.
        """
        ...

    async def add_page(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        document_id: uuid.UUID,
        source: str,
    ) -> int:
        """Appends a stored original and returns its page number."""
        ...

    async def item_belongs_to_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, item_id: uuid.UUID
    ) -> bool: ...

    async def items_without_pages(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> Sequence[uuid.UUID]: ...

    async def discard_item(
        self,
        *,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str,
    ) -> None: ...

    async def finalise(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> Sequence[Expense]: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class CaptureService:
    """FR-EXP-001's entry point, for both paths."""

    def __init__(
        self,
        repository: CaptureRepository,
        documents: DocumentService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._documents = documents
        self._authorization = authorization
        self._audit = audit_log

    async def open_session(
        self, *, administration_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> CaptureSession:
        """FR-EXP-001a's "one session"."""
        await self._require(actor_user_id, administration_id)
        organization_id = await self._organization_of(administration_id)
        session = await self._repository.open_session(
            organization_id=organization_id,
            administration_id=administration_id,
            user_id=actor_user_id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="open_capture_session",
            resource_id=session.id,
            detail={},
        )
        return session

    async def capture(
        self,
        *,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        fiscal_year_id: uuid.UUID,
        data: bytes,
        source: CaptureSource,
        filename: str | None = None,
        declared_content_type: str | None = None,
        item_id: uuid.UUID | None = None,
        category: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[uuid.UUID, Document, int]:
        """The one place both paths land.

        `item_id` is what separates FR-EXP-001's multi-page from
        FR-EXP-001a's batch, and it is the only difference between them:

            absent   a NEW receipt. A new item, a new draft expense.
            present  ANOTHER ORIGINAL for a receipt already in this session.
                     No new expense.

        Making it an explicit parameter rather than something inferred is
        deliberate. Nothing in an image says whether it is the second page of
        the last receipt or the first page of the next one, and a heuristic -
        elapsed time, similarity - would be wrong silently and in the direction
        that either multiplies or merges somebody's claim.

        `category` files the NEW expense under one of `api.expenses.categories`'s
        keys. It is checked before anything is stored, so a key this product does
        not have leaves no document behind. It only applies where an expense is
        created: on a further page (`item_id` present) there is nothing to file
        and it is ignored, rather than silently re-filing a receipt the person
        may already have corrected in the form.

        Returns the item, the stored document, and the page number, so a client
        can show the page it just added against the receipt it belongs to.
        """
        await self._require(actor_user_id, administration_id)
        session = await self._open_session_or_refuse(administration_id, session_id)
        # Resolved up front: `category_for_key` raises for an unknown key, and
        # that must happen before the archive has stored anything.
        category_label = category_for_key(category).label if category and item_id is None else None

        if item_id is not None and not await self._repository.item_belongs_to_session(
            administration_id=administration_id, session_id=session.id, item_id=item_id
        ):
            # Checked rather than trusted: an item id from another session -
            # or another tenant's - would attach this original to a receipt
            # the caller is not reviewing.
            raise ItemNotFound(f"item {item_id} is not in session {session_id}")

        organization_id = await self._organization_of(administration_id)

        # FR-EXP-001d: "retained as the source document under §6.10". The
        # archive owns the format allowlist (SEC-005), the malware scan, the
        # hash and the retention date; capture does not re-decide any of them.
        document = await self._documents.upload(
            administration_id=administration_id,
            fiscal_year_id=fiscal_year_id,
            actor_user_id=actor_user_id,
            data=data,
            original_filename=filename,
            declared_content_type=declared_content_type,
            # A receipt is not a deed. FR-DOC-002's ten-year basis is an
            # accounting judgement about the transaction, and capture has not
            # got one - the expense form is where it could be given.
            retention_basis=RetentionBasis.STANDARD,
            correlation_id=correlation_id,
        )

        # The branch that decides between FR-EXP-001's multi-page and
        # FR-EXP-001a's batch, and the ONLY place a new expense is created.
        started_new_receipt = item_id is None
        if item_id is None:
            item_id, _expense = await self._repository.add_item(
                organization_id=organization_id,
                administration_id=administration_id,
                session_id=session.id,
                user_id=actor_user_id,
                category=category_label,
            )

        page_number = await self._repository.add_page(
            organization_id=organization_id,
            administration_id=administration_id,
            item_id=item_id,
            document_id=document.id,
            source=source.value,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="capture_receipt_page",
            resource_id=item_id,
            correlation_id=correlation_id,
            detail={
                "session_id": str(session.id),
                "document_id": str(document.id),
                "page_number": page_number,
                # Recorded, never branched on - see the module docstring.
                "source": source.value,
                "content_type": document.content_type.value,
                # Which of the two "several" this was, so the audit trail
                # distinguishes a page added to a receipt from a new claim.
                "started_new_receipt": started_new_receipt,
            },
        )
        return item_id, document, page_number

    async def review(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> CaptureSession:
        """FR-EXP-001a's "review list before posting"."""
        await self._require(actor_user_id, administration_id)
        session = await self._session_or_refuse(administration_id, session_id)
        entries = await self._repository.review_list(
            administration_id=administration_id, session_id=session_id
        )
        return CaptureSession(
            id=session.id,
            administration_id=session.administration_id,
            opened_by_user_id=session.opened_by_user_id,
            opened_at=session.opened_at,
            finalised_at=session.finalised_at,
            items=tuple(entries),
        )

    async def discard(
        self,
        *,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        """Drop a receipt from the list: a blurred retake, a duplicate, or
        something that turned out not to be a receipt.

        The DOCUMENTS behind it stay. They are inside their FR-DOC-002
        retention period and are not capture's to remove - and a discarded item
        with its originals intact is the record of a decision, which is what
        FR-EXP-001a's review step is for.
        """
        await self._require(actor_user_id, administration_id)
        await self._open_session_or_refuse(administration_id, session_id)
        if not await self._repository.item_belongs_to_session(
            administration_id=administration_id, session_id=session_id, item_id=item_id
        ):
            raise ItemNotFound(f"item {item_id} is not in session {session_id}")

        await self._repository.discard_item(
            administration_id=administration_id,
            item_id=item_id,
            user_id=actor_user_id,
            reason=reason,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="discard_captured_receipt",
            resource_id=item_id,
            detail={"session_id": str(session_id), "reason": reason},
        )

    async def finalise(
        self,
        *,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Sequence[Expense]:
        """Accept the review list. FR-EXP-001a's "before posting" ends here.

        Two refusals before anything changes, and both are about not producing
        a claim nobody can stand behind:

          * an EMPTY session. Somebody who has just photographed six receipts
            and is told "done" cannot tell that from six uploads that failed.
          * an item with NO stored original. It would become an expense
            resting on nothing - the state FR-DOC-003's completeness report
            exists to find after the fact, refused here while the person is
            still holding the receipt.
        """
        await self._require(actor_user_id, administration_id)
        await self._open_session_or_refuse(administration_id, session_id)

        entries = await self._repository.review_list(
            administration_id=administration_id, session_id=session_id
        )
        if not [entry for entry in entries if not entry.discarded]:
            raise EmptySession(
                f"capture session {session_id} has nothing to finalise. If receipts "
                f"were captured, they did not arrive."
            )

        empty = await self._repository.items_without_pages(
            administration_id=administration_id, session_id=session_id
        )
        if empty:
            raise ItemWithoutPages(tuple(empty))

        expenses = await self._repository.finalise(
            administration_id=administration_id,
            session_id=session_id,
            user_id=actor_user_id,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="finalise_capture_session",
            resource_id=session_id,
            correlation_id=correlation_id,
            detail={
                "expenses": len(expenses),
                "expense_ids": [str(e.id) for e in expenses],
            },
        )
        return expenses

    # -- internals ---------------------------------------------------------

    async def _session_or_refuse(
        self, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> CaptureSession:
        session = await self._repository.get_session(
            administration_id=administration_id, session_id=session_id
        )
        if session is None:
            # Indistinguishable from "belongs to another tenant": RLS filters
            # it out either way, so this branch cannot tell and must not seem
            # to.
            raise SessionNotFound(f"capture session {session_id} not found")
        return session

    async def _open_session_or_refuse(
        self, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> CaptureSession:
        session = await self._session_or_refuse(administration_id, session_id)
        if not session.is_open:
            raise SessionAlreadyFinalised(
                f"capture session {session_id} was finalised at "
                f"{session.finalised_at}; its review list is what was accepted "
                f"(FR-EXP-001a)"
            )
        return session

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise SessionNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = SUBMIT_EXPENSE
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToCapture(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                # IAM-090. A captured receipt is evidence entering the books,
                # and the session that produced a batch of claims is the
                # context an auditor reads them in.
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="expense",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def duplicates_in(entries: Sequence[ReviewEntry]) -> dict[uuid.UUID, uuid.UUID]:
    """Items whose evidence is byte-identical to an earlier one, as a mapping
    from the later item to the earlier.

    A pure view over the review list, so a client can group them without
    re-deriving the relation - and so the property can be tested without a
    database.

    EXACT duplicates only. FR-EXP-001g's supplier/date/amount matching needs
    extraction (FR-EXP-001c, P1) and is not built; this is the case extraction
    is not needed for and which a batch makes common.
    """
    return {
        entry.item_id: entry.duplicate_of for entry in entries if entry.duplicate_of is not None
    }


def expenses_that_would_be_created(entries: Sequence[ReviewEntry]) -> int:
    """How many expenses finalising this list produces.

    One per live item - FR-EXP-001a's "each becoming a separate expense" - and
    stated as a function so a review screen can show the number before the
    person commits to it. Pages do not count: that is the whole distinction.
    """
    return len([entry for entry in entries if not entry.discarded])


def status_after_finalisation() -> ExpenseStatus:
    """What a captured expense's status is once its session closes: still
    DRAFT.

    This CHANGED when the expense form landed (FR-EXP-001b/e, migration 0033),
    and the earlier answer was wrong. Finalising a capture session used to move
    every expense to `ready`, which made sense only while `ready` meant nothing
    - the fields did not exist yet.

    They do now, and `ready` means the minimum has been given: a date, a
    supplier, an amount, a treatment, a category and a payment method. An
    expense released as ready without them is a claim nobody can approve
    (FR-EXP-002) or reimburse (FR-EXP-003).

    So finalisation closes the SESSION - no more receipts in this sitting - and
    each expense becomes ready individually when its form is complete. That is
    also the only shape that works on a phone: photograph six receipts on the
    train, fill them in later, which is what FR-EXP-001c's "never blocks" and
    FR-EXP-001f's offline queue both describe.

    Migration 0033's `expense_ready_is_complete` CHECK is what makes the old
    behaviour impossible rather than merely discouraged.
    """
    return ExpenseStatus.DRAFT

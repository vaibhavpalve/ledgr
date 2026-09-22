"""Value types for receipt capture - FR-EXP-001, FR-EXP-001a.

--- The two "several", named apart ---

FR-EXP-001 and FR-EXP-001a both say several, and they mean opposite things:

    CaptureSession   one sitting              several receipts
      CaptureItem    one receipt              -> one Expense
        CapturePage  one stored original      several images of one receipt

Confusing them is the defect this module exists to make unspellable. A
three-page invoice that became three expenses is claimed three times; an
afternoon's receipts that collapsed into one is claimed once. Both look
plausible on a screen.

--- What this module does NOT do ---

It does not enforce the guarantees. Migration 0032 holds them:

    one item, one expense       UNIQUE on expense.capture_item_id
    nothing after review        triggers refusing inserts into a finalised
                                session
    one administration          same-tenant triggers on page and expense

The checks here are a fast, well-worded rejection before a round trip, the
stance `api.ledger.model` and `api.documents.model` both take.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from api.documents.content_type import DocumentContentType


class CaptureSource(enum.Enum):
    """How a page arrived, recorded and never branched on.

    FR-EXP-001's "Both paths land in the same place" is a statement about
    behaviour, so this exists for support and for MOB-003's offline queue
    display - not to route anything. The moment a query says
    `where source = CAMERA` the two paths have stopped landing in the same
    place.

    FR-EXP-001h's e-mail and share-sheet paths will add members here; they are
    absent rather than stubbed because a value nothing can produce is a value
    somebody will write a branch for.
    """

    CAMERA = "camera"
    UPLOAD = "upload"


class ExpenseStatus(enum.Enum):
    """Mirrors the `status` CHECK in migration 0032.

    `READY` is as far as capture goes. FR-EXP-002's approval workflow and
    FR-EXP-003's reimbursement run take it from there, and neither is built.
    """

    DRAFT = "draft"
    READY = "ready"
    #: FR-EXP-001d: the claim has a ledger entry. Terminal - migration 0034's
    #: `expense_posting_is_final` refuses any way back, because FR-GL-003
    #: corrects a posting with a reversing entry rather than by unmaking it.
    POSTED = "posted"


class PaymentMethod(enum.Enum):
    """FR-EXP-001e: "captured at entry ... because it determines the posting
    and cannot be reliably inferred later".

    The three the requirement names, and the reason each is distinct is the
    posting it produces, not the label:

        BUSINESS_ACCOUNT       paid from the bank account. Reconciles against a
                               bank transaction (FR-BNK).
        BUSINESS_CARD          paid on a company card. Settles later, so the
                               liability sits somewhere else in the meantime.
        PERSONAL_REIMBURSABLE  the person is owed money. Produces a payable to
                               an employee and lands in FR-EXP-003's
                               reimbursement run.

    Nothing on a receipt says which. A card slip looks the same whoever's card
    it was, which is exactly why FR-EXP-001e says it cannot be inferred later.
    """

    BUSINESS_ACCOUNT = "business_account"
    BUSINESS_CARD = "business_card"
    PERSONAL_REIMBURSABLE = "personal_reimbursable"

    @property
    def is_reimbursable(self) -> bool:
        """Whether the submitter is owed money. The one distinction that
        changes who gets paid rather than only which account is credited.
        """
        return self is PaymentMethod.PERSONAL_REIMBURSABLE


class VatTreatment(enum.Enum):
    """WHICH VAT applies - migration 0028's effective-dated treatments.

    FR-EXP-001b calls this field "VAT rate", and a rate is what the form
    SHOWS. What it stores is the treatment, because a rate is a fact about a
    treatment on a DATE: the shipped ruleset has `btw_21` at 19% from
    2001-01-01 and 21% from 2012-10-01, so a receipt dated 2012-09-30 is a 19%
    receipt however carefully somebody types.

    `BTW_MARGE` is absent, and that is not an oversight. The margin scheme
    charges VAT on the margin rather than the sale price, so extracting VAT
    from a gross receipt under it would compute the tax on the whole amount -
    overstating it by whatever the goods cost. Migration 0033 refuses it with a
    CHECK, because the resulting row would be internally consistent and wrong
    only in its scheme, which nothing downstream would catch.
    """

    BTW_21 = "btw_21"
    BTW_9 = "btw_9"
    BTW_0 = "btw_0"
    BTW_VRIJGESTELD = "btw_vrijgesteld"
    BTW_VERLEGD = "btw_verlegd"
    BTW_ICP = "btw_icp"
    BTW_EXPORT = "btw_export"


class CaptureError(Exception):
    """Base for this module's refusals."""


class SessionNotFound(CaptureError):
    """No such session in this administration.

    One exception for "does not exist" and "not yours": under RLS the query
    cannot tell them apart and the API must not appear to.
    """


class SessionAlreadyFinalised(CaptureError):
    """FR-EXP-001a's review list is a boundary.

    A receipt added after the list was accepted would not have been in it, so
    the acceptance would cover something nobody saw.
    """


class ItemNotFound(CaptureError):
    pass


class EmptySession(CaptureError):
    """Finalising a session with nothing in it.

    Refused rather than silently producing no expenses: a person who has just
    photographed six receipts and is told "done" has no way to tell that from
    six receipts that failed to upload.
    """


class ItemWithoutPages(CaptureError):
    """An item that would become an expense resting on no evidence.

    FR-DOC-003's completeness report finds these after the fact; refusing at
    finalisation is cheaper and happens while the person is still holding the
    receipt.
    """

    def __init__(self, item_ids: tuple[uuid.UUID, ...]) -> None:
        self.item_ids = item_ids
        super().__init__(
            f"{len(item_ids)} item(s) in this session have no captured original: "
            f"{', '.join(str(i) for i in item_ids)}"
        )


class ExpenseNotFound(CaptureError):
    """No such expense in this administration.

    One exception for "does not exist" and "not yours": under RLS the query
    cannot tell them apart and the API must not appear to.
    """


class IncompleteExpense(CaptureError):
    """FR-EXP-001b's minimum, or FR-EXP-001e's payment method, is missing.

    Raised only when marking an expense READY. Saving a partly-filled form is
    ordinary and allowed - FR-EXP-001c is explicit that the product never
    blocks on a field not being known yet.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(f"this expense cannot be marked ready until it has: {', '.join(missing)}")


class ExpenseAlreadyReady(CaptureError):
    """The claim has been released and is somebody else's to change now.

    FR-EXP-001c's "always editable" is about extraction never locking a field
    against the person filling the form in - not about a claim already in front
    of an approver. Returning one for correction belongs to FR-EXP-002's
    approval workflow, which owns that state and is not built.
    """


class UnknownVatTreatment(CaptureError):
    """A treatment code migration 0028's ruleset does not define."""


class VatRateUnavailable(CaptureError):
    """No rate is in force for this treatment on this date.

    Reachable for a date before the ruleset's floor row. Refused rather than
    defaulted to zero: a claim with no VAT because nobody knew the rate is
    indistinguishable from a genuinely zero-rated one, and only one of them is
    correct.
    """


class NotAuthorizedToCapture(CaptureError):
    def __init__(self, action: str, resource_type: str, detail: str) -> None:
        self.action, self.resource_type, self.detail = action, resource_type, detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")


@dataclass(frozen=True, slots=True)
class CapturePage:
    """One stored original.

    Counts STORED ORIGINALS, not sheets of paper. Four camera shots of a
    four-page invoice are four pages; a four-page PDF of the same invoice is
    one, because FR-DOC-001 stores the file unaltered and splitting it would
    mean storing four things the user never gave us.
    """

    id: uuid.UUID
    item_id: uuid.UUID
    document_id: uuid.UUID
    page_number: int
    source: CaptureSource
    content_type: DocumentContentType
    byte_size: int


@dataclass(frozen=True, slots=True)
class Expense:
    """FR-EXP-001a's "each becoming a separate expense".

    Every field but the linkage is optional, and that is FR-EXP-001c rather
    than laziness: "the product never blocks on extraction being available". A
    draft with nothing filled in is the correct state for a receipt that has
    just been photographed.
    """

    id: uuid.UUID
    administration_id: uuid.UUID
    capture_item_id: uuid.UUID
    status: ExpenseStatus
    submitted_by_user_id: uuid.UUID
    expense_date: date | None = None
    supplier: str | None = None
    #: NFR-031 / CLAUDE.md rule four. Decimal, matching journal_line's scale.
    #: `float` is rejected at the boundary rather than coerced - see
    #: `api.expenses.vat`.
    gross_amount: Decimal | None = None
    #: WHICH VAT applies. The rate below is derived from it and the date.
    vat_treatment: VatTreatment | None = None
    #: The rate `vat.rate_on()` gave for this expense's DATE, stored rather
    #: than recomputed so a later ruleset load cannot restate a historical
    #: claim (CMP-014).
    vat_rate: Decimal | None = None
    #: Rounded. See api.expenses.vat for why this half is the rounded one.
    vat_amount: Decimal | None = None
    #: Generated in the database as `gross - vat`, so `net + vat == gross` is
    #: an identity rather than something that usually holds (FR-GL-001).
    net_amount: Decimal | None = None
    category: str | None = None
    payment_method: PaymentMethod | None = None
    #: FR-EXP-001d. The ledger entry this claim produced, once confirmed.
    #: Written by `api.expenses.posting` through the ledger service - never by
    #: the form, and never twice (migration 0034).
    journal_entry_id: uuid.UUID | None = None
    posted_at: datetime | None = None
    #: The supplier's own number. Optional: not part of FR-EXP-001b's minimum.
    invoice_number: str | None = None
    #: FR-AP-002. How automatic reading filled the fields (status, provider,
    #: model, a confidence per field) - never the values. None: not read.
    extraction: dict[str, Any] | None = None

    #: FR-EXP-001b's "minimum", plus FR-EXP-001e's payment method. Named once,
    #: here, because three places need to agree on it: this type's
    #: `missing_fields`, migration 0033's `expense_ready_is_complete` CHECK,
    #: and the form service's refusal. The CHECK is the one that binds.
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "expense_date",
        "supplier",
        "gross_amount",
        "vat_treatment",
        "vat_rate",
        "vat_amount",
        "category",
        "payment_method",
    )

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """What still has to be given before this can be marked ready.

        Returned rather than raised, because FR-EXP-001c makes an incomplete
        expense a normal state - the form is filled in over time, and a screen
        needs to show what is outstanding rather than be refused for asking.
        """
        missing = []
        for field_name in self.REQUIRED_FIELDS:
            value = getattr(self, field_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field_name)
        return tuple(missing)

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def amounts_balance(self) -> bool:
        """`net + vat == gross`, checked on the way out of the database too.

        The database generates net, so this cannot fail there - which is the
        point of asserting it here: if it ever does, the row did not come from
        this schema.
        """
        if self.gross_amount is None or self.vat_amount is None or self.net_amount is None:
            return True
        return self.net_amount + self.vat_amount == self.gross_amount


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """One row of FR-EXP-001a's review list.

    What a person needs to decide whether an item is right: how many originals
    it holds, what they are, and whether the same file was captured twice in
    this sitting.
    """

    item_id: uuid.UUID
    position: int
    expense_id: uuid.UUID | None
    page_count: int
    content_types: tuple[str, ...]
    total_bytes: int
    discarded: bool
    #: The earlier item in this session holding byte-identical evidence.
    #:
    #: EXACT duplicates only - same bytes, same hash. This is deliberately NOT
    #: FR-EXP-001g, which matches on supplier, date and amount and needs
    #: extraction (FR-EXP-001c, P1). What it catches is the case extraction is
    #: not needed for and which a batch makes common: the same receipt
    #: photographed twice, or a file dropped in twice.
    duplicate_of: uuid.UUID | None = None

    @property
    def has_evidence(self) -> bool:
        return self.page_count > 0


@dataclass(frozen=True, slots=True)
class CaptureSession:
    id: uuid.UUID
    administration_id: uuid.UUID
    opened_by_user_id: uuid.UUID
    opened_at: datetime | None = None
    finalised_at: datetime | None = None
    items: tuple[ReviewEntry, ...] = field(default_factory=tuple)

    @property
    def is_open(self) -> bool:
        return self.finalised_at is None

    @property
    def live_items(self) -> tuple[ReviewEntry, ...]:
        """Items that will become expenses: everything not discarded in review."""
        return tuple(entry for entry in self.items if not entry.discarded)

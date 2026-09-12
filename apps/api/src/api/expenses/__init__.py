"""Receipt capture - PRD §6.7 (FR-EXP-001, FR-EXP-001a).

    model.py       the three levels, named apart: session -> item -> page
    capture.py     the one place both paths land
    repository.py  the SQL, under the request's own RLS session
    routes.py      one intake endpoint, and the review list

The distinction the whole feature turns on is that FR-EXP-001 and FR-EXP-001a
both say "several" and mean opposite things - several images of one receipt
versus several receipts. Migration 0032 makes the confusion unspellable: an
expense hangs off an ITEM with a UNIQUE constraint, and pages hang off the
item below it.
"""

from api.expenses.capture import (
    SUBMIT_EXPENSE,
    CaptureRepository,
    CaptureService,
    duplicates_in,
    expenses_that_would_be_created,
    status_after_finalisation,
)
from api.expenses.model import (
    CaptureError,
    CapturePage,
    CaptureSession,
    CaptureSource,
    EmptySession,
    Expense,
    ExpenseStatus,
    ItemNotFound,
    ItemWithoutPages,
    NotAuthorizedToCapture,
    PaymentMethod,
    ReviewEntry,
    SessionAlreadyFinalised,
    SessionNotFound,
)
from api.expenses.repository import SqlCaptureRepository

__all__ = [
    "SUBMIT_EXPENSE",
    "CaptureError",
    "CapturePage",
    "CaptureRepository",
    "CaptureService",
    "CaptureSession",
    "CaptureSource",
    "EmptySession",
    "Expense",
    "ExpenseStatus",
    "ItemNotFound",
    "ItemWithoutPages",
    "NotAuthorizedToCapture",
    "PaymentMethod",
    "ReviewEntry",
    "SessionAlreadyFinalised",
    "SessionNotFound",
    "SqlCaptureRepository",
    "duplicates_in",
    "expenses_that_would_be_created",
    "status_after_finalisation",
]

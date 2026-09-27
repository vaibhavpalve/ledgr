"""ADR-095: POST .../expenses/{id}/extraction - the handler's own decisions.

Tenant isolation for the route is in tests/integration/test_expense_form_isolation.py;
what the reading writes is in test_extraction.py. This is what the route decides
between them: when it refuses, and that it reads the STORED invoice.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.documents.model import DocumentNotFound, DocumentNotReleasable
from api.documents.scanning import ScanStatus
from api.expenses.form import ExpenseView
from api.expenses.model import Expense, ExpenseStatus
from api.expenses.routes import read_expense_again
from api.tenancy import TenantContext

ORG, ADMIN, USER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
EXPENSE, ITEM, DOCUMENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def a_request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
    )


def a_view(
    status: ExpenseStatus = ExpenseStatus.DRAFT, document_id: uuid.UUID | None = DOCUMENT
) -> ExpenseView:
    expense = Expense(
        id=EXPENSE,
        administration_id=ADMIN,
        capture_item_id=ITEM,
        status=status,
        submitted_by_user_id=USER,
    )
    return ExpenseView(
        expense=expense, suggested_category=None, missing_fields=(), document_id=document_id
    )


class FakeForm:
    def __init__(self, view: ExpenseView) -> None:
        self._view = view

    async def view(self, **_: Any) -> ExpenseView:
        return self._view


class FakeExtraction:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[dict[str, Any]] = []

    async def read_into_expense(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeDocuments:
    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure

    async def original(self, **_: Any) -> tuple[object, bytes]:
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(content_type=SimpleNamespace(value="application/pdf")), b"%PDF-1.7"


async def call(
    *,
    view: ExpenseView | None = None,
    extraction: FakeExtraction | None = None,
    documents: FakeDocuments | None = None,
) -> dict[str, object]:
    return await read_expense_again(
        administration_id=ADMIN,
        expense_id=EXPENSE,
        request=a_request(),
        tenant=TenantContext(organization_id=ORG, user_id=USER),
        form=FakeForm(view or a_view()),  # type: ignore[arg-type]
        extraction=extraction or FakeExtraction(),  # type: ignore[arg-type]
        documents=documents or FakeDocuments(),  # type: ignore[arg-type]
        _=None,  # type: ignore[arg-type]
    )


async def test_the_stored_invoice_is_read_into_the_expense() -> None:
    extraction = FakeExtraction()

    body = await call(extraction=extraction)

    (read,) = extraction.calls
    assert read["item_id"] == ITEM
    assert read["data"] == b"%PDF-1.7"
    assert read["content_type"] == "application/pdf"
    assert body["id"] == str(EXPENSE)


async def test_with_reading_switched_off_it_says_so_instead_of_pretending() -> None:
    with pytest.raises(HTTPException) as refused:
        await call(extraction=FakeExtraction(available=False))

    assert refused.value.status_code == 409
    assert refused.value.detail["reason"] == "extraction_unavailable"  # type: ignore[index]


@pytest.mark.parametrize("status", [ExpenseStatus.READY, ExpenseStatus.POSTED])
async def test_a_submitted_claim_is_not_read_again(status: ExpenseStatus) -> None:
    """Past draft, the form is closed to edits - and so to a reading."""
    extraction = FakeExtraction()

    with pytest.raises(HTTPException) as refused:
        await call(view=a_view(status=status), extraction=extraction)

    assert refused.value.status_code == 409
    assert extraction.calls == []


@pytest.mark.parametrize(
    ("failure", "status", "reason"),
    [
        (DocumentNotFound("gone"), 404, "document_not_found"),
        (DocumentNotReleasable(ScanStatus.PENDING), 409, "document_not_releasable"),
    ],
)
async def test_an_original_that_cannot_be_opened_is_refused_without_reading(
    failure: Exception, status: int, reason: str
) -> None:
    extraction = FakeExtraction()

    with pytest.raises(HTTPException) as refused:
        await call(documents=FakeDocuments(failure), extraction=extraction)

    assert refused.value.status_code == status
    assert refused.value.detail["reason"] == reason  # type: ignore[index]
    assert extraction.calls == []

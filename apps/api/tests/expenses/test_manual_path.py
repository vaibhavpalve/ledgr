"""FR-EXP-001c: manual entry is a complete path on its own.

    FR-EXP-001c  Fields are pre-filled on a best-effort basis in P0 and are
                 always editable. Confidence-scored extraction with a review
                 queue is P1 (see FR-AP-002); THE PRODUCT NEVER BLOCKS ON
                 EXTRACTION BEING AVAILABLE.

Automatic reading has since been built (ADR-081) - and this file is what says it
arrived the way it was required to: behind a switch that is OFF by default, in
its own sub-package, wired in by `routes.py` alone. The claim it protects is
easy to lose: a commit that put a model call between capture and posting would
break the requirement without breaking a single other test, because every other
test would still be exercising a system where extraction happened to succeed.

So this asserts what stays true with extraction present:

  1. the WHOLE path - capture, form, confirm, post - runs end to end with
     nothing pre-filling anything,
  2. the domain modules of `api.expenses` do not reach for extraction, and
  3. extraction is off by default and only the composition root touches it.
"""

from __future__ import annotations

import ast
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.documents.scanning import LocalPatternScanner
from api.documents.service import DocumentService
from api.documents.storage import InMemoryBlobStore
from api.expenses.capture import CaptureService
from api.expenses.form import ExpenseFormService
from api.expenses.model import (
    ExpenseStatus,
    PaymentMethod,
    VatTreatment,
)
from api.expenses.posting import ExpensePostingService
from tests.authz.helpers import build_world
from tests.documents.test_service import FakeDocumentRepository
from tests.expenses.test_capture import JPEG, FakeCaptureRepository
from tests.expenses.test_form import FakeFormRepository
from tests.expenses.test_posting import (
    ACCOUNTS,
    JOURNAL,
    PERIOD,
    FakePostingRepository,
    RecordingLedgerRepository,
)
from tests.support.fake_audit_repository import InMemoryAuditRepository

EXPENSES_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "api" / "expenses"

#: Names that would mean something is reading a receipt for us. Deliberately
#: broad: the point is to catch a dependency arriving, not to guess what it
#: will be called.
EXTRACTION_MARKERS = (
    "ocr",
    "extract",
    "tesseract",
    "textract",
    "document_intelligence",
    "form_recognizer",
    "recognize",
    "prefill",
    "autofill",
)

#: Words that legitimately appear in this package and merely contain a marker.
#: `extracted_fields` is a COLUMN - the place extraction will one day write -
#: and having somewhere to put a result is not the same as depending on one.
ALLOWED = (
    "extracted_fields",
    "extraction",  # only ever in prose explaining that it is absent
)


#: Where extraction is ALLOWED to live: its own sub-package (the switch and the
#: adapters behind it) and `routes.py`, the composition root that wires it in.
#: Everything else in `api.expenses` is the domain path - capture, the form, VAT,
#: duplicates, posting - and that stays free of it (see the tests below).
_EXTRACTION_HOME = EXPENSES_PACKAGE / "extraction"
_COMPOSITION_ROOT = EXPENSES_PACKAGE / "routes.py"


def _python_files() -> list[Path]:
    """The domain modules: every expense module that is not extraction itself."""
    return sorted(
        path
        for path in EXPENSES_PACKAGE.rglob("*.py")
        if _EXTRACTION_HOME not in path.parents and path != _COMPOSITION_ROOT
    )


def test_extraction_is_off_by_default_and_builds_nothing_when_off() -> None:
    """The switch FR-EXP-001c asks for: reading sends an invoice to a model
    (PRIV-010/011), so a deployment that has not chosen it reads nothing."""
    from api.config import Settings
    from api.expenses.extraction.build import build_extractor

    assert Settings.model_fields["extraction_provider"].default == "none"
    assert build_extractor(Settings(extraction_provider="none")) is None


def test_only_the_composition_root_reaches_into_extraction() -> None:
    """The domain path may not import it; `routes.py` wires it and nothing else
    outside its own package does."""
    importers = []
    for path in EXPENSES_PACKAGE.rglob("*.py"):
        if _EXTRACTION_HOME in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = " ".join(alias.name for alias in node.names)
            if "expenses.extraction" in module:
                importers.append(path.name)
    assert set(importers) == {"routes.py"}


def test_there_are_expense_modules_to_check() -> None:
    """A sweep that finds nothing passes completely and guarantees nothing."""
    assert len(_python_files()) >= 6


def test_no_expense_module_imports_anything_that_reads_a_receipt() -> None:
    """The structural half, and the one that survives extraction arriving.

    An import is how a dependency gets in. Checking the import graph rather
    than the text means a module that merely mentions OCR in a comment - as
    several here do, explaining that it is not used - does not trip this.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                lowered = name.lower()
                if any(marker in lowered for marker in EXTRACTION_MARKERS):
                    offenders.append(f"{path.name}: imports {name}")

    assert not offenders, (
        "FR-EXP-001c: the product never blocks on extraction being available, so "
        "nothing in api.expenses may depend on it. If extraction is arriving, put "
        "it behind a switch that is OFF by default and update this test to assert "
        "the manual path still works with it off:\n" + "\n".join(offenders)
    )


def test_no_expense_module_calls_out_to_an_extractor() -> None:
    """The textual half, over identifiers rather than prose.

    Catches a call added without an import - a method on something already
    injected, which is exactly how a dependency sneaks past the check above.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name is None or name in ALLOWED:
                continue
            lowered = name.lower()
            if any(marker in lowered for marker in EXTRACTION_MARKERS):
                offenders.append(f"{path.name}:{node.lineno}: {name}")

    assert not offenders, (
        "FR-EXP-001c: an identifier in api.expenses looks like it reads a receipt. "
        "Manual entry has to be a complete path with extraction switched off "
        "entirely:\n" + "\n".join(offenders)
    )


async def test_the_whole_path_works_with_nothing_pre_filling_anything() -> None:
    """Capture, form, confirm, post - by hand, end to end.

    Every field is typed. Nothing is read off the image, and the image is never
    parsed: it is stored, hashed, scanned for malware and retained, which is
    all FR-EXP-001d asks of it.

    This is the P0 path. If it ever stops working, the product blocks on
    something FR-EXP-001c says it must not.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Bookkeeper", scope_id=world.acme_books)
    authorization = AuthorizationService(world.repository)
    audit = InMemoryAuditRepository()
    audit_log = AuditLog(audit)

    # --- capture: a photograph, and nothing looks at it -------------------
    documents = FakeDocumentRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    capture_repository = FakeCaptureRepository(
        organization_id=world.acme,
        administration_id=world.acme_books,
        documents=documents,
    )
    capture = CaptureService(
        repository=capture_repository,
        documents=DocumentService(
            repository=documents,
            blobs=InMemoryBlobStore(),
            scanner=LocalPatternScanner(),
            authorization=authorization,
            audit_log=audit_log,
        ),
        authorization=authorization,
        audit_log=audit_log,
    )

    session = await capture.open_session(
        administration_id=world.acme_books, actor_user_id=world.user
    )
    from api.expenses.model import CaptureSource

    item_id, document, page = await capture.capture(
        administration_id=world.acme_books,
        session_id=session.id,
        actor_user_id=world.user,
        fiscal_year_id=uuid.uuid4(),
        data=JPEG,
        source=CaptureSource.CAMERA,
    )
    assert page == 1
    # The claim exists and is empty. Nothing has been read off the receipt.
    expense = next(iter(capture_repository.expenses.values()))
    assert expense.status is ExpenseStatus.DRAFT
    assert expense.missing_fields, "a captured claim is incomplete by design"
    assert document.derived_text is None, "nothing extracted any text"
    assert document.extracted_fields == {}, "and nothing filled any field"

    # --- form: every field typed by a person ------------------------------
    form_repository = FakeFormRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    form_repository.expenses[expense.id] = expense
    form = ExpenseFormService(
        repository=form_repository, authorization=authorization, audit_log=audit_log
    )

    opened = await form.view(
        administration_id=world.acme_books,
        expense_id=expense.id,
        actor_user_id=world.user,
    )
    assert opened.suggested_category is None, "no history yet, so no default"
    assert not opened.can_be_marked_ready

    filled = await form.update(
        administration_id=world.acme_books,
        expense_id=expense.id,
        actor_user_id=world.user,
        expense_date=date(2025, 6, 1),
        supplier="Albert Heijn",
        gross_amount=Decimal("121.00"),
        vat_treatment=VatTreatment.BTW_21,
        category="Kantoorbenodigdheden",
        payment_method=PaymentMethod.PERSONAL_REIMBURSABLE,
    )
    # FR-EXP-001b's derived figures, from typed input alone.
    assert filled.expense.vat_amount == Decimal("21.00")
    assert filled.expense.net_amount == Decimal("100.00")
    assert filled.can_be_marked_ready

    ready = await form.mark_ready(
        administration_id=world.acme_books,
        expense_id=expense.id,
        actor_user_id=world.user,
    )
    assert ready.expense.status is ExpenseStatus.READY

    # --- confirm: into the ledger -----------------------------------------
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    posting_repository = FakePostingRepository(
        organization_id=world.acme, administration_id=world.acme_books
    )
    posting_repository.expenses[expense.id] = ready.expense
    posting_repository.accounts = dict(ACCOUNTS)
    posting_repository.journal = JOURNAL
    posting_repository.open_periods[date(2025, 6, 1)] = PERIOD

    ledger_repository = RecordingLedgerRepository(organization_id=world.acme)
    from api.ledger.service import LedgerService

    posting = ExpensePostingService(
        repository=posting_repository,
        ledger=LedgerService(ledger_repository, audit_log),  # type: ignore[arg-type]
        authorization=authorization,
        audit_log=audit_log,
    )

    posted = await posting.post(
        administration_id=world.acme_books,
        expense_id=expense.id,
        actor_user_id=world.user,
    )

    # The claim is in the books, and the entry balances.
    entry = ledger_repository.posted[0]
    assert entry.total_debit == entry.total_credit == Decimal("121.00")
    assert posting_repository.expenses[expense.id].status is ExpenseStatus.POSTED
    assert posting_repository.expenses[expense.id].journal_entry_id == posted.id
    # FR-EXP-001d: the photograph is linked to the entry it produced.
    assert posting_repository.linked == [(expense.id, posted.id)]
    # And the item captured at the start is the one that became this claim.
    assert ready.expense.capture_item_id == item_id


@pytest.mark.parametrize(
    "field_name",
    ["expense_date", "supplier", "gross_amount", "vat_treatment", "category", "payment_method"],
)
async def test_every_required_field_is_reachable_by_typing(field_name: str) -> None:
    """Each of FR-EXP-001b's five plus FR-EXP-001e's one has a parameter on the
    form service.

    Stated per field rather than as a set comparison, so a field that lost its
    way into the form - accepted only from an extraction payload, say - fails
    with its own name.
    """
    import inspect

    parameters = inspect.signature(ExpenseFormService.update).parameters
    assert field_name in parameters, (
        f"{field_name} cannot be typed into the form. FR-EXP-001c requires manual "
        f"entry to be a complete path."
    )

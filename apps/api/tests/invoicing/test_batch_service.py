"""api.invoicing.batch_service - one pass, each entry on its own (ADR-079).

Fakes for the repository and for `InvoicingService`: under test are that every entry is
attempted independently, that failures are reported with a code and never spoil the rest, what
issuing does when refused, retries by key, and who may run one.
`tests/integration/test_invoice_batches.py` runs the same against Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.customers.model import CustomerAddressIncomplete, CustomerIsArchived, CustomerNotFound
from api.invoicing.approval import ApprovalRequired, ApprovalState
from api.invoicing.batch_service import (
    BatchInvoicingService,
    BatchItem,
    BatchKeyTaken,
    BatchRecord,
    BatchRejected,
    InvoiceBatchNotFound,
)
from api.invoicing.batches import BatchEntry
from api.invoicing.model import (
    InvoicingError,
    NotAuthorizedToInvoice,
    NotStatutoryCompliant,
)
from api.invoicing.posting import NoOpenPeriod
from api.invoicing.quotes import QuoteLine
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 10, 0, 0).astimezone()
YEAR = uuid.uuid4()


def line(**kw: object) -> QuoteLine:
    defaults: dict[str, object] = dict(
        description="Contributie", quantity=Decimal("1"), unit_price=Decimal("250"),
        vat_treatment="btw_21",
    )  # fmt: skip
    defaults.update(kw)
    return QuoteLine(**defaults)  # type: ignore[arg-type]


class FakeInvoicing:
    """Stands in for `InvoicingService`: creates and issues, or raises what it is told to."""

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.issued: list[uuid.UUID] = []
        self.create_errors: dict[uuid.UUID, Exception] = {}
        self.issue_errors: dict[uuid.UUID, Exception] = {}
        self.by_invoice: dict[uuid.UUID, uuid.UUID] = {}

    async def create_draft(self, **kwargs: object):  # type: ignore[no-untyped-def]
        customer = kwargs["customer_id"]
        assert isinstance(customer, uuid.UUID)
        if customer in self.create_errors:
            raise self.create_errors[customer]
        invoice_id = uuid.uuid4()
        self.by_invoice[invoice_id] = customer
        self.created.append(kwargs)
        return SimpleNamespace(invoice=SimpleNamespace(id=invoice_id))

    async def issue(self, *, administration_id, invoice_id, actor_user_id):  # type: ignore[no-untyped-def]
        customer = self.by_invoice[invoice_id]
        if customer in self.issue_errors:
            raise self.issue_errors[customer]
        self.issued.append(invoice_id)


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    year: uuid.UUID | None = YEAR
    batches: dict[uuid.UUID, BatchRecord] = field(default_factory=dict)
    items: list[BatchItem] = field(default_factory=list)
    key_lost: bool = False

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        yield

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def fiscal_year_for(self, *, administration_id, on):  # type: ignore[no-untyped-def]
        return self.year

    async def batch_by_key(self, *, administration_id, batch_key):  # type: ignore[no-untyped-def]
        return next((b for b in self.batches.values() if b.batch_key == batch_key), None)

    async def insert_batch(self, batch, items):  # type: ignore[no-untyped-def]
        if self.key_lost:
            raise BatchKeyTaken(batch.batch_key or "")
        self.batches[batch.id] = batch
        self.items.extend(items)

    async def get_batch(self, *, administration_id, batch_id):  # type: ignore[no-untyped-def]
        return self.batches.get(batch_id)

    async def items_for_batch(self, *, administration_id, batch_id):  # type: ignore[no-untyped-def]
        return [i for i in self.items if i.batch_id == batch_id]

    async def list_batches(self, *, administration_id, limit):  # type: ignore[no-untyped-def]
        return list(self.batches.values())


@dataclass
class Setup:
    service: BatchInvoicingService
    repository: FakeRepository
    invoicing: FakeInvoicing
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def run(self, entries: list[BatchEntry], **overrides: object):  # type: ignore[no-untyped-def]
        params: dict[str, object] = dict(
            administration_id=self.administration,
            actor_user_id=self.user,
            name="Contributie 2026",
            entries=entries,
            today=TODAY,
            now=NOW,
            default_lines=(line(),),
        )
        params.update(overrides)
        return await self.service.run(**params)  # type: ignore[arg-type]


def _setup(*, role: str = "Bookkeeper", **repository_overrides: object) -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(organization_id=world.acme)
    for name, value in repository_overrides.items():
        setattr(repository, name, value)
    invoicing = FakeInvoicing()
    audit = InMemoryAuditRepository()
    service = BatchInvoicingService(
        repository=repository,  # type: ignore[arg-type]
        invoicing=invoicing,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, invoicing, audit, world.user, world.acme_books)


def entries(count: int) -> list[BatchEntry]:
    return [BatchEntry(customer_id=uuid.uuid4()) for _ in range(count)]


# --- drafting ----------------------------------------
async def test_every_entry_becomes_a_draft_dated_and_termed_as_asked() -> None:
    setup = _setup()
    batch_entries = [
        BatchEntry(customer_id=uuid.uuid4()),
        BatchEntry(customer_id=uuid.uuid4(), due_days=30, notes="Eigen notitie"),
    ]

    result = await setup.run(
        batch_entries,
        invoice_date=date(2026, 10, 1),
        default_due_days=14,
        default_notes="Bedankt",
    )

    assert [i.status for i in result.items] == ["drafted", "drafted"]
    assert all(i.invoice_id for i in result.items)
    first, second = setup.invoicing.created
    assert first["invoice_date"] == date(2026, 10, 1)
    assert first["due_date"] == date(2026, 10, 1) + timedelta(days=14)
    assert first["notes"] == "Bedankt" and first["fiscal_year_id"] == YEAR
    assert second["due_date"] == date(2026, 10, 1) + timedelta(days=30)
    assert second["notes"] == "Eigen notitie"
    assert (result.batch.item_count, result.batch.drafted_count) == (2, 2)
    assert setup.invoicing.issued == []  # nothing issues unless asked


async def test_the_invoice_date_defaults_to_today_and_no_due_date_means_the_customers_terms() -> (
    None
):
    setup = _setup()
    await setup.run(entries(1))
    (created,) = setup.invoicing.created
    assert created["invoice_date"] == TODAY
    assert created["due_date"] is None  # `create_draft` applies the customer's own terms


async def test_lines_are_copied_exactly_as_given() -> None:
    setup = _setup()
    own = (line(description="Verbruik", quantity=Decimal("37.5"), unit_price=Decimal("0.035")),)
    await setup.run([BatchEntry(customer_id=uuid.uuid4(), lines=own)])
    (created,) = setup.invoicing.created
    (created_line,) = created["lines"]  # type: ignore[misc]
    assert (created_line.quantity, created_line.unit_price) == (Decimal("37.5"), Decimal("0.035"))


# --- one failure never spoils the rest ----------------------------------------
async def test_a_failing_entry_is_reported_with_a_code_and_the_others_still_run() -> None:
    setup = _setup()
    batch_entries = entries(5)
    setup.invoicing.create_errors = {
        batch_entries[1].customer_id: CustomerNotFound("x"),
        batch_entries[2].customer_id: CustomerIsArchived("x"),
        batch_entries[3].customer_id: CustomerAddressIncomplete("x"),
    }

    result = await setup.run(batch_entries)

    assert [(i.position, i.status, i.error_code) for i in result.items] == [
        (1, "drafted", None),
        (2, "failed", "customer_not_found"),
        (3, "failed", "customer_archived"),
        (4, "failed", "customer_details_incomplete"),
        (5, "drafted", None),
    ]
    assert all(i.invoice_id is None for i in result.items if i.status == "failed")
    summary = result.batch
    assert (summary.drafted_count, summary.issued_count, summary.failed_count) == (2, 0, 3)


async def test_an_unexpected_invoicing_error_is_invoice_invalid_not_a_crash() -> None:
    setup = _setup()
    batch_entries = entries(2)
    setup.invoicing.create_errors = {batch_entries[0].customer_id: InvoicingError("bad line")}
    result = await setup.run(batch_entries)
    assert [i.error_code for i in result.items] == ["invoice_invalid", None]


async def test_a_date_in_no_fiscal_year_fails_every_entry_with_that_reason() -> None:
    setup = _setup(year=None)
    result = await setup.run(entries(3))
    assert {i.error_code for i in result.items} == {"no_fiscal_year"}
    assert setup.invoicing.created == []
    assert result.batch.failed_count == 3  # still recorded: somebody asked


async def test_a_permission_failure_deep_down_is_not_swallowed() -> None:
    setup = _setup()
    batch_entries = entries(1)
    setup.invoicing.create_errors = {
        batch_entries[0].customer_id: NotAuthorizedToInvoice("a", "b", "c")
    }
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.run(batch_entries)
    assert setup.repository.batches == {}


# --- issuing ----------------------------------------
async def test_with_issue_each_draft_is_issued() -> None:
    setup = _setup()
    result = await setup.run(entries(3), issue=True)
    assert [i.status for i in result.items] == ["issued"] * 3
    assert len(setup.invoicing.issued) == 3
    assert (result.batch.issued_count, result.batch.drafted_count) == (3, 0)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotAuthorizedToInvoice("send", "sales_invoice", "no"), "not_authorized_to_issue"),
        (ApprovalRequired(ApprovalState.NONE), "approval_required"),
        (NotStatutoryCompliant([]), "not_statutory_compliant"),
        (NoOpenPeriod("closed"), "no_open_period"),
    ],
)
async def test_an_entry_that_cannot_be_issued_stays_a_draft_with_the_reason(
    error: Exception, code: str
) -> None:
    setup = _setup()
    batch_entries = entries(2)
    setup.invoicing.issue_errors = {batch_entries[0].customer_id: error}

    result = await setup.run(batch_entries, issue=True)

    refused, fine = result.items
    assert (refused.status, refused.issue_error) == ("drafted", code)
    assert refused.invoice_id is not None  # the draft is there for a person to finish
    assert (fine.status, fine.issue_error) == ("issued", None)
    assert (result.batch.drafted_count, result.batch.issued_count) == (1, 1)


# --- rejecting the whole batch ----------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "code", "position"),
    [
        ({"name": "  "}, "name_required", None),
        ({"entries": []}, "empty", None),
        ({"default_lines": None}, "lines", 1),
    ],
)
async def test_an_unusable_batch_is_rejected_before_anything_is_created(
    kwargs: dict[str, object], code: str, position: int | None
) -> None:
    setup = _setup()
    with pytest.raises(BatchRejected) as rejected:
        await setup.run(kwargs.pop("entries", entries(2)), **kwargs)  # type: ignore[arg-type]
    assert (rejected.value.code, rejected.value.position) == (code, position)
    assert setup.invoicing.created == [] and setup.repository.batches == {}


# --- retries ----------------------------------------
async def test_a_repeated_batch_key_returns_the_first_batch_and_creates_nothing() -> None:
    setup = _setup()
    first = await setup.run(entries(2), batch_key="2026-09 contributie")
    created = len(setup.invoicing.created)

    again = await setup.run(entries(2), batch_key="  2026-09 contributie  ")

    assert again.already_exists and not first.already_exists
    assert again.batch.id == first.batch.id
    assert [i.id for i in again.items] == [i.id for i in first.items]
    assert len(setup.invoicing.created) == created  # nothing new was drafted


async def test_no_key_means_every_request_is_a_new_batch() -> None:
    setup = _setup()
    a = await setup.run(entries(1))
    b = await setup.run(entries(1))
    assert a.batch.id != b.batch.id and not b.already_exists


async def test_losing_the_key_race_raises_so_the_whole_request_rolls_back() -> None:
    setup = _setup(key_lost=True)
    with pytest.raises(BatchKeyTaken):
        await setup.run(entries(1), batch_key="k")


# --- who and what is recorded ----------------------------------------
async def test_a_viewer_cannot_run_a_batch_and_the_denial_is_audited() -> None:
    setup = _setup(role="Viewer")
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.run(entries(1))
    assert setup.invoicing.created == []
    assert any(e.outcome.value == "denied" for e in setup.audit._entries)


async def test_the_run_is_audited_with_counts_not_customers() -> None:
    setup = _setup()
    batch_entries = entries(3)
    setup.invoicing.create_errors = {batch_entries[0].customer_id: CustomerNotFound("x")}
    await setup.run(batch_entries, issue=True)
    entry = next(e for e in setup.audit._entries if e.action == "run_sales_invoice_batch")
    assert entry.detail["entries"] == 3 and entry.detail["failed"] == 1
    assert entry.detail["issued"] == 2 and entry.detail["issue_requested"] is True
    assert str(batch_entries[0].customer_id) not in str(entry.detail)


async def test_a_batch_can_be_read_back_and_an_unknown_one_is_not_found() -> None:
    setup = _setup()
    result = await setup.run(entries(2))
    read = await setup.service.batch(
        administration_id=setup.administration,
        actor_user_id=setup.user,
        batch_id=result.batch.id,
    )
    assert [i.position for i in read.items] == [1, 2]
    with pytest.raises(InvoiceBatchNotFound):
        await setup.service.batch(
            administration_id=setup.administration,
            actor_user_id=setup.user,
            batch_id=uuid.uuid4(),
        )
    listing = await setup.service.batches(
        administration_id=setup.administration, actor_user_id=setup.user
    )
    assert [b.id for b in listing] == [result.batch.id]

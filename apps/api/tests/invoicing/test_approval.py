"""SI-16's fingerprint, gate and service - api.invoicing.approval (ADR-078).

Under test: that an approval is of a specific version of the draft, who may request and who may
approve, what the gate refuses and when, and that a re-request replaces the old one.
`tests/integration/test_invoice_approval.py` runs the workflow end to end against Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.approval import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalNotReady,
    ApprovalNotRequired,
    ApprovalReasonMissing,
    ApprovalRequired,
    ApprovalService,
    ApprovalStale,
    ApprovalState,
    InvoiceApproval,
    QueueEntry,
    SalesInvoiceApprovalGate,
    content_fingerprint,
    state_of,
)
from api.invoicing.model import (
    InvoiceAlreadyIssued,
    InvoiceLine,
    InvoiceStatus,
    NotAuthorizedToInvoice,
    SalesInvoice,
)
from api.invoicing.statutory import StatutoryFailure
from api.vat.rules import TreatmentRole
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

D = Decimal
NOW = datetime(2026, 9, 21, 10, 0, 0).astimezone()


def line(position: int = 1, **kw: object) -> InvoiceLine:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        position=position,
        description="Advies",
        quantity=D("10"),
        unit_price=D("95"),
        discount_percent=D("0"),
        vat_treatment="btw_21",
        role=TreatmentRole.STANDARD,
        line_net=D("950.00"),
    )
    defaults.update(kw)
    return InvoiceLine(**defaults)  # type: ignore[arg-type]


def invoice(administration_id: uuid.UUID | None = None, **kw: object) -> SalesInvoice:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        administration_id=administration_id or uuid.uuid4(),
        fiscal_year_id=uuid.uuid4(),
        status=InvoiceStatus.DRAFT,
        invoice_number=None,
        number_prefix=None,
        invoice_reference=None,
        invoice_date=date(2026, 9, 21),
        supply_date=None,
        due_date=date(2026, 10, 21),
        customer_name="De Vries Holding B.V.",
        customer_address="Damrak 70, Amsterdam",
        customer_country="NL",
        customer_vat_number=None,
        customer_id=None,
        customer_language=Language.NL,
        credits_invoice_id=None,
        notes=None,
        issued_at=None,
        lines=(line(),),
    )
    defaults.update(kw)
    return SalesInvoice(**defaults)  # type: ignore[arg-type]


# --- the fingerprint ----------------------------------------
def test_the_fingerprint_is_stable_and_ignores_ids_and_decimal_scale() -> None:
    a = invoice()
    b = replace(
        a,
        id=uuid.uuid4(),
        lines=(line(id=uuid.uuid4(), unit_price=D("95.0000"), quantity=D("10.00")),),
    )
    assert content_fingerprint(a) == content_fingerprint(b)
    assert len(content_fingerprint(a)) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"customer_name": "Andere B.V."},
        {"customer_address": "Elders 1"},
        {"customer_vat_number": "NL123456789B01"},
        {"invoice_date": date(2026, 9, 22)},
        {"due_date": date(2026, 11, 1)},
        {"supply_date": date(2026, 9, 1)},
        {"notes": "Betaal snel"},
        {"customer_language": Language.EN},
        {"lines": (line(unit_price=D("950")),)},
        {"lines": (line(quantity=D("11")),)},
        {"lines": (line(discount_percent=D("5")),)},
        {"lines": (line(description="Andere dienst"),)},
        {"lines": (line(vat_treatment="btw_9"),)},
        {"lines": (line(), line(2, description="Extra"))},
        {"lines": ()},
    ],
)
def test_any_change_to_what_the_customer_receives_changes_the_fingerprint(
    change: dict[str, object],
) -> None:
    base = invoice()
    assert content_fingerprint(replace(base, **change)) != content_fingerprint(base)


def test_line_order_by_position_not_by_storage_order() -> None:
    first, second = line(1, description="A"), line(2, description="B")
    assert content_fingerprint(invoice(lines=(first, second))) == content_fingerprint(
        invoice(lines=(second, first))
    )


# --- state ----------------------------------------
def approval(status: str, content_hash: str, **kw: object) -> InvoiceApproval:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        content_hash=content_hash,
        status=status,
        requested_by_user_id=uuid.uuid4(),
        requested_at=NOW,
    )
    defaults.update(kw)
    return InvoiceApproval(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "same_hash", "expected"),
    [
        (None, True, ApprovalState.NONE),
        ("superseded", True, ApprovalState.NONE),
        ("pending", True, ApprovalState.PENDING),
        ("pending", False, ApprovalState.PENDING),
        ("rejected", True, ApprovalState.REJECTED),
        ("approved", True, ApprovalState.APPROVED),
        ("approved", False, ApprovalState.STALE),
    ],
)
def test_state_of(status: str | None, same_hash: bool, expected: ApprovalState) -> None:
    current = "a" * 64
    found = None if status is None else approval(status, current if same_hash else "b" * 64)
    assert state_of(found, current) is expected


# --- fakes ----------------------------------------
@dataclass
class FakeRepository:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    required: bool = True
    approvals: list[InvoiceApproval] = field(default_factory=list)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        yield

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def approval_required(self, *, administration_id: uuid.UUID) -> bool:
        return self.required

    async def create_request(self, *, administration_id, invoice_id, content_hash, user_id, note):  # type: ignore[no-untyped-def]
        self.approvals = [
            replace(a, status="superseded")
            if a.invoice_id == invoice_id and a.status in ("pending", "approved")
            else a
            for a in self.approvals
        ]
        created = approval(
            "pending",
            content_hash,
            administration_id=administration_id,
            invoice_id=invoice_id,
            requested_by_user_id=user_id,
            request_note=note,
        )
        self.approvals.append(created)
        return created

    async def pending_for_invoice(self, *, administration_id, invoice_id):  # type: ignore[no-untyped-def]
        return next(
            (a for a in self.approvals if a.invoice_id == invoice_id and a.status == "pending"),
            None,
        )

    async def latest_for_invoice(self, *, administration_id, invoice_id):  # type: ignore[no-untyped-def]
        mine = [a for a in self.approvals if a.invoice_id == invoice_id]
        return mine[-1] if mine else None

    async def decide(self, *, administration_id, approval_id, status, user_id, reason):  # type: ignore[no-untyped-def]
        current = next(a for a in self.approvals if a.id == approval_id)
        if current.status != "pending":
            return None
        decided = replace(
            current,
            status=status,
            decided_by_user_id=user_id,
            decided_at=NOW,
            decision_reason=reason,
        )
        self.approvals[self.approvals.index(current)] = decided
        return decided

    async def queue(self, *, administration_id, status, limit):  # type: ignore[no-untyped-def]
        return [
            QueueEntry(a, "De Vries", date(2026, 9, 21), None)
            for a in self.approvals
            if a.status == status
        ]


class FakeInvoices:
    """Stands in for `InvoicingService.view`."""

    def __init__(self, current: SalesInvoice) -> None:
        self.current = current
        self.failures: tuple[StatutoryFailure, ...] = ()

    async def view(self, *, administration_id, invoice_id, actor_user_id):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            invoice=self.current,
            statutory_failures=self.failures,
            can_be_issued=self.current.is_draft and not self.failures,
        )


@dataclass
class Setup:
    repository: FakeRepository
    invoices: FakeInvoices
    gate: SalesInvoiceApprovalGate
    service: ApprovalService
    audit: InMemoryAuditRepository
    drafter: uuid.UUID
    owner: uuid.UUID
    administration: uuid.UUID

    @property
    def draft(self) -> SalesInvoice:
        return self.invoices.current


def _setup(*, drafter_role: str = "Bookkeeper", **repository_overrides: object) -> Setup:
    world = build_world()
    drafter, owner = world.user, uuid.uuid4()
    world.repository.assign(user_id=drafter, role=drafter_role, scope_id=world.acme_books)
    world.repository.assign(user_id=owner, role="Owner", scope_id=world.acme)
    repository = FakeRepository(administration_id=world.acme_books, organization_id=world.acme)
    for name, value in repository_overrides.items():
        setattr(repository, name, value)
    invoices = FakeInvoices(invoice(world.acme_books))
    authorization = AuthorizationService(world.repository)
    audit = InMemoryAuditRepository()
    return Setup(
        repository=repository,
        invoices=invoices,
        gate=SalesInvoiceApprovalGate(repository, authorization),  # type: ignore[arg-type]
        service=ApprovalService(
            repository=repository,  # type: ignore[arg-type]
            invoices=invoices,
            authorization=authorization,
            audit_log=AuditLog(audit),
        ),
        audit=audit,
        drafter=drafter,
        owner=owner,
        administration=world.acme_books,
    )


async def _request(setup: Setup, **kw: object) -> InvoiceApproval:
    return await setup.service.request(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.drafter,
        **kw,  # type: ignore[arg-type]
    )


async def _approve(setup: Setup) -> InvoiceApproval:
    return await setup.service.approve(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.owner,
    )


# --- the gate ----------------------------------------
async def test_with_approval_switched_off_anyone_who_may_issue_may_issue() -> None:
    setup = _setup(required=False)
    await setup.gate.check(setup.draft, actor_user_id=setup.drafter)  # no error


async def test_a_bookkeeper_cannot_issue_an_unapproved_draft() -> None:
    setup = _setup()
    with pytest.raises(ApprovalRequired) as refused:
        await setup.gate.check(setup.draft, actor_user_id=setup.drafter)
    assert refused.value.state is ApprovalState.NONE


async def test_the_owner_may_issue_without_a_request_because_they_hold_the_authority() -> None:
    setup = _setup()
    await setup.gate.check(setup.draft, actor_user_id=setup.owner)


async def test_a_pending_or_rejected_request_does_not_open_the_gate() -> None:
    setup = _setup()
    await _request(setup)
    with pytest.raises(ApprovalRequired) as pending:
        await setup.gate.check(setup.draft, actor_user_id=setup.drafter)
    assert pending.value.state is ApprovalState.PENDING

    await setup.service.reject(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.owner,
        reason="Verkeerd bedrag",
    )
    with pytest.raises(ApprovalRequired) as rejected:
        await setup.gate.check(setup.draft, actor_user_id=setup.drafter)
    assert rejected.value.state is ApprovalState.REJECTED


async def test_an_approved_draft_may_be_issued_by_the_drafter() -> None:
    setup = _setup()
    await _request(setup)
    await _approve(setup)
    await setup.gate.check(setup.draft, actor_user_id=setup.drafter)


async def test_editing_after_approval_closes_the_gate_again() -> None:
    """The point of binding an approval to a version: a small invoice approved and then
    changed into a large one must not go out."""
    setup = _setup()
    await _request(setup)
    await _approve(setup)

    setup.invoices.current = replace(setup.draft, lines=(line(unit_price=D("95000")),))

    with pytest.raises(ApprovalRequired) as refused:
        await setup.gate.check(setup.draft, actor_user_id=setup.drafter)
    assert refused.value.state is ApprovalState.STALE


async def test_a_new_request_replaces_the_old_one_and_can_be_approved() -> None:
    setup = _setup()
    await _request(setup)
    await _approve(setup)
    setup.invoices.current = replace(setup.draft, notes="Aangepast")
    await _request(setup)  # asks again

    assert [a.status for a in setup.repository.approvals] == ["superseded", "pending"]
    await _approve(setup)
    await setup.gate.check(setup.draft, actor_user_id=setup.drafter)


# --- requesting ----------------------------------------
async def test_a_request_records_who_and_binds_to_the_current_contents() -> None:
    setup = _setup()

    created = await _request(setup, note="  Graag vandaag nog  ")

    assert created.status == "pending" and created.requested_by_user_id == setup.drafter
    assert created.content_hash == content_fingerprint(setup.draft)
    assert created.request_note == "Graag vandaag nog"
    assert setup.audit._entries[-1].action == "request_sales_invoice_approval"


async def test_nothing_to_request_when_approval_is_not_switched_on() -> None:
    setup = _setup(required=False)
    with pytest.raises(ApprovalNotRequired):
        await _request(setup)


async def test_an_incomplete_draft_cannot_be_put_up_for_approval() -> None:
    setup = _setup()
    setup.invoices.failures = (SimpleNamespace(field=SimpleNamespace(value="customer_address")),)  # type: ignore[assignment]
    with pytest.raises(ApprovalNotReady):
        await _request(setup)
    assert setup.repository.approvals == []


async def test_an_issued_invoice_cannot_be_put_up_for_approval() -> None:
    setup = _setup()
    setup.invoices.current = replace(setup.draft, status=InvoiceStatus.ISSUED)
    with pytest.raises(InvoiceAlreadyIssued):
        await _request(setup)


# --- deciding ----------------------------------------
async def test_a_bookkeeper_may_request_but_not_approve_or_reject() -> None:
    """The person who drafts must not be able to release."""
    setup = _setup()
    await _request(setup)
    for act in (
        setup.service.approve(
            administration_id=setup.administration,
            invoice_id=setup.draft.id,
            actor_user_id=setup.drafter,
        ),
        setup.service.reject(
            administration_id=setup.administration,
            invoice_id=setup.draft.id,
            actor_user_id=setup.drafter,
            reason="x",
        ),
        setup.service.queue(administration_id=setup.administration, actor_user_id=setup.drafter),
    ):
        with pytest.raises(NotAuthorizedToInvoice):
            await act
    assert setup.repository.approvals[0].status == "pending"
    assert any(e.outcome.value == "denied" for e in setup.audit._entries)


async def test_an_accountant_cannot_approve_either() -> None:
    setup = _setup(drafter_role="Accountant")
    await _request(setup)
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.approve(
            administration_id=setup.administration,
            invoice_id=setup.draft.id,
            actor_user_id=setup.drafter,
        )


async def test_approving_a_draft_that_changed_since_the_request_is_refused() -> None:
    setup = _setup()
    await _request(setup)
    setup.invoices.current = replace(setup.draft, lines=(line(quantity=D("1000")),))

    with pytest.raises(ApprovalStale):
        await _approve(setup)

    assert setup.repository.approvals[0].status == "pending"  # nothing was approved


async def test_approve_needs_a_pending_request_and_decides_once() -> None:
    setup = _setup()
    with pytest.raises(ApprovalNotFound):
        await _approve(setup)
    await _request(setup)
    approved = await _approve(setup)
    assert approved.status == "approved" and approved.decided_by_user_id == setup.owner
    with pytest.raises(ApprovalNotFound):  # no longer pending
        await _approve(setup)


async def test_a_lost_decision_race_is_reported_not_double_applied() -> None:
    setup = _setup()
    await _request(setup)

    async def lost(**kwargs: object) -> None:
        return None

    setup.repository.decide = lost  # type: ignore[method-assign]
    with pytest.raises(ApprovalAlreadyDecided):
        await _approve(setup)


async def test_a_rejection_needs_a_reason_which_is_kept_but_not_audited() -> None:
    setup = _setup()
    await _request(setup)
    with pytest.raises(ApprovalReasonMissing):
        await setup.service.reject(
            administration_id=setup.administration,
            invoice_id=setup.draft.id,
            actor_user_id=setup.owner,
            reason="   ",
        )
    rejected = await setup.service.reject(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.owner,
        reason="Verkeerd btw-tarief",
    )
    assert rejected.status == "rejected" and rejected.decision_reason == "Verkeerd btw-tarief"
    assert "btw-tarief" not in str(setup.audit._entries[-1].detail)


# --- status and queue ----------------------------------------
async def test_status_says_whether_the_caller_could_issue_now() -> None:
    setup = _setup()
    status = await setup.service.status(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.drafter,
    )
    assert status.required and status.state is ApprovalState.NONE and not status.can_issue

    await _request(setup)
    await _approve(setup)
    status = await setup.service.status(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.drafter,
    )
    assert status.state is ApprovalState.APPROVED and status.can_issue

    owners = await setup.service.status(
        administration_id=setup.administration,
        invoice_id=setup.draft.id,
        actor_user_id=setup.owner,
    )
    assert owners.can_issue


async def test_the_queue_lists_what_is_waiting_for_the_owner() -> None:
    setup = _setup()
    await _request(setup)
    queue = await setup.service.queue(
        administration_id=setup.administration, actor_user_id=setup.owner
    )
    assert [entry.approval.invoice_id for entry in queue] == [setup.draft.id]

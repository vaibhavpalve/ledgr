"""SI-11 - DunningService.chase, the bulk "chase everything overdue" (ADR-073).

What is under test is the safety design, not the loop: that a formal notice is never
sent by a plain "chase everything", that a reviewed step which has since changed is
skipped rather than sent as something else, that one customer's failure does not stop
the rest, that the cap defers rather than drops, and that repeating a call cannot
escalate anybody.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.delivery import DeliveryChannel, DeliveryStatus, UnreachableCustomer
from api.invoicing.delivery_service import DeliveryRecord
from api.invoicing.dunning import (
    DEFAULT_LADDER,
    Blocker,
    DunningAssessment,
    InterestRate,
    InterestRateKind,
    LadderStep,
    StepKind,
)
from api.invoicing.dunning_service import (
    MAX_CHASE_PER_CALL,
    ChaseFailure,
    ChaseItem,
    ChaseSkip,
    ChaseStatus,
    DunningInvoice,
    DunningService,
    ReminderAlreadySent,
)
from api.invoicing.model import NotAuthorizedToInvoice
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

DUE = date(2026, 1, 1)
RATE_10 = [InterestRate(valid_from=date(2025, 1, 1), rate=Decimal("10"))]


def _today(days_overdue: int) -> date:
    return DUE + timedelta(days=days_overdue)


# --- fakes ---------------------------------------------------------------------


@dataclass
class FakeDelivery:
    """Answers per invoice: sent by default, or refused / failing as told."""

    status_for: dict[uuid.UUID, DeliveryStatus] = field(default_factory=dict)
    raises_for: dict[uuid.UUID, Exception] = field(default_factory=dict)
    sent_to: list[uuid.UUID] = field(default_factory=list)

    async def dispatch(self, **kwargs: object) -> DeliveryRecord:
        invoice_id = kwargs["invoice_id"]
        assert isinstance(invoice_id, uuid.UUID)
        if invoice_id in self.raises_for:
            raise self.raises_for[invoice_id]
        status = self.status_for.get(invoice_id, DeliveryStatus.SENT)
        if status is DeliveryStatus.SENT:
            self.sent_to.append(invoice_id)
        return DeliveryRecord(
            id=uuid.uuid4(),
            invoice_id=invoice_id,
            channel=DeliveryChannel.EMAIL,
            status=status,
            recipient="klant@example.com",
            language=Language.NL,
            attempts=1,
        )


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    invoices: list[DunningInvoice] = field(default_factory=list)
    ladder_rows: tuple[LadderStep, ...] | None = None
    recorded: list[uuid.UUID] = field(default_factory=list)
    raises_on_record: dict[uuid.UUID, Exception] = field(default_factory=dict)
    savepoints_opened: int = 0
    savepoints_rolled_back: int = 0

    async def organization_of(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.organization_id

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        self.savepoints_opened += 1
        try:
            yield
        except Exception:
            self.savepoints_rolled_back += 1
            raise

    async def ladder(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.ladder_rows

    async def rates(self, *, kind: InterestRateKind):  # type: ignore[no-untyped-def]
        return list(RATE_10)

    async def overdue(self, *, administration_id: uuid.UUID, today: date):  # type: ignore[no-untyped-def]
        return list(self.invoices)

    async def record_reminder(  # type: ignore[no-untyped-def]
        self, *, administration_id, invoice_id, assessment, delivery_id, user_id
    ):
        if invoice_id in self.raises_on_record:
            raise self.raises_on_record[invoice_id]
        self.recorded.append(invoice_id)


@dataclass
class Setup:
    service: DunningService
    repository: FakeRepository
    delivery: FakeDelivery
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def chase(self, days: int = 10, items: list[ChaseItem] | None = None):  # type: ignore[no-untyped-def]
        return await self.service.chase(
            administration_id=self.administration,
            actor_user_id=self.user,
            today=_today(days),
            items=items,
        )


def _invoice(**overrides: object) -> DunningInvoice:
    defaults: dict[str, object] = dict(
        invoice_id=uuid.uuid4(),
        invoice_reference="2026-1",
        invoice_date=date(2025, 12, 1),
        due_date=DUE,
        customer_id=uuid.uuid4(),
        customer_name="De Vries Holding B.V.",
        outstanding=Decimal("1000.00"),
        is_business=True,
        is_paused=False,
        sent_positions=frozenset(),
        last_sent_on=None,
    )
    defaults.update(overrides)
    return DunningInvoice(**defaults)  # type: ignore[arg-type]


def _setup(*invoices: DunningInvoice, role: str = "Accountant") -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(organization_id=world.acme, invoices=list(invoices))
    delivery = FakeDelivery()
    audit = InMemoryAuditRepository()
    service = DunningService(
        repository=repository,  # type: ignore[arg-type]
        delivery=delivery,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, delivery, audit, world.user, world.acme_books)


def _by_id(report):  # type: ignore[no-untyped-def]
    return {r.invoice_id: r for r in report.results}


# --- chase everything --------------------------------------------------------------------


async def test_chase_everything_sends_a_due_reminder_to_every_overdue_invoice() -> None:
    a, b = _invoice(), _invoice()
    setup = _setup(a, b)

    report = await setup.chase(days=10)

    assert (report.sent, report.skipped, report.failed, report.deferred) == (2, 0, 0, 0)
    assert setup.delivery.sent_to == [a.invoice_id, b.invoice_id]
    assert setup.repository.recorded == [a.invoice_id, b.invoice_id]


async def test_a_formal_notice_is_never_sent_by_chase_everything() -> None:
    """It claims interest and collection cost; a bulk click must not be able to
    demand money from customers nobody looked at."""
    due_a_notice = _invoice(sent_positions=frozenset({1, 2}), last_sent_on=_today(20))
    ordinary = _invoice()
    setup = _setup(due_a_notice, ordinary)

    report = await setup.chase(days=40)

    results = _by_id(report)
    assert results[due_a_notice.invoice_id].skip is ChaseSkip.NEEDS_CONFIRMATION
    assert results[ordinary.invoice_id].status is ChaseStatus.SENT
    assert setup.delivery.sent_to == [ordinary.invoice_id]  # the notice did NOT go


async def test_invoices_that_are_not_due_a_step_are_reported_not_hidden() -> None:
    # 25 days overdue: step 2's day (21) has come, but step 1 went out yesterday.
    paused = _invoice(is_paused=True)
    too_soon = _invoice(sent_positions=frozenset({1}), last_sent_on=_today(24))
    setup = _setup(paused, too_soon)

    report = await setup.chase(days=25)

    results = _by_id(report)
    assert results[paused.invoice_id].skip is ChaseSkip.BLOCKED
    assert results[paused.invoice_id].assessment.blocker is Blocker.PAUSED  # type: ignore[union-attr]
    assert results[too_soon.invoice_id].assessment.blocker is Blocker.TOO_SOON  # type: ignore[union-attr]
    assert setup.delivery.sent_to == []
    assert report.skipped == 2


# --- explicit selection --------------------------------------------------------------------


async def test_a_formal_notice_can_be_sent_when_a_person_chose_it() -> None:
    notice = _invoice(sent_positions=frozenset({1, 2}), last_sent_on=_today(20))
    setup = _setup(notice)

    report = await setup.chase(days=40, items=[ChaseItem(notice.invoice_id, 3)])

    assert report.sent == 1
    assert setup.delivery.sent_to == [notice.invoice_id]


async def test_a_step_that_changed_since_the_preview_is_skipped_not_sent_as_something_else() -> (
    None
):
    """The person reviewed step 1; someone else has since sent it, so the next one
    is step 2. Sending step 2 to a customer who was shown step 1 is the surprise
    this guard exists to prevent."""
    moved_on = _invoice(sent_positions=frozenset({1}), last_sent_on=_today(20))
    setup = _setup(moved_on)

    report = await setup.chase(days=40, items=[ChaseItem(moved_on.invoice_id, 1)])

    (result,) = report.results
    assert result.skip is ChaseSkip.STEP_CHANGED
    assert setup.delivery.sent_to == []


async def test_a_named_invoice_that_is_not_overdue_is_skipped() -> None:
    setup = _setup()
    report = await setup.chase(items=[ChaseItem(uuid.uuid4(), 1)])

    (result,) = report.results
    assert result.skip is ChaseSkip.NOT_OVERDUE_OR_UNKNOWN
    assert result.invoice is None


async def test_an_explicit_item_that_is_blocked_is_skipped_with_its_blocker() -> None:
    paused = _invoice(is_paused=True)
    setup = _setup(paused)

    report = await setup.chase(items=[ChaseItem(paused.invoice_id, 1)])

    (result,) = report.results
    assert result.skip is ChaseSkip.BLOCKED
    assert setup.delivery.sent_to == []


async def test_only_the_named_invoices_are_chased() -> None:
    chosen, ignored = _invoice(), _invoice()
    setup = _setup(chosen, ignored)

    report = await setup.chase(items=[ChaseItem(chosen.invoice_id, 1)])

    assert setup.delivery.sent_to == [chosen.invoice_id]
    assert [r.invoice_id for r in report.results] == [chosen.invoice_id]


async def test_naming_an_invoice_twice_sends_it_once() -> None:
    one = _invoice()
    setup = _setup(one)

    report = await setup.chase(items=[ChaseItem(one.invoice_id, 1), ChaseItem(one.invoice_id, 1)])

    assert report.sent == 1
    assert setup.delivery.sent_to == [one.invoice_id]


# --- one failure does not stop the rest -------------------------------------------------------


async def test_a_customer_with_no_address_does_not_stop_the_others() -> None:
    unreachable, fine = _invoice(), _invoice()
    setup = _setup(unreachable, fine)
    setup.delivery.raises_for[unreachable.invoice_id] = UnreachableCustomer(
        DeliveryChannel.EMAIL, "no address"
    )

    report = await setup.chase()

    results = _by_id(report)
    assert results[unreachable.invoice_id].failure is ChaseFailure.UNREACHABLE
    assert results[fine.invoice_id].status is ChaseStatus.SENT
    assert (report.sent, report.failed) == (1, 1)
    # Each attempt ran in its own savepoint; only the failed one was rolled back.
    assert setup.repository.savepoints_opened == 2
    assert setup.repository.savepoints_rolled_back == 1


async def test_a_provider_refusal_is_reported_and_leaves_the_step_due() -> None:
    refused, fine = _invoice(), _invoice()
    setup = _setup(refused, fine)
    setup.delivery.status_for[refused.invoice_id] = DeliveryStatus.QUEUED

    report = await setup.chase()

    result = _by_id(report)[refused.invoice_id]
    assert result.failure is ChaseFailure.NOT_DELIVERED
    assert result.delivery is not None and result.delivery.status is DeliveryStatus.QUEUED
    assert refused.invoice_id not in setup.repository.recorded  # not a reminder sent
    assert fine.invoice_id in setup.repository.recorded
    # A refusal is RETURNED, not raised, so its savepoint commits and the failed
    # dispatch's record survives.
    assert setup.repository.savepoints_rolled_back == 0


async def test_a_race_that_records_the_step_twice_is_reported_as_already_sent() -> None:
    raced, fine = _invoice(), _invoice()
    setup = _setup(raced, fine)
    setup.repository.raises_on_record[raced.invoice_id] = ReminderAlreadySent("raced")

    report = await setup.chase()

    assert _by_id(report)[raced.invoice_id].failure is ChaseFailure.ALREADY_SENT
    assert _by_id(report)[fine.invoice_id].status is ChaseStatus.SENT


async def test_a_failure_is_audited_outside_the_rolled_back_savepoint() -> None:
    """The savepoint took the dispatch's own audit with it; without this a customer
    who may have been e-mailed (the race) would leave no trace."""
    unreachable = _invoice()
    setup = _setup(unreachable)
    setup.delivery.raises_for[unreachable.invoice_id] = UnreachableCustomer(
        DeliveryChannel.EMAIL, "no address"
    )

    await setup.chase()

    failures = [e for e in setup.audit._entries if e.outcome.value == "failure"]
    assert len(failures) == 1
    assert failures[0].detail["reason"] == "unreachable"


# --- the cap and repeating ----------------------------------------------------------------------


async def test_more_than_the_cap_are_deferred_not_dropped() -> None:
    invoices = [_invoice() for _ in range(MAX_CHASE_PER_CALL + 3)]
    setup = _setup(*invoices)

    report = await setup.chase()

    assert report.sent == MAX_CHASE_PER_CALL
    assert report.deferred == 3
    assert len(setup.delivery.sent_to) == MAX_CHASE_PER_CALL
    # The deferred ones are exactly the tail, still reported.
    assert {r.invoice_id for r in report.results if r.skip is ChaseSkip.DEFERRED} == {
        i.invoice_id for i in invoices[MAX_CHASE_PER_CALL:]
    }


async def test_repeating_a_chase_cannot_escalate_anybody() -> None:
    """The defect the spacing rule closes, at bulk scale: the same call twice must
    not send step 2 to everyone the moment step 1 has gone."""
    far_overdue = _invoice()
    setup = _setup(far_overdue)

    first = await setup.chase(days=40)
    assert first.sent == 1

    # The database would now report step 1 as sent, today. Model exactly that.
    setup.repository.invoices = [
        _invoice(
            invoice_id=far_overdue.invoice_id,
            sent_positions=frozenset({1}),
            last_sent_on=_today(40),
        )
    ]
    second = await setup.chase(days=40)

    assert second.sent == 0
    assert _by_id(second)[far_overdue.invoice_id].assessment.blocker is Blocker.TOO_SOON  # type: ignore[union-attr]
    assert setup.delivery.sent_to == [far_overdue.invoice_id]  # once, ever


# --- audit and authorization ---------------------------------------------------------------------


async def test_a_chase_is_summarised_in_the_audit_log() -> None:
    setup = _setup(_invoice(), _invoice(is_paused=True))

    await setup.chase()

    summary = setup.audit._entries[-1]
    assert summary.action == "chase_overdue"
    assert summary.detail == {
        "sent": 1,
        "skipped": 1,
        "failed": 0,
        "deferred": 0,
        "explicit_selection": False,
    }


async def test_an_explicit_selection_is_recorded_as_one() -> None:
    one = _invoice()
    setup = _setup(one)
    await setup.chase(items=[ChaseItem(one.invoice_id, 1)])
    assert setup.audit._entries[-1].detail["explicit_selection"] is True


async def test_a_role_without_send_cannot_chase() -> None:
    setup = _setup(_invoice(), role="Expense Submitter")

    with pytest.raises(NotAuthorizedToInvoice):
        await setup.chase()
    assert setup.delivery.sent_to == []


async def test_a_ladder_that_is_empty_chases_nobody() -> None:
    setup = _setup(_invoice())
    setup.repository.ladder_rows = ()

    report = await setup.chase(days=40)

    assert report.sent == 0
    assert all(r.assessment.blocker is Blocker.LADDER_COMPLETE for r in report.results)  # type: ignore[union-attr]


async def test_the_default_ladder_is_used_when_none_is_configured() -> None:
    setup = _setup(_invoice())
    report = await setup.chase(days=10)
    (result,) = report.results
    assert isinstance(result.assessment, DunningAssessment)
    assert result.assessment.step == DEFAULT_LADDER[0]
    assert result.assessment.step.kind is StepKind.FRIENDLY  # type: ignore[union-attr]

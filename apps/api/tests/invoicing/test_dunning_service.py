"""api.invoicing.dunning_service - SI-04's orchestration (ADR-071).

The rules are tested in test_dunning.py. What is under test here is the wiring:
that only an ACCEPTED dispatch becomes a reminder sent, that the right amounts and
rate kind reach the email, that a pause and a paid invoice send nothing, and who
may do what. The delivery service is a fake that records what it was asked to send.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.delivery import DeliveryChannel, DeliveryStatus, ReminderNotice
from api.invoicing.delivery_service import DeliveryRecord
from api.invoicing.dunning import (
    DEFAULT_LADDER,
    Blocker,
    DunningAssessment,
    InterestRate,
    InterestRateKind,
    LadderInvalid,
    LadderStep,
    StepKind,
)
from api.invoicing.dunning_service import (
    CustomerNotFoundForPause,
    DunningInvoice,
    DunningService,
    NothingToSend,
    ReminderNotDelivered,
)
from api.invoicing.model import InvoiceNotFound, NotAuthorizedToInvoice
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

DUE = date(2026, 1, 1)
INVOICE = uuid.uuid4()
CUSTOMER = uuid.uuid4()
RATE_10 = [InterestRate(valid_from=date(2025, 1, 1), rate=Decimal("10"))]


def _today(days_overdue: int) -> date:
    return DUE + timedelta(days=days_overdue)


# --- fakes -------------------------------------------------------------------


@dataclass
class FakeDelivery:
    """Records each dispatch and answers with a chosen status."""

    status: DeliveryStatus = DeliveryStatus.SENT
    calls: list[dict[str, object]] = field(default_factory=list)

    async def dispatch(self, **kwargs: object) -> DeliveryRecord:
        self.calls.append(kwargs)
        return DeliveryRecord(
            id=uuid.uuid4(),
            invoice_id=kwargs["invoice_id"],  # type: ignore[arg-type]
            channel=DeliveryChannel.EMAIL,
            status=self.status,
            recipient="klant@example.com",
            language=Language.NL,
            attempts=1,
        )


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    invoice: DunningInvoice | None = None
    ladder_rows: tuple[LadderStep, ...] | None = None
    rates_by_kind: dict[InterestRateKind, list[InterestRate]] = field(default_factory=dict)
    customers: set[uuid.UUID] = field(default_factory=lambda: {CUSTOMER})
    paused: set[uuid.UUID] = field(default_factory=set)
    reminders: list[DunningAssessment] = field(default_factory=list)
    rate_lookups: list[InterestRateKind] = field(default_factory=list)
    replaced: list[tuple[LadderStep, ...]] = field(default_factory=list)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def ladder(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.ladder_rows

    async def replace_ladder(self, *, administration_id, user_id, steps):  # type: ignore[no-untyped-def]
        self.ladder_rows = tuple(steps)
        self.replaced.append(tuple(steps))

    async def rates(self, *, kind: InterestRateKind):  # type: ignore[no-untyped-def]
        self.rate_lookups.append(kind)
        return self.rates_by_kind.get(kind, [])

    async def overdue(self, *, administration_id: uuid.UUID, today: date):  # type: ignore[no-untyped-def]
        return [self.invoice] if self.invoice else []

    async def facts_for(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        inv = self.invoice
        return inv if inv is not None and inv.invoice_id == invoice_id else None

    async def customer_exists(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID
    ) -> bool:
        return customer_id in self.customers

    async def pause(self, *, administration_id, customer_id, user_id, reason):  # type: ignore[no-untyped-def]
        self.paused.add(customer_id)

    async def resume(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> bool:
        was = customer_id in self.paused
        self.paused.discard(customer_id)
        return was

    async def record_reminder(  # type: ignore[no-untyped-def]
        self, *, administration_id, invoice_id, assessment, delivery_id, user_id
    ):
        self.reminders.append(assessment)


@dataclass
class Setup:
    service: DunningService
    repository: FakeRepository
    delivery: FakeDelivery
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def send(self, days: int = 10):  # type: ignore[no-untyped-def]
        return await self.service.send_next(
            administration_id=self.administration,
            invoice_id=INVOICE,
            actor_user_id=self.user,
            today=_today(days),
        )


def _invoice(**overrides: object) -> DunningInvoice:
    defaults: dict[str, object] = dict(
        invoice_id=INVOICE,
        invoice_reference="2026-7",
        invoice_date=date(2025, 12, 1),
        due_date=DUE,
        customer_id=CUSTOMER,
        customer_name="De Vries Holding B.V.",
        outstanding=Decimal("1000.00"),
        is_business=True,
        is_paused=False,
        sent_positions=frozenset(),
    )
    defaults.update(overrides)
    return DunningInvoice(**defaults)  # type: ignore[arg-type]


def _setup(*, role: str = "Accountant", **invoice_overrides: object) -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(
        organization_id=world.acme,
        invoice=_invoice(**invoice_overrides),
        rates_by_kind={
            InterestRateKind.COMMERCIAL: list(RATE_10),
            InterestRateKind.CONSUMER: [InterestRate(date(2025, 1, 1), Decimal("6"))],
        },
    )
    delivery = FakeDelivery()
    audit = InMemoryAuditRepository()
    service = DunningService(
        repository=repository,  # type: ignore[arg-type]
        delivery=delivery,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, delivery, audit, world.user, world.acme_books)


def _last_audit(setup: Setup):  # type: ignore[no-untyped-def]
    return setup.audit._entries[-1]


# --- the ladder --------------------------------------------------------------


async def test_an_unconfigured_administration_gets_the_default_ladder() -> None:
    setup = _setup()
    steps, is_default = await setup.service.ladder(
        administration_id=setup.administration, actor_user_id=setup.user
    )
    assert (steps, is_default) == (DEFAULT_LADDER, True)


async def test_a_configured_ladder_is_returned_as_configured() -> None:
    setup = _setup()
    mine = (LadderStep(1, 5, StepKind.FRIENDLY),)
    await setup.service.set_ladder(
        administration_id=setup.administration, actor_user_id=setup.user, steps=mine
    )

    steps, is_default = await setup.service.ladder(
        administration_id=setup.administration, actor_user_id=setup.user
    )
    assert (steps, is_default) == (mine, False)
    assert _last_audit(setup).action == "configure_dunning_ladder"


async def test_an_empty_ladder_is_configured_not_default() -> None:
    """'Chase nobody' is a choice, and it must not read back as 'never set up'."""
    setup = _setup()
    await setup.service.set_ladder(
        administration_id=setup.administration, actor_user_id=setup.user, steps=()
    )
    steps, is_default = await setup.service.ladder(
        administration_id=setup.administration, actor_user_id=setup.user
    )
    assert (steps, is_default) == ((), False)


async def test_an_invalid_ladder_is_refused_before_anything_is_written() -> None:
    setup = _setup()
    overreaching = (LadderStep(1, 7, StepKind.REMINDER, charge_collection_cost=True),)

    with pytest.raises(LadderInvalid):
        await setup.service.set_ladder(
            administration_id=setup.administration, actor_user_id=setup.user, steps=overreaching
        )
    assert setup.repository.replaced == []


# --- sending -----------------------------------------------------------------


async def test_the_first_reminder_is_a_friendly_one_with_no_claim() -> None:
    setup = _setup()
    sent = await setup.send(days=40)  # far overdue, still step 1 first

    (call,) = setup.delivery.calls
    notice = call["reminder"]
    assert isinstance(notice, ReminderNotice)
    assert notice.kind == "friendly"
    assert notice.outstanding == Decimal("1000.00")
    assert notice.days_overdue == 40
    assert notice.interest is None and notice.collection_cost is None and notice.pay_by is None
    assert sent.assessment.step == DEFAULT_LADDER[0]


async def test_a_sent_reminder_is_recorded_and_audited() -> None:
    setup = _setup()
    await setup.send(days=10)

    (recorded,) = setup.repository.reminders
    assert recorded.step == DEFAULT_LADDER[0]
    entry = _last_audit(setup)
    assert entry.action == "send_dunning_reminder"
    assert entry.detail["step"] == 1
    assert entry.detail["outstanding"] == "1000.00"


async def test_the_formal_notice_carries_interest_cost_and_deadline() -> None:
    setup = _setup(sent_positions=frozenset({1, 2}))
    await setup.send(days=36)

    notice = setup.delivery.calls[0]["reminder"]
    assert isinstance(notice, ReminderNotice)
    assert notice.kind == "formal_notice"
    assert notice.interest == Decimal("9.86")  # 1000 x 10% x 36 / 365
    assert notice.collection_cost == Decimal("150.00")  # 15% of 1000
    assert notice.pay_by == _today(36) + timedelta(days=15)


async def test_a_business_is_charged_the_commercial_rate_and_a_consumer_the_consumer_one() -> None:
    business = _setup(sent_positions=frozenset({1, 2}), is_business=True)
    consumer = _setup(sent_positions=frozenset({1, 2}), is_business=False)
    await business.send(days=36)
    await consumer.send(days=36)

    assert business.repository.rate_lookups == [InterestRateKind.COMMERCIAL]
    assert consumer.repository.rate_lookups == [InterestRateKind.CONSUMER]
    b = business.delivery.calls[0]["reminder"]
    c = consumer.delivery.calls[0]["reminder"]
    assert isinstance(b, ReminderNotice) and isinstance(c, ReminderNotice)
    assert b.interest == Decimal("9.86")  # at 10%
    assert c.interest == Decimal("5.92")  # 1000 x 6% x 36 / 365 = 5.9178


async def test_an_administration_that_charges_no_interest_never_reads_the_rate_table() -> None:
    setup = _setup(sent_positions=frozenset({1}))
    no_interest = (
        LadderStep(1, 7, StepKind.FRIENDLY),
        LadderStep(2, 21, StepKind.FORMAL_NOTICE, charge_collection_cost=True),
    )
    setup.repository.ladder_rows = no_interest

    await setup.send(days=25)

    assert setup.repository.rate_lookups == []


async def test_a_missing_rate_refuses_the_formal_notice_and_sends_nothing() -> None:
    setup = _setup(sent_positions=frozenset({1, 2}))
    setup.repository.rates_by_kind = {}

    with pytest.raises(NothingToSend) as caught:
        await setup.send(days=36)

    assert caught.value.assessment.blocker is Blocker.INTEREST_RATE_MISSING
    assert setup.delivery.calls == []
    assert setup.repository.reminders == []


@pytest.mark.parametrize(
    ("overrides", "days", "blocker"),
    [
        ({"is_paused": True}, 40, Blocker.PAUSED),
        ({"outstanding": Decimal("0.00")}, 40, Blocker.NOTHING_OUTSTANDING),
        ({"due_date": None}, 40, Blocker.NO_DUE_DATE),
        ({}, 0, Blocker.NOT_OVERDUE),
        ({}, 3, Blocker.STEP_NOT_DUE),
        ({"sent_positions": frozenset({1, 2, 3})}, 400, Blocker.LADDER_COMPLETE),
    ],
)
async def test_nothing_is_sent_when_the_rules_say_so(
    overrides: dict[str, object], days: int, blocker: Blocker
) -> None:
    setup = _setup(**overrides)

    with pytest.raises(NothingToSend) as caught:
        await setup.send(days=days)

    assert caught.value.assessment.blocker is blocker
    assert setup.delivery.calls == []
    assert setup.repository.reminders == []


@pytest.mark.parametrize("status", [DeliveryStatus.QUEUED, DeliveryStatus.FAILED])
async def test_only_an_accepted_dispatch_counts_as_a_reminder_sent(
    status: DeliveryStatus,
) -> None:
    """Nothing drains the delivery queue, so recording a queued reminder as sent
    would stop the step ever being retried."""
    setup = _setup()
    setup.delivery.status = status

    with pytest.raises(ReminderNotDelivered) as caught:
        await setup.send(days=10)

    assert caught.value.delivery.status is status
    assert setup.repository.reminders == []
    assert _last_audit(setup).outcome.value == "failure"


async def test_an_unknown_invoice_is_not_found() -> None:
    setup = _setup()
    with pytest.raises(InvoiceNotFound):
        await setup.service.send_next(
            administration_id=setup.administration,
            invoice_id=uuid.uuid4(),
            actor_user_id=setup.user,
            today=_today(10),
        )


# --- reading ------------------------------------------------------------------


async def test_the_overview_assesses_every_overdue_invoice() -> None:
    setup = _setup()
    rows = await setup.service.overview(
        administration_id=setup.administration, actor_user_id=setup.user, today=_today(10)
    )

    ((invoice, assessment),) = rows
    assert invoice.invoice_id == INVOICE
    assert assessment.can_send


async def test_looking_sends_nothing() -> None:
    setup = _setup()
    await setup.service.overview(
        administration_id=setup.administration, actor_user_id=setup.user, today=_today(40)
    )
    await setup.service.assessment(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        actor_user_id=setup.user,
        today=_today(40),
    )
    assert setup.delivery.calls == []


# --- pause --------------------------------------------------------------------


async def test_pausing_a_customer_stops_the_chase_and_resuming_restarts_it() -> None:
    setup = _setup()
    await setup.service.pause(
        administration_id=setup.administration,
        customer_id=CUSTOMER,
        actor_user_id=setup.user,
        reason="  in dispute  ",
    )
    assert CUSTOMER in setup.repository.paused
    assert _last_audit(setup).action == "pause_dunning"

    await setup.service.resume(
        administration_id=setup.administration, customer_id=CUSTOMER, actor_user_id=setup.user
    )
    assert CUSTOMER not in setup.repository.paused
    assert _last_audit(setup).action == "resume_dunning"


async def test_pausing_an_unknown_customer_is_refused() -> None:
    setup = _setup()
    with pytest.raises(CustomerNotFoundForPause):
        await setup.service.pause(
            administration_id=setup.administration,
            customer_id=uuid.uuid4(),
            actor_user_id=setup.user,
        )


async def test_the_audit_records_that_a_reason_was_given_but_not_what_it_was() -> None:
    """A reason is free text about a customer dispute; the audit log has its own
    retention (IAM-093) and is not where that accumulates."""
    setup = _setup()
    await setup.service.pause(
        administration_id=setup.administration,
        customer_id=CUSTOMER,
        actor_user_id=setup.user,
        reason="Klant betwist de levering",
    )
    detail = _last_audit(setup).detail
    assert detail == {"reason_given": True}


# --- who may ---------------------------------------------------------------------


async def test_a_role_without_send_cannot_send_pause_or_configure() -> None:
    setup = _setup(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToInvoice):
        await setup.send()
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.pause(
            administration_id=setup.administration,
            customer_id=CUSTOMER,
            actor_user_id=setup.user,
        )
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.set_ladder(
            administration_id=setup.administration, actor_user_id=setup.user, steps=()
        )
    assert setup.delivery.calls == []
    assert setup.repository.paused == set()

"""SI-07's orchestration - api.invoicing.recurring_service (ADR-074).

The dates and prices are tested in test_recurrence.py. What is under test here is
what a run DOES: that each date is generated once, dated in its own month, priced
after indexation, and never sent; that a failed issue keeps the draft; that a failure
stops only its own schedule, in order; and that a definition is refused when saved.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.customers.model import CustomerIsArchived, CustomerNotFound
from api.i18n.language import Language
from api.invoicing.model import NotAuthorizedToInvoice, NotStatutoryCompliant
from api.invoicing.posting import NoOpenPeriod, PostingConfigurationMissing
from api.invoicing.recurrence import DefinitionInvalid, RecurringLine
from api.invoicing.recurring_service import (
    MAX_RUNS_PER_CALL_TOTAL,
    RecurringDefinition,
    RecurringInvoice,
    RecurringInvoiceService,
    RecurringNotFound,
    RunAlreadyGenerated,
    RunStatus,
    ScheduleLocked,
    ScheduleStatus,
)
from api.invoicing.routes import _recurring_json
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

D = Decimal
CUSTOMER = uuid.uuid4()
YEAR = uuid.uuid4()


def definition(**overrides: object) -> RecurringDefinition:
    defaults: dict[str, object] = dict(
        customer_id=CUSTOMER,
        name="Onderhoudscontract",
        interval_months=1,
        start_date=date(2026, 1, 15),
        end_date=None,
        max_runs=None,
        due_days=None,
        indexation_percent=D("0"),
        auto_issue=False,
        notes=None,
        lines=(
            RecurringLine(
                description="Onderhoud",
                quantity=D("1"),
                unit_price=D("100.0000"),
                vat_treatment="btw_21",
            ),
        ),
    )
    defaults.update(overrides)
    return RecurringDefinition(**defaults)  # type: ignore[arg-type]


# --- fakes ---------------------------------------------------------------------------


@dataclass
class FakeInvoicing:
    """Stands in for `InvoicingService`: records what it was asked to create and
    issue, and fails for chosen dates."""

    drafts: list[dict[str, object]] = field(default_factory=list)
    issued: list[uuid.UUID] = field(default_factory=list)
    create_raises: dict[date, Exception] = field(default_factory=dict)
    issue_raises: Exception | None = None

    async def create_draft(self, **kwargs: object) -> SimpleNamespace:
        on = kwargs["invoice_date"]
        assert isinstance(on, date)
        if on in self.create_raises:
            raise self.create_raises[on]
        self.drafts.append(kwargs)
        return SimpleNamespace(invoice=SimpleNamespace(id=uuid.uuid4()))

    async def issue(self, **kwargs: object) -> None:
        if self.issue_raises is not None:
            raise self.issue_raises
        invoice_id = kwargs["invoice_id"]
        assert isinstance(invoice_id, uuid.UUID)
        self.issued.append(invoice_id)


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    schedules: dict[uuid.UUID, RecurringInvoice] = field(default_factory=dict)
    runs: dict[tuple[uuid.UUID, date], tuple[uuid.UUID, bool, str | None]] = field(
        default_factory=dict
    )
    customers: set[uuid.UUID] = field(default_factory=lambda: {CUSTOMER})
    fiscal_years: bool = True
    race_on: set[date] = field(default_factory=set)
    errors: dict[uuid.UUID, str | None] = field(default_factory=dict)
    rolled_back: int = 0

    async def organization_of(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.organization_id

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        try:
            yield
        except Exception:
            self.rolled_back += 1
            raise

    async def create(self, *, administration_id, user_id, definition, next_run_on):  # type: ignore[no-untyped-def]
        schedule = RecurringInvoice(
            id=uuid.uuid4(),
            administration_id=administration_id,
            definition=definition,
            status=ScheduleStatus.ACTIVE if next_run_on else ScheduleStatus.ENDED,
            runs_generated=0,
            next_run_on=next_run_on,
        )
        self.schedules[schedule.id] = schedule
        return schedule

    async def replace(self, *, administration_id, recurring_id, definition, status, next_run_on):  # type: ignore[no-untyped-def]
        updated = replace(
            self.schedules[recurring_id],
            definition=definition,
            status=status,
            next_run_on=next_run_on,
        )
        self.schedules[recurring_id] = updated
        return updated

    async def customer_exists(self, *, administration_id, customer_id) -> bool:  # type: ignore[no-untyped-def]
        return customer_id in self.customers

    async def get(self, *, administration_id, recurring_id):  # type: ignore[no-untyped-def]
        return self.schedules.get(recurring_id)

    async def list(self, *, administration_id):  # type: ignore[no-untyped-def]
        return list(self.schedules.values())

    async def due(self, *, administration_id, today):  # type: ignore[no-untyped-def]
        return sorted(
            (
                s
                for s in self.schedules.values()
                if s.status is ScheduleStatus.ACTIVE and s.next_run_on and s.next_run_on <= today
            ),
            key=lambda s: s.next_run_on,  # type: ignore[arg-type,return-value]
        )

    async def set_status(self, *, administration_id, recurring_id, status, next_run_on):  # type: ignore[no-untyped-def]
        self.schedules[recurring_id] = replace(
            self.schedules[recurring_id], status=status, next_run_on=next_run_on
        )

    async def fiscal_year_for(self, *, administration_id, on):  # type: ignore[no-untyped-def]
        return YEAR if self.fiscal_years else None

    async def record_run(  # type: ignore[no-untyped-def]
        self, *, administration_id, recurring_id, run_date, invoice_id, issued, issue_error
    ):
        if run_date in self.race_on or (recurring_id, run_date) in self.runs:
            raise RunAlreadyGenerated(str(run_date))
        self.runs[(recurring_id, run_date)] = (invoice_id, issued, issue_error)

    async def advance(self, *, administration_id, recurring_id, runs_generated, next_run_on):  # type: ignore[no-untyped-def]
        current = self.schedules[recurring_id]
        if runs_generated <= current.runs_generated:
            return  # only ever forward, as the SQL guarantees
        self.schedules[recurring_id] = replace(
            current,
            runs_generated=runs_generated,
            next_run_on=next_run_on,
            status=ScheduleStatus.ENDED if next_run_on is None else current.status,
        )

    async def set_error(self, *, administration_id, recurring_id, error):  # type: ignore[no-untyped-def]
        self.errors[recurring_id] = error


@dataclass
class Setup:
    service: RecurringInvoiceService
    repository: FakeRepository
    invoicing: FakeInvoicing
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def create(self, **overrides: object) -> RecurringInvoice:
        return await self.service.create(
            administration_id=self.administration,
            actor_user_id=self.user,
            definition=definition(**overrides),
        )

    async def run(self, today: date):  # type: ignore[no-untyped-def]
        return await self.service.run_due(
            administration_id=self.administration, actor_user_id=self.user, today=today
        )


def _setup(role: str = "Accountant") -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(organization_id=world.acme)
    invoicing = FakeInvoicing()
    audit = InMemoryAuditRepository()
    service = RecurringInvoiceService(
        repository=repository,  # type: ignore[arg-type]
        invoicing=invoicing,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, invoicing, audit, world.user, world.acme_books)


def _last_audit(setup: Setup):  # type: ignore[no-untyped-def]
    return setup.audit._entries[-1]


# --- defining -----------------------------------------------------------------------------


async def test_a_definition_is_saved_with_its_first_run_date() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 3, 31))

    assert schedule.status is ScheduleStatus.ACTIVE
    assert schedule.next_run_on == date(2026, 3, 31)
    assert schedule.runs_generated == 0
    assert _last_audit(setup).action == "create_recurring_invoice"


async def test_an_unrunnable_definition_is_refused_when_saved() -> None:
    setup = _setup()
    with pytest.raises(DefinitionInvalid):
        await setup.create(interval_months=5)
    with pytest.raises(DefinitionInvalid):
        await setup.create(lines=())
    assert setup.repository.schedules == {}


async def test_a_stranger_as_customer_is_a_clean_not_found() -> None:
    setup = _setup()
    with pytest.raises(CustomerNotFound):
        await setup.create(customer_id=uuid.uuid4())
    assert setup.repository.schedules == {}


async def test_a_role_without_create_sales_invoice_cannot_define_or_run() -> None:
    setup = _setup("Expense Submitter")
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.create()
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.run(date(2026, 1, 1))


# --- running: what a run does ----------------------------------------------------------------


async def test_a_due_run_creates_a_draft_dated_on_its_scheduled_date() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))

    outcomes = await setup.run(date(2026, 1, 20))

    (outcome,) = outcomes
    assert outcome.status is RunStatus.GENERATED
    assert outcome.run_date == date(2026, 1, 15)
    (draft,) = setup.invoicing.drafts
    assert draft["invoice_date"] == date(2026, 1, 15)
    assert draft["customer_id"] == CUSTOMER
    assert draft["fiscal_year_id"] == YEAR
    updated = setup.repository.schedules[schedule.id]
    assert (updated.runs_generated, updated.next_run_on) == (1, date(2026, 2, 15))


async def test_nothing_is_due_before_the_start_date() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 6, 1))
    assert await setup.run(date(2026, 5, 31)) == []
    assert setup.invoicing.drafts == []


async def test_catching_up_dates_each_invoice_in_its_own_month() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 1, 15))

    outcomes = await setup.run(date(2026, 3, 20))

    assert [o.run_date for o in outcomes] == [
        date(2026, 1, 15),
        date(2026, 2, 15),
        date(2026, 3, 15),
    ]
    assert [d["invoice_date"] for d in setup.invoicing.drafts] == [o.run_date for o in outcomes]


async def test_running_twice_generates_each_date_once() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 1, 15))

    first = await setup.run(date(2026, 2, 20))
    second = await setup.run(date(2026, 2, 20))

    assert len(first) == 2
    assert second == []
    assert len(setup.invoicing.drafts) == 2  # not four


async def test_the_next_run_is_derived_from_the_anchor_not_chained() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 31))

    await setup.run(date(2026, 2, 28))  # 31 Jan and 28 Feb

    assert setup.repository.schedules[schedule.id].next_run_on == date(2026, 3, 31)


async def test_prices_are_indexed_on_each_anniversary() -> None:
    setup = _setup()
    await setup.create(
        start_date=date(2026, 1, 15), indexation_percent=D("3.5"), interval_months=12
    )

    await setup.run(date(2028, 1, 20))  # 15 Jan 2026, 2027, 2028

    prices = [d["lines"][0].unit_price for d in setup.invoicing.drafts]  # type: ignore[index]
    assert prices == [D("100.0000"), D("103.5000"), D("107.1225")]


async def test_the_customers_own_terms_apply_unless_the_schedule_sets_its_own() -> None:
    own = _setup()
    await own.create(start_date=date(2026, 1, 15), due_days=14)
    await own.run(date(2026, 1, 15))
    assert own.invoicing.drafts[0]["due_date"] == date(2026, 1, 29)

    customers = _setup()
    await customers.create(start_date=date(2026, 1, 15), due_days=None)
    await customers.run(date(2026, 1, 15))
    assert customers.invoicing.drafts[0]["due_date"] is None  # the customer master decides


def test_the_service_has_no_way_to_send_anything() -> None:
    """SENDING is never automatic. The service is built from a repository, the
    invoicing service, authorization and audit - no delivery service - so a run can
    create and issue an invoice but has nothing to e-mail one with."""
    import inspect

    parameters = set(inspect.signature(RecurringInvoiceService.__init__).parameters)
    assert parameters == {"self", "repository", "invoicing", "authorization", "audit_log"}


async def test_a_schedule_ends_at_its_end_date() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15), end_date=date(2026, 2, 20))

    outcomes = await setup.run(date(2026, 12, 31))

    assert [o.run_date for o in outcomes] == [date(2026, 1, 15), date(2026, 2, 15)]
    ended = setup.repository.schedules[schedule.id]
    assert ended.status is ScheduleStatus.ENDED and ended.next_run_on is None
    assert await setup.run(date(2027, 12, 31)) == []


async def test_a_schedule_ends_after_max_runs() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15), max_runs=2)
    await setup.run(date(2026, 12, 31))
    assert setup.repository.schedules[schedule.id].status is ScheduleStatus.ENDED
    assert len(setup.invoicing.drafts) == 2


# --- issuing ---------------------------------------------------------------------------------


async def test_without_auto_issue_the_invoice_stays_a_draft() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 1, 15), auto_issue=False)
    (outcome,) = await setup.run(date(2026, 1, 20))
    assert not outcome.issued and outcome.issue_error is None
    assert setup.invoicing.issued == []


async def test_auto_issue_issues_the_invoice() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 1, 15), auto_issue=True)
    (outcome,) = await setup.run(date(2026, 1, 20))
    assert outcome.issued
    assert setup.invoicing.issued == [outcome.invoice_id]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotAuthorizedToInvoice("send", "sales_invoice", "no"), "not_authorized_to_issue"),
        (NotStatutoryCompliant(()), "not_statutory_compliant"),
        (NoOpenPeriod("closed"), "no_open_period"),
        (PostingConfigurationMissing("revenue", "unmapped"), "posting_unconfigured"),
    ],
)
async def test_a_refused_issue_keeps_the_draft_and_says_why(error: Exception, code: str) -> None:
    """The point of the nested savepoint: the invoice is not lost and the number is
    not burned - it is a draft with a reason, for a person to finish."""
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15), auto_issue=True)
    setup.invoicing.issue_raises = error

    (outcome,) = await setup.run(date(2026, 1, 20))

    assert outcome.status is RunStatus.GENERATED  # the run itself succeeded
    assert not outcome.issued and outcome.issue_error == code
    assert outcome.invoice_id is not None  # the draft exists
    assert setup.repository.runs[(schedule.id, date(2026, 1, 15))] == (
        outcome.invoice_id,
        False,
        code,
    )
    assert setup.repository.schedules[schedule.id].runs_generated == 1  # and it advanced


# --- failures --------------------------------------------------------------------------------


async def test_a_failed_run_stops_its_schedule_so_dates_are_never_out_of_order() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    setup.invoicing.create_raises[date(2026, 1, 15)] = CustomerIsArchived("archived")

    outcomes = await setup.run(date(2026, 3, 20))

    (outcome,) = outcomes  # February and March were NOT attempted
    assert outcome.status is RunStatus.FAILED and outcome.error == "customer_archived"
    assert setup.invoicing.drafts == []
    assert setup.repository.schedules[schedule.id].runs_generated == 0
    assert setup.repository.errors[schedule.id] == "customer_archived"


async def test_a_failing_schedule_does_not_stop_the_others() -> None:
    setup = _setup()
    bad = await setup.create(name="Bad", start_date=date(2026, 1, 15))
    good = await setup.create(name="Good", start_date=date(2026, 1, 20))
    setup.invoicing.create_raises[date(2026, 1, 15)] = CustomerIsArchived("archived")

    outcomes = await setup.run(date(2026, 1, 25))

    by_name = {o.schedule_name: o for o in outcomes}
    assert by_name["Bad"].status is RunStatus.FAILED
    assert by_name["Good"].status is RunStatus.GENERATED
    assert setup.repository.schedules[good.id].runs_generated == 1
    assert setup.repository.schedules[bad.id].runs_generated == 0


async def test_a_failed_run_is_retried_on_the_next_call() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    setup.invoicing.create_raises[date(2026, 1, 15)] = CustomerNotFound("gone")
    assert (await setup.run(date(2026, 1, 20)))[0].error == "customer_not_found"

    del setup.invoicing.create_raises[date(2026, 1, 15)]  # the customer is restored
    (retry,) = await setup.run(date(2026, 1, 20))

    assert retry.status is RunStatus.GENERATED and retry.run_date == date(2026, 1, 15)
    assert setup.repository.errors[schedule.id] is None  # cleared by success


async def test_a_date_in_no_fiscal_year_fails_with_a_code() -> None:
    setup = _setup()
    await setup.create(start_date=date(2026, 1, 15))
    setup.repository.fiscal_years = False

    (outcome,) = await setup.run(date(2026, 1, 20))

    assert outcome.status is RunStatus.FAILED and outcome.error == "no_fiscal_year"
    assert setup.invoicing.drafts == []


async def test_losing_the_race_for_a_date_bills_nobody_twice() -> None:
    """`unique (schedule, run_date)` caught a second generation: the savepoint rolled
    back OUR draft, and the counter is brought forward so the date is not offered
    again."""
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    setup.repository.race_on.add(date(2026, 1, 15))

    (outcome,) = await setup.run(date(2026, 1, 20))

    assert outcome.status is RunStatus.ALREADY_GENERATED
    assert setup.repository.rolled_back == 1
    assert setup.repository.schedules[schedule.id].runs_generated == 1


async def test_one_call_generates_at_most_the_total_cap() -> None:
    setup = _setup()
    for i in range(MAX_RUNS_PER_CALL_TOTAL // 10 + 3):
        await setup.create(name=f"s{i}", start_date=date(2025, 1, 1), interval_months=1)

    outcomes = await setup.run(date(2026, 1, 1))

    assert len(outcomes) == MAX_RUNS_PER_CALL_TOTAL
    # The rest wait for the next call; running again picks them up.
    assert len(await setup.run(date(2026, 1, 1))) > 0


# --- editing, pausing -------------------------------------------------------------------------


async def test_lines_and_price_can_change_after_runs() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    await setup.run(date(2026, 1, 20))

    new_lines = (
        RecurringLine(
            description="Onderhoud plus",
            quantity=D("1"),
            unit_price=D("120"),
            vat_treatment="btw_21",
        ),
    )
    updated = await setup.service.update(
        administration_id=setup.administration,
        recurring_id=schedule.id,
        actor_user_id=setup.user,
        definition=definition(start_date=date(2026, 1, 15), lines=new_lines),
    )

    assert updated.definition.lines == new_lines
    assert updated.runs_generated == 1  # history untouched
    assert updated.next_run_on == date(2026, 2, 15)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"start_date": date(2026, 1, 20)}, "start_date"),
        ({"interval_months": 3}, "interval_months"),
    ],
)
async def test_the_rhythm_is_locked_once_the_schedule_has_billed(
    change: dict[str, object], field: str
) -> None:
    """Dates derive from the start date and interval, so moving either would re-date
    the whole history."""
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    await setup.run(date(2026, 1, 20))

    with pytest.raises(ScheduleLocked) as caught:
        await setup.service.update(
            administration_id=setup.administration,
            recurring_id=schedule.id,
            actor_user_id=setup.user,
            definition=definition(**{"start_date": date(2026, 1, 15), **change}),
        )
    assert caught.value.field == field


async def test_the_rhythm_is_free_to_change_before_any_run() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    updated = await setup.service.update(
        administration_id=setup.administration,
        recurring_id=schedule.id,
        actor_user_id=setup.user,
        definition=definition(start_date=date(2026, 6, 1), interval_months=3),
    )
    assert updated.next_run_on == date(2026, 6, 1)


async def test_an_end_date_already_passed_ends_the_schedule_on_edit() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    await setup.run(date(2026, 3, 20))  # three runs done

    updated = await setup.service.update(
        administration_id=setup.administration,
        recurring_id=schedule.id,
        actor_user_id=setup.user,
        definition=definition(start_date=date(2026, 1, 15), end_date=date(2026, 2, 1)),
    )
    assert updated.status is ScheduleStatus.ENDED and updated.next_run_on is None


async def test_extending_an_ended_schedule_does_not_silently_revive_it() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15), max_runs=1)
    await setup.run(date(2026, 1, 20))
    assert setup.repository.schedules[schedule.id].status is ScheduleStatus.ENDED

    updated = await setup.service.update(
        administration_id=setup.administration,
        recurring_id=schedule.id,
        actor_user_id=setup.user,
        definition=definition(start_date=date(2026, 1, 15), max_runs=5),
    )

    assert updated.status is ScheduleStatus.PAUSED  # resume is a deliberate act


async def test_an_unknown_schedule_is_not_found() -> None:
    setup = _setup()
    with pytest.raises(RecurringNotFound):
        await setup.service.get(
            administration_id=setup.administration,
            recurring_id=uuid.uuid4(),
            actor_user_id=setup.user,
        )


async def test_a_paused_schedule_generates_nothing_and_resume_catches_up() -> None:
    """A pause postpones billing; it does not waive it. The runs that fell due while
    paused are generated on resume, each dated in its own month."""
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))

    await setup.service.pause(
        administration_id=setup.administration, recurring_id=schedule.id, actor_user_id=setup.user
    )
    assert await setup.run(date(2026, 3, 20)) == []

    await setup.service.resume(
        administration_id=setup.administration, recurring_id=schedule.id, actor_user_id=setup.user
    )
    outcomes = await setup.run(date(2026, 3, 20))

    assert [o.run_date for o in outcomes] == [
        date(2026, 1, 15),
        date(2026, 2, 15),
        date(2026, 3, 15),
    ]


async def test_pausing_twice_is_harmless() -> None:
    setup = _setup()
    schedule = await setup.create()
    for _ in range(2):
        paused = await setup.service.pause(
            administration_id=setup.administration,
            recurring_id=schedule.id,
            actor_user_id=setup.user,
        )
    assert paused.status is ScheduleStatus.PAUSED


# --- the wire shape ---------------------------------------------------------------------------


async def test_the_json_shows_the_agreed_price_and_what_the_next_invoice_will_charge() -> None:
    """So a screen can show a coming increase BEFORE the customer is billed it."""
    setup = _setup()
    schedule = await setup.create(
        start_date=date(2026, 1, 15), interval_months=12, indexation_percent=D("3.5")
    )
    await setup.run(date(2026, 1, 20))  # the first year's invoice
    current = setup.repository.schedules[schedule.id]

    body = _recurring_json(current, Language.EN)

    assert body["next_run_on"] == "2027-01-15"
    (line,) = body["lines"]  # type: ignore[misc]
    assert line["unit_price"] == "100.0000"  # what is agreed
    assert line["next_unit_price"] == "103.5000"  # what 15 Jan 2027 will charge


async def test_a_failed_schedule_explains_itself_in_the_readers_language() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15))
    schedule = replace(schedule, last_error="customer_archived")

    dutch = _recurring_json(schedule, Language.NL)
    english = _recurring_json(schedule, Language.EN)

    assert dutch["last_error"] == "customer_archived"
    assert "gearchiveerd" in str(dutch["last_error_message"])
    assert "archived" in str(english["last_error_message"])


async def test_an_ended_schedule_has_no_next_price() -> None:
    setup = _setup()
    schedule = await setup.create(start_date=date(2026, 1, 15), max_runs=1)
    await setup.run(date(2026, 1, 20))

    body = _recurring_json(setup.repository.schedules[schedule.id], Language.EN)

    assert body["status"] == "ended" and body["next_run_on"] is None
    assert body["lines"][0]["next_unit_price"] is None  # type: ignore[index]

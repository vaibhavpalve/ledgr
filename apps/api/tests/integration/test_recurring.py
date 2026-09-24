"""Migration 0056 and `SqlRecurringRepository` against a real Postgres - ADR-074.

What only the database can establish: the CHECKs a schedule cannot dodge, that a
schedule bills only a customer of its own administration, that a date is generated
once (the idempotency key), that a run is immutable, and that another tenant sees
none of it. Then every repository method as `ledgr_app` under RLS.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0056.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.invoicing.recurrence import RecurringLine
from api.invoicing.recurring_repository import SqlRecurringRepository
from api.invoicing.recurring_service import (
    RecurringDefinition,
    RunAlreadyGenerated,
    ScheduleStatus,
)
from tests.integration.test_sales_invoice_posting import _draft, _exec, _scalar, _world
from tests.support.seed import SeededTenants


async def _as_repo[T](org: uuid.UUID, work: Callable[[SqlRecurringRepository], Awaitable[T]]) -> T:
    """Run `work` against the repository in ONE transaction under `org`'s RLS."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        return await work(SqlRecurringRepository(AsyncSession(bind=conn)))


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


def _definition(customer: uuid.UUID, **overrides: object) -> RecurringDefinition:
    defaults: dict[str, object] = dict(
        customer_id=customer,
        name="Onderhoudscontract",
        interval_months=1,
        start_date=date(2026, 7, 31),
        end_date=None,
        max_runs=None,
        due_days=14,
        indexation_percent=Decimal("3.500"),
        auto_issue=False,
        notes="Per maand",
        lines=(
            RecurringLine(
                description="Onderhoud",
                quantity=Decimal("2.0000"),
                unit_price=Decimal("49.9500"),
                vat_treatment="btw_21",
                discount_percent=Decimal("10.0000"),
            ),
            RecurringLine(
                description="Voorrijkosten",
                quantity=Decimal("1.0000"),
                unit_price=Decimal("0.0350"),
                vat_treatment="btw_9",
            ),
        ),
    )
    defaults.update(overrides)
    return RecurringDefinition(**defaults)  # type: ignore[arg-type]


async def _insert(tenants: SeededTenants, customer: uuid.UUID, **columns: object) -> uuid.UUID:
    values: dict[str, object] = {
        "interval_months": 1,
        "start_date": date(2026, 7, 31),
        "status": "active",
        "next_run_on": date(2026, 7, 31),
    }
    values.update(columns)
    return await _scalar(
        tenants,
        "INSERT INTO recurring_invoice (organization_id, administration_id, customer_id, name, "
        "  interval_months, start_date, end_date, status, next_run_on) "
        "VALUES (:org, :admin, :customer, 'x', :interval_months, :start_date, "
        "  CAST(:end_date AS date), :status, CAST(:next_run_on AS date)) RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        customer=str(customer),
        end_date=values.get("end_date"),
        **{k: v for k, v in values.items() if k != "end_date"},
    )


# --- the constraints ---------------------------------------------------------------------


async def test_an_interval_outside_the_billing_rhythms_is_refused(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    with pytest.raises(Exception, match="(?i)interval_months|check"):
        await _insert(two_organizations, customer, interval_months=5)


async def test_an_end_date_before_the_start_is_refused(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    with pytest.raises(Exception, match="(?i)end_after_start|check"):
        await _insert(two_organizations, customer, end_date=date(2026, 1, 1))


async def test_only_an_ended_schedule_may_have_no_next_run(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    with pytest.raises(Exception, match="(?i)next_run|check"):
        await _insert(two_organizations, customer, status="active", next_run_on=None)
    with pytest.raises(Exception, match="(?i)next_run|check"):
        await _insert(two_organizations, customer, status="ended")  # has a next date
    await _insert(two_organizations, customer, status="ended", next_run_on=None)


async def test_a_schedule_bills_only_a_customer_of_its_own_administration(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        strangers = (
            await conn.execute(
                text(
                    "INSERT INTO customer (organization_id, administration_id, name, "
                    "  delivery_channel, invoice_email) "
                    "VALUES (:org, :admin, 'Elders B.V.', 'email', 'e@example.com') RETURNING id"
                ),
                {"org": str(two_organizations.org_b), "admin": str(two_organizations.admin_b)},
            )
        ).scalar_one()

    with pytest.raises(Exception, match="(?i)own administration|CLAUDE.md rule 1|violates"):
        await _insert(two_organizations, strangers)


# --- runs: the idempotency key and immutability ------------------------------------------------


async def _run(tenants: SeededTenants, schedule: uuid.UUID, invoice: uuid.UUID, on: date) -> None:
    await _exec(
        tenants,
        "INSERT INTO recurring_invoice_run (organization_id, administration_id, "
        "  recurring_invoice_id, run_date, invoice_id) "
        "VALUES (:org, :admin, :schedule, :on, :invoice)",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        schedule=str(schedule),
        on=on,
        invoice=str(invoice),
    )


async def test_a_date_can_only_be_generated_once_per_schedule(
    two_organizations: SeededTenants,
) -> None:
    """The idempotency key: a retry or a race reaches this constraint, and the
    second loses rather than billing the customer twice (NFR-032)."""
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    schedule = await _insert(two_organizations, customer)
    first = await _draft(two_organizations, world)
    second = await _draft(two_organizations, world)

    await _run(two_organizations, schedule, first, date(2026, 7, 31))

    with pytest.raises(Exception, match="(?i)once_per_date|unique"):
        await _run(two_organizations, schedule, second, date(2026, 7, 31))
    await _run(two_organizations, schedule, second, date(2026, 8, 31))  # another date is fine


async def test_one_invoice_cannot_be_two_runs(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    schedule = await _insert(two_organizations, customer)
    invoice = await _draft(two_organizations, world)

    await _run(two_organizations, schedule, invoice, date(2026, 7, 31))
    with pytest.raises(Exception, match="(?i)run_invoice_idx|unique"):
        await _run(two_organizations, schedule, invoice, date(2026, 8, 31))


async def test_an_issue_error_means_the_invoice_is_not_issued(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    schedule = await _insert(two_organizations, customer)
    invoice = await _draft(two_organizations, world)

    with pytest.raises(Exception, match="(?i)error_means_draft|check"):
        await _exec(
            two_organizations,
            "INSERT INTO recurring_invoice_run (organization_id, administration_id, "
            "  recurring_invoice_id, run_date, invoice_id, issued, issue_error) "
            "VALUES (:org, :admin, :schedule, DATE '2026-07-31', :invoice, true, 'no_open_period')",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            schedule=str(schedule),
            invoice=str(invoice),
        )


async def test_a_run_is_immutable_and_undeletable(two_organizations: SeededTenants) -> None:
    """The record of what was generated."""
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    schedule = await _insert(two_organizations, customer)
    invoice = await _draft(two_organizations, world)
    await _run(two_organizations, schedule, invoice, date(2026, 7, 31))

    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(two_organizations, "UPDATE recurring_invoice_run SET issued = true")
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(two_organizations, "DELETE FROM recurring_invoice_run")


async def test_a_schedule_is_ended_never_deleted(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    await _insert(two_organizations, customer)
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(two_organizations, "DELETE FROM recurring_invoice")


async def test_another_tenant_sees_none_of_it(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    schedule = await _insert(two_organizations, customer)
    await _run(
        two_organizations, schedule, await _draft(two_organizations, world), date(2026, 7, 31)
    )

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        counts = {
            table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            for table in ("recurring_invoice", "recurring_invoice_line", "recurring_invoice_run")
        }
    assert counts == {
        "recurring_invoice": 0,
        "recurring_invoice_line": 0,
        "recurring_invoice_run": 0,
    }


# --- the repository ----------------------------------------------------------------------------


async def test_a_schedule_round_trips_with_its_lines_and_exact_decimals(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    definition = _definition(customer)

    created = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=definition,
            next_run_on=date(2026, 7, 31),
        ),
    )
    loaded = await _as_repo(org, lambda r: r.get(administration_id=admin, recurring_id=created.id))

    assert loaded is not None
    assert loaded.definition == definition  # every field, both lines, exact Decimals
    assert (loaded.status, loaded.runs_generated, loaded.next_run_on) == (
        ScheduleStatus.ACTIVE,
        0,
        date(2026, 7, 31),
    )
    # A unit price of EUR 0.0350 survived: four decimals, never rounded to the cent.
    assert loaded.definition.lines[1].unit_price == Decimal("0.0350")


async def test_list_returns_every_schedule_and_get_of_a_stranger_is_none(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    for name in ("a", "b"):
        await _as_repo(
            org,
            lambda r, name=name: r.create(
                administration_id=admin,
                user_id=user,
                definition=_definition(customer, name=name),
                next_run_on=date(2026, 7, 31),
            ),
        )

    listed = await _as_repo(org, lambda r: r.list(administration_id=admin))
    other_tenant = await _as_repo(
        two_organizations.org_b, lambda r: r.list(administration_id=admin)
    )
    unknown = await _as_repo(
        org, lambda r: r.get(administration_id=admin, recurring_id=uuid.uuid4())
    )

    assert {s.definition.name for s in listed} == {"a", "b"}
    assert other_tenant == []
    assert unknown is None


async def test_replace_swaps_the_definition_and_all_of_its_lines(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    created = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=_definition(customer),
            next_run_on=date(2026, 7, 31),
        ),
    )
    replacement = _definition(
        customer,
        name="Nieuw",
        lines=(
            RecurringLine(
                description="Alleen dit",
                quantity=Decimal("1"),
                unit_price=Decimal("10"),
                vat_treatment="btw_21",
            ),
        ),
    )

    replaced = await _as_repo(
        org,
        lambda r: r.replace(
            administration_id=admin,
            recurring_id=created.id,
            definition=replacement,
            status=ScheduleStatus.PAUSED,
            next_run_on=date(2026, 7, 31),
        ),
    )
    loaded = await _as_repo(org, lambda r: r.get(administration_id=admin, recurring_id=created.id))

    assert replaced.definition.name == "Nieuw"
    assert loaded is not None
    assert [line.description for line in loaded.definition.lines] == [
        "Alleen dit"
    ]  # one, not three
    assert loaded.status is ScheduleStatus.PAUSED


async def test_due_returns_only_active_schedules_that_have_come_due(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    made = {}
    for name, next_run in (("due", date(2026, 7, 31)), ("later", date(2026, 12, 1))):
        made[name] = await _as_repo(
            org,
            lambda r, name=name, next_run=next_run: r.create(
                administration_id=admin,
                user_id=user,
                definition=_definition(customer, name=name),
                next_run_on=next_run,
            ),
        )
    paused = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=_definition(customer, name="paused"),
            next_run_on=date(2026, 7, 31),
        ),
    )
    await _as_repo(
        org,
        lambda r: r.set_status(
            administration_id=admin,
            recurring_id=paused.id,
            status=ScheduleStatus.PAUSED,
            next_run_on=date(2026, 7, 31),
        ),
    )

    due = await _as_repo(org, lambda r: r.due(administration_id=admin, today=date(2026, 9, 1)))

    assert [s.definition.name for s in due] == ["due"]


async def test_advance_only_ever_goes_forward_and_ends_the_schedule(
    two_organizations: SeededTenants,
) -> None:
    """A stale writer must not move the counter back and re-offer a date that has
    been billed."""
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    created = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=_definition(customer),
            next_run_on=date(2026, 7, 31),
        ),
    )

    async def state() -> tuple[int, date | None, ScheduleStatus]:
        s = await _as_repo(org, lambda r: r.get(administration_id=admin, recurring_id=created.id))
        assert s is not None
        return s.runs_generated, s.next_run_on, s.status

    await _as_repo(
        org,
        lambda r: r.advance(
            administration_id=admin,
            recurring_id=created.id,
            runs_generated=2,
            next_run_on=date(2026, 9, 30),
        ),
    )
    assert await state() == (2, date(2026, 9, 30), ScheduleStatus.ACTIVE)

    await _as_repo(  # a stale writer, one run behind
        org,
        lambda r: r.advance(
            administration_id=admin,
            recurring_id=created.id,
            runs_generated=1,
            next_run_on=date(2026, 8, 31),
        ),
    )
    assert await state() == (2, date(2026, 9, 30), ScheduleStatus.ACTIVE)  # unchanged

    await _as_repo(  # the last run
        org,
        lambda r: r.advance(
            administration_id=admin,
            recurring_id=created.id,
            runs_generated=3,
            next_run_on=None,
        ),
    )
    assert await state() == (3, None, ScheduleStatus.ENDED)


async def test_the_error_is_remembered_and_cleared(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    created = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=_definition(customer),
            next_run_on=date(2026, 7, 31),
        ),
    )
    for error in ("customer_archived", None):
        await _as_repo(
            org,
            lambda r, error=error: r.set_error(
                administration_id=admin, recurring_id=created.id, error=error
            ),
        )
        loaded = await _as_repo(
            org, lambda r: r.get(administration_id=admin, recurring_id=created.id)
        )
        assert loaded is not None and loaded.last_error == error


async def test_fiscal_year_for_finds_the_year_containing_the_date(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)  # a 2026 fiscal year
    admin, org = two_organizations.admin_a, two_organizations.org_a

    inside = await _as_repo(
        org, lambda r: r.fiscal_year_for(administration_id=admin, on=date(2026, 8, 31))
    )
    outside = await _as_repo(
        org, lambda r: r.fiscal_year_for(administration_id=admin, on=date(2019, 1, 1))
    )

    assert inside == world["year"]
    assert outside is None  # never invented


async def test_recording_a_date_twice_is_reported_as_already_generated(
    two_organizations: SeededTenants,
) -> None:
    """The race the service cannot close: the database catches it and the repository
    turns it into `RunAlreadyGenerated`, not a 500."""
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    created = await _as_repo(
        org,
        lambda r: r.create(
            administration_id=admin,
            user_id=user,
            definition=_definition(customer),
            next_run_on=date(2026, 7, 31),
        ),
    )
    first = await _draft(two_organizations, world)
    second = await _draft(two_organizations, world)

    async def record(invoice: uuid.UUID) -> None:
        await _as_repo(
            org,
            lambda r: r.record_run(
                administration_id=admin,
                recurring_id=created.id,
                run_date=date(2026, 7, 31),
                invoice_id=invoice,
                issued=False,
                issue_error="no_open_period",
            ),
        )

    await record(first)
    with pytest.raises(RunAlreadyGenerated):
        await record(second)


async def test_customer_exists_is_scoped_to_the_administration(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org = two_organizations.admin_a, two_organizations.org_a

    assert await _as_repo(
        org, lambda r: r.customer_exists(administration_id=admin, customer_id=customer)
    )
    assert not await _as_repo(
        org, lambda r: r.customer_exists(administration_id=admin, customer_id=uuid.uuid4())
    )
    assert not await _as_repo(
        two_organizations.org_b,
        lambda r: r.customer_exists(administration_id=admin, customer_id=customer),
    )

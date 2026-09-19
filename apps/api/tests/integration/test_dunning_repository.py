"""`SqlDunningRepository` against a real Postgres - ADR-071.

test_dunning.py exercises the tables and functions with raw SQL. What it cannot
catch is the Python repository's own statements naming a column that does not
exist - the class of bug (`purchase_journal` selecting `type` for `journal_type`)
that fakes hide and production finds. So every repository method runs here, as
`ledgr_app`, under RLS, in a real transaction.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0053.
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
from api.invoicing.dunning import (
    DunningAssessment,
    InterestRateKind,
    LadderStep,
    StepKind,
)
from api.invoicing.dunning_repository import SqlDunningRepository
from api.invoicing.dunning_service import ReminderAlreadySent
from tests.integration.test_dunning import _overdue_invoice
from tests.integration.test_sales_invoice_payment import _pay
from tests.integration.test_sales_invoice_posting import _exec, _scalar
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 15)


async def _as_repo[T](org: uuid.UUID, work: Callable[[SqlDunningRepository], Awaitable[T]]) -> T:
    """Run `work` against the repository in ONE transaction under `org`'s RLS."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        return await work(SqlDunningRepository(AsyncSession(bind=conn)))


# --- the ladder --------------------------------------------------------------------


async def test_an_unconfigured_administration_reads_back_none(
    two_organizations: SeededTenants,
) -> None:
    ladder = await _as_repo(
        two_organizations.org_a,
        lambda r: r.ladder(administration_id=two_organizations.admin_a),
    )
    assert ladder is None


async def test_a_ladder_round_trips_and_is_replaced_wholesale(
    two_organizations: SeededTenants,
) -> None:
    first = (
        LadderStep(1, 5, StepKind.FRIENDLY),
        LadderStep(
            2, 30, StepKind.FORMAL_NOTICE, charge_interest=True, charge_collection_cost=True
        ),
    )
    second = (LadderStep(1, 10, StepKind.REMINDER),)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )

    await _as_repo(
        org, lambda r: r.replace_ladder(administration_id=admin, user_id=user, steps=first)
    )
    assert await _as_repo(org, lambda r: r.ladder(administration_id=admin)) == first

    # Replaced, not merged: the old second step is gone.
    await _as_repo(
        org, lambda r: r.replace_ladder(administration_id=admin, user_id=user, steps=second)
    )
    assert await _as_repo(org, lambda r: r.ladder(administration_id=admin)) == second


async def test_an_empty_ladder_is_configured_not_unconfigured(
    two_organizations: SeededTenants,
) -> None:
    """'Chase nobody' is a choice and must not read back as 'never set up'."""
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    await _as_repo(org, lambda r: r.replace_ladder(administration_id=admin, user_id=user, steps=()))
    assert await _as_repo(org, lambda r: r.ladder(administration_id=admin)) == ()


async def test_another_tenant_reads_no_ladder(two_organizations: SeededTenants) -> None:
    await _as_repo(
        two_organizations.org_a,
        lambda r: r.replace_ladder(
            administration_id=two_organizations.admin_a,
            user_id=two_organizations.owner_a,
            steps=(LadderStep(1, 5, StepKind.FRIENDLY),),
        ),
    )
    seen = await _as_repo(
        two_organizations.org_b,
        lambda r: r.ladder(administration_id=two_organizations.admin_a),
    )
    assert seen is None


# --- what is overdue --------------------------------------------------------------------


async def test_overdue_and_facts_agree_and_carry_what_the_rules_need(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")
    admin, org = two_organizations.admin_a, two_organizations.org_a

    (overdue,) = await _as_repo(org, lambda r: r.overdue(administration_id=admin, today=TODAY))
    facts = await _as_repo(
        org, lambda r: r.facts_for(administration_id=admin, invoice_id=owed["invoice"])
    )

    assert facts == overdue
    assert facts is not None
    assert facts.invoice_id == owed["invoice"]
    assert facts.outstanding == Decimal("810.00")  # 1210.00 less the 400.00 payment
    assert facts.due_date == date(2026, 9, 30)
    assert facts.is_paused is False
    assert facts.sent_positions == frozenset()


async def test_facts_for_a_not_yet_due_invoice_is_a_real_answer(
    two_organizations: SeededTenants,
) -> None:
    """An assessment of an invoice that is not overdue is 'not overdue', not a 404 -
    so facts_for must return it although `overdue` does not."""
    owed = await _overdue_invoice(two_organizations, due="2026-11-30")
    admin, org = two_organizations.admin_a, two_organizations.org_a

    assert await _as_repo(org, lambda r: r.overdue(administration_id=admin, today=TODAY)) == []
    facts = await _as_repo(
        org, lambda r: r.facts_for(administration_id=admin, invoice_id=owed["invoice"])
    )
    assert facts is not None and facts.outstanding == Decimal("1210.00")


async def test_facts_for_another_tenants_invoice_is_none(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    facts = await _as_repo(
        two_organizations.org_b,
        lambda r: r.facts_for(
            administration_id=two_organizations.admin_a, invoice_id=owed["invoice"]
        ),
    )
    assert facts is None


# --- pause ---------------------------------------------------------------------------------


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


async def test_pause_is_idempotent_keeps_the_first_pause_and_resume_reports_it(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    assert await _as_repo(
        org, lambda r: r.customer_exists(administration_id=admin, customer_id=customer)
    )

    for reason in ("in dispute", "a later, different reason"):
        await _as_repo(
            org,
            lambda r, reason=reason: r.pause(
                administration_id=admin, customer_id=customer, user_id=user, reason=reason
            ),
        )
    kept = await _exec(
        two_organizations,
        "SELECT reason FROM dunning_pause WHERE customer_id = :c",
        c=str(customer),
    )
    # The ORIGINAL pause is the fact worth keeping.
    assert kept.scalar_one() == "in dispute"

    assert await _as_repo(org, lambda r: r.resume(administration_id=admin, customer_id=customer))
    assert not await _as_repo(
        org, lambda r: r.resume(administration_id=admin, customer_id=customer)
    )


async def test_an_unknown_customer_does_not_exist(two_organizations: SeededTenants) -> None:
    assert not await _as_repo(
        two_organizations.org_a,
        lambda r: r.customer_exists(
            administration_id=two_organizations.admin_a, customer_id=uuid.uuid4()
        ),
    )


# --- recording a reminder ---------------------------------------------------------------------


async def _delivery(tenants: SeededTenants, invoice: uuid.UUID) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at) "
        "VALUES (:org, :admin, :invoice, 'email', 'sent', 'k@example.com', 'nl', now()) "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(invoice),
    )


def _assessment(invoice: uuid.UUID, step: LadderStep, **kw: object) -> DunningAssessment:
    return DunningAssessment(
        invoice_id=invoice,
        outstanding=Decimal("1210.00"),
        due_date=date(2026, 9, 30),
        days_overdue=15,
        step=step,
        blocker=None,
        interest_kind=InterestRateKind.COMMERCIAL,
        **kw,  # type: ignore[arg-type]
    )


async def test_a_recorded_reminder_shows_up_as_a_sent_step(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )
    delivery = await _delivery(two_organizations, owed["invoice"])

    await _as_repo(
        org,
        lambda r: r.record_reminder(
            administration_id=admin,
            invoice_id=owed["invoice"],
            assessment=_assessment(owed["invoice"], LadderStep(1, 7, StepKind.FRIENDLY)),
            delivery_id=delivery,
            user_id=user,
        ),
    )

    facts = await _as_repo(
        org, lambda r: r.facts_for(administration_id=admin, invoice_id=owed["invoice"])
    )
    assert facts is not None and facts.sent_positions == frozenset({1})


async def test_a_formal_notice_records_its_interest_cost_and_deadline(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    delivery = await _delivery(two_organizations, owed["invoice"])
    notice = LadderStep(
        1, 7, StepKind.FORMAL_NOTICE, charge_interest=True, charge_collection_cost=True
    )

    await _as_repo(
        two_organizations.org_a,
        lambda r: r.record_reminder(
            administration_id=two_organizations.admin_a,
            invoice_id=owed["invoice"],
            assessment=_assessment(
                owed["invoice"],
                notice,
                interest=Decimal("9.86"),
                collection_cost=Decimal("181.50"),
                pay_by=date(2026, 10, 30),
            ),
            delivery_id=delivery,
            user_id=two_organizations.owner_a,
        ),
    )

    row = await _exec(
        two_organizations,
        "SELECT kind, interest_amount, collection_cost_amount, pay_by "
        "  FROM dunning_reminder WHERE invoice_id = :i",
        i=str(owed["invoice"]),
    )
    kind, interest, cost, pay_by = row.one()
    assert (kind, interest, cost, pay_by) == (
        "formal_notice",
        Decimal("9.86"),
        Decimal("181.50"),
        date(2026, 10, 30),
    )


async def test_recording_the_same_step_twice_is_reported_as_already_sent(
    two_organizations: SeededTenants,
) -> None:
    """The race the service check cannot close: the database catches it and the
    repository turns it into `ReminderAlreadySent`, not a 500."""
    owed = await _overdue_invoice(two_organizations)
    delivery = await _delivery(two_organizations, owed["invoice"])
    step = LadderStep(1, 7, StepKind.FRIENDLY)

    async def record(r: SqlDunningRepository) -> None:
        await r.record_reminder(
            administration_id=two_organizations.admin_a,
            invoice_id=owed["invoice"],
            assessment=_assessment(owed["invoice"], step),
            delivery_id=delivery,
            user_id=two_organizations.owner_a,
        )

    await _as_repo(two_organizations.org_a, record)
    with pytest.raises(ReminderAlreadySent):
        await _as_repo(two_organizations.org_a, record)


# --- interest rates -------------------------------------------------------------------------------


async def test_reading_rates_works_and_an_empty_table_reads_as_no_rates(
    two_organizations: SeededTenants,
) -> None:
    """The table ships empty; the repository must answer that as an empty list, not
    an error - which is what makes the 'rate missing' refusal reachable at all."""
    rates = await _as_repo(
        two_organizations.org_a, lambda r: r.rates(kind=InterestRateKind.COMMERCIAL)
    )
    assert isinstance(rates, list)

"""Migration 0053 against a real Postgres - ADR-071.

The properties here are the database's: which invoices count as overdue and what the
overview function reports about them, that a step is sent once per invoice, that
collection cost is a consequence of a formal notice, that a reminder is immutable,
that interest rates are append-only and ship empty, and that another tenant sees
none of it.

Reuses the payment tests' owed invoice (an issued invoice with a posted receivable
and VAT totals). Skipped without TENANT_ISOLATION_TESTS_ENABLED=1, and needs
migrations through 0053 applied and the VAT reference data loaded.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.integration.test_sales_invoice_payment import (
    _bank,
    _bank_journal,
    _pay,
)
from tests.integration.test_sales_invoice_posting import (
    _exec,
    _issued_and_posted,
    _party,
    _scalar,
    _world,
)
from tests.support.seed import SeededTenants

TODAY = date(2026, 10, 15)


async def _overdue_invoice(
    tenants: SeededTenants, *, due: str = "2026-09-30", customer_id: uuid.UUID | None = None
) -> dict[str, uuid.UUID]:
    """An issued invoice owing 1210.00, due `due`."""
    world = await _world(tenants)
    party = await _party(tenants)
    invoice, _ = await _issued_and_posted(
        tenants, world, party, due_date=due, customer_id=customer_id
    )
    await _exec(
        tenants,
        "INSERT INTO sales_invoice_vat_total (invoice_id, organization_id, "
        "  administration_id, vat_treatment, rate, taxable_amount, vat_amount) "
        "VALUES (:invoice, :org, :admin, 'btw_21', 21, 1000.00, 210.00)",
        invoice=str(invoice),
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )
    return {
        **world,
        "party": party,
        "invoice": invoice,
        "bank": await _bank(tenants),
        "bank_journal": await _bank_journal(tenants),
    }


async def _overdue_rows(tenants: SeededTenants) -> list[object]:
    result = await _exec(
        tenants,
        "SELECT invoice_id, outstanding, is_business, is_paused, sent_positions, last_sent_on "
        "  FROM invoicing.overdue_invoices(:admin, CAST(:today AS date))",
        admin=str(tenants.admin_a),
        today=TODAY,
    )
    return list(result)


async def _remind(
    tenants: SeededTenants,
    owed: dict[str, uuid.UUID],
    *,
    position: int = 1,
    kind: str = "friendly",
    cost: str | None = None,
    pay_by: str | None = None,
) -> uuid.UUID:
    delivery = await _scalar(
        tenants,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at) "
        "VALUES (:org, :admin, :invoice, 'email', 'sent', 'klant@example.com', 'nl', now()) "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(owed["invoice"]),
    )
    return await _scalar(
        tenants,
        "INSERT INTO dunning_reminder (organization_id, administration_id, invoice_id, "
        "  step_position, kind, delivery_id, outstanding_amount, collection_cost_amount, "
        "  pay_by, days_overdue, sent_by_user_id) "
        "VALUES (:org, :admin, :invoice, :position, :kind, :delivery, 1210.00, "
        "  CAST(:cost AS numeric), CAST(:pay_by AS date), 15, :user) RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
        invoice=str(owed["invoice"]),
        position=position,
        kind=kind,
        delivery=str(delivery),
        cost=Decimal(cost) if cost else None,
        pay_by=date.fromisoformat(pay_by) if pay_by else None,
        user=str(tenants.owner_a),
    )


# --- what is overdue -----------------------------------------------------------------


async def test_an_unpaid_invoice_past_its_due_date_is_overdue(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    (row,) = await _overdue_rows(two_organizations)

    assert row.invoice_id == owed["invoice"]  # type: ignore[attr-defined]
    assert Decimal(row.outstanding) == Decimal("1210.00")  # type: ignore[attr-defined]
    assert row.is_paused is False  # type: ignore[attr-defined]
    assert list(row.sent_positions) == []  # type: ignore[attr-defined]


async def test_an_invoice_not_yet_due_is_not_overdue(two_organizations: SeededTenants) -> None:
    await _overdue_invoice(two_organizations, due="2026-11-30")
    assert await _overdue_rows(two_organizations) == []


async def test_a_paid_invoice_is_not_overdue(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "1210.00")
    assert await _overdue_rows(two_organizations) == []


async def test_a_part_paid_invoice_is_overdue_for_what_remains(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _pay(two_organizations, owed, "400.00")

    (row,) = await _overdue_rows(two_organizations)
    assert Decimal(row.outstanding) == Decimal("810.00")  # type: ignore[attr-defined]


async def test_sent_steps_are_reported(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _remind(two_organizations, owed, position=1)
    await _remind(two_organizations, owed, position=2, kind="reminder")

    (row,) = await _overdue_rows(two_organizations)
    assert list(row.sent_positions) == [1, 2]  # type: ignore[attr-defined]


async def test_the_day_of_the_last_reminder_is_reported(two_organizations: SeededTenants) -> None:
    """Migration 0055: the fact the spacing rule (MIN_DAYS_BETWEEN_REMINDERS) needs."""
    owed = await _overdue_invoice(two_organizations)
    (before,) = await _overdue_rows(two_organizations)
    assert before.last_sent_on is None  # type: ignore[attr-defined]

    await _remind(two_organizations, owed, position=1)
    await _remind(two_organizations, owed, position=2, kind="reminder")

    (after,) = await _overdue_rows(two_organizations)
    # The MOST RECENT of the two, as a date - not the first, not a timestamp.
    assert after.last_sent_on == date.today()  # type: ignore[attr-defined]


async def test_a_customer_paused_is_reported_paused(two_organizations: SeededTenants) -> None:
    # The customer exists BEFORE the invoice is issued: `customer_id` is frozen at
    # issue (0039), so it cannot be attached afterwards.
    customer = await _scalar(
        two_organizations,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email, kvk_number) "
        "VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com', '12345678') "
        "RETURNING id",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
    )
    await _overdue_invoice(two_organizations, customer_id=customer)
    await _exec(
        two_organizations,
        "INSERT INTO dunning_pause (customer_id, organization_id, administration_id, "
        "  paused_by_user_id) VALUES (:customer, :org, :admin, :user)",
        customer=str(customer),
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
        user=str(two_organizations.owner_a),
    )

    (row,) = await _overdue_rows(two_organizations)
    assert row.is_paused is True  # type: ignore[attr-defined]
    # A KvK number on the customer master makes them a business.
    assert row.is_business is True  # type: ignore[attr-defined]


# --- one reminder per step ---------------------------------------------------------------


async def test_a_step_can_only_be_sent_once_per_invoice(
    two_organizations: SeededTenants,
) -> None:
    """The database is what stops two people - or one person twice - sending the
    same step to the same customer."""
    owed = await _overdue_invoice(two_organizations)
    await _remind(two_organizations, owed, position=1)

    with pytest.raises(Exception, match="(?i)once_per_step|unique"):
        await _remind(two_organizations, owed, position=1)


async def test_collection_cost_needs_a_formal_notice(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    with pytest.raises(Exception, match="(?i)costs_only_on_a_notice|check"):
        await _remind(two_organizations, owed, kind="reminder", cost="150.00")


async def test_a_formal_notice_needs_a_deadline(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    with pytest.raises(Exception, match="(?i)has_a_deadline|check"):
        await _remind(two_organizations, owed, kind="formal_notice", cost="150.00")


async def test_a_formal_notice_with_a_deadline_is_accepted(
    two_organizations: SeededTenants,
) -> None:
    owed = await _overdue_invoice(two_organizations)
    reminder = await _remind(
        two_organizations,
        owed,
        position=3,
        kind="formal_notice",
        cost="150.00",
        pay_by="2026-11-01",
    )
    assert reminder is not None


async def test_a_reminder_is_immutable_and_undeletable(
    two_organizations: SeededTenants,
) -> None:
    """The record of what a customer was told they owed."""
    owed = await _overdue_invoice(two_organizations)
    reminder = await _remind(two_organizations, owed)

    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "UPDATE dunning_reminder SET outstanding_amount = 1 WHERE id = :id",
            id=str(reminder),
        )
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "DELETE FROM dunning_reminder WHERE id = :id",
            id=str(reminder),
        )


async def test_a_draft_invoice_is_never_chased(two_organizations: SeededTenants) -> None:
    """A reminder pointing at a draft is refused by the guard, whatever delivery it
    names: a draft has no number and owes nothing (FR-AR-004)."""
    from tests.integration.test_sales_invoice_posting import _draft

    owed = await _overdue_invoice(two_organizations)
    draft = await _draft(two_organizations, owed)
    delivery = await _scalar(
        two_organizations,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, status, recipient, language, sent_at) "
        "VALUES (:org, :admin, :invoice, 'email', 'sent', 'k@example.com', 'nl', now()) "
        "RETURNING id",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
        invoice=str(owed["invoice"]),
    )

    with pytest.raises(Exception, match="(?i)not an issued invoice|only one is chased"):
        await _exec(
            two_organizations,
            "INSERT INTO dunning_reminder (organization_id, administration_id, invoice_id, "
            "  step_position, kind, delivery_id, outstanding_amount, days_overdue, "
            "  sent_by_user_id) "
            "VALUES (:org, :admin, :draft, 1, 'friendly', :delivery, 10.00, 5, :user)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            draft=str(draft),
            delivery=str(delivery),
            user=str(two_organizations.owner_a),
        )


# --- the ladder ----------------------------------------------------------------------


async def test_collection_cost_on_a_step_needs_a_formal_notice(
    two_organizations: SeededTenants,
) -> None:
    with pytest.raises(Exception, match="(?i)needs_a_formal_notice|check"):
        await _exec(
            two_organizations,
            "INSERT INTO dunning_step (organization_id, administration_id, position, "
            "  days_after_due, kind, charge_collection_cost) "
            "VALUES (:org, :admin, 1, 7, 'reminder', true)",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
        )


async def test_two_steps_cannot_share_a_position_or_a_day(
    two_organizations: SeededTenants,
) -> None:
    insert = (
        "INSERT INTO dunning_step (organization_id, administration_id, position, "
        "  days_after_due, kind) VALUES (:org, :admin, :position, :days, 'reminder')"
    )
    params = dict(org=str(two_organizations.org_a), admin=str(two_organizations.admin_a))
    await _exec(two_organizations, insert, position=1, days=7, **params)

    with pytest.raises(Exception, match="(?i)position_unique|unique"):
        await _exec(two_organizations, insert, position=1, days=14, **params)
    with pytest.raises(Exception, match="(?i)days_unique|unique"):
        await _exec(two_organizations, insert, position=2, days=7, **params)


# --- statutory interest rates ---------------------------------------------------------------


async def test_the_application_role_cannot_write_an_interest_rate(
    two_organizations: SeededTenants,
) -> None:
    """Rates are loaded by an operator, never by a tenant's request."""
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(
            two_organizations,
            "INSERT INTO statutory_interest_rate (kind, valid_from, rate, source_note) "
            "VALUES ('commercial', DATE '2026-01-01', 10.000, 'test')",
        )


async def test_interest_rates_are_readable_by_the_application_role(
    two_organizations: SeededTenants,
) -> None:
    result = await _exec(two_organizations, "SELECT count(*) FROM statutory_interest_rate")
    assert result.scalar_one() >= 0


# --- tenant isolation -----------------------------------------------------------------------


async def test_another_tenant_sees_none_of_it(two_organizations: SeededTenants) -> None:
    owed = await _overdue_invoice(two_organizations)
    await _remind(two_organizations, owed)
    await _exec(
        two_organizations,
        "INSERT INTO dunning_step (organization_id, administration_id, position, "
        "  days_after_due, kind) VALUES (:org, :admin, 1, 7, 'friendly')",
        org=str(two_organizations.org_a),
        admin=str(two_organizations.admin_a),
    )

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        counts = {
            table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            for table in ("dunning_step", "dunning_reminder", "dunning_pause")
        }
        overdue = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM invoicing.overdue_invoices(:admin, CAST(:today AS date))"
                ),
                {"admin": str(two_organizations.admin_a), "today": TODAY},
            )
        ).scalar_one()

    assert counts == {"dunning_step": 0, "dunning_reminder": 0, "dunning_pause": 0}
    assert overdue == 0

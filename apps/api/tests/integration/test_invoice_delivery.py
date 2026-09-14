"""Migration 0041 against a real Postgres - FR-AR-005.

The properties here are the database's: that a draft cannot be dispatched, that
a settled dispatch is history, that the per-channel status function answers with
the LATEST dispatch, and that a dispatch cannot be attached to another tenant's
invoice.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio


async def _exec(  # type: ignore[no-untyped-def]
    tenants: SeededTenants, sql: str, *, as_org: uuid.UUID | None = None, **params: object
):
    """Run one statement inside a tenant context.

    The session-org argument is `as_org` rather than `org` deliberately: every
    statement below binds a SQL parameter called `:org`, and a keyword named
    the same would be swallowed by this signature instead of reaching the
    query - which fails as "missing bind parameter" rather than as anything
    that points at the cause.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(as_org or tenants.org_a)},
        )
        return await conn.execute(text(sql), params)


async def _invoice(
    tenants: SeededTenants,
    *,
    admin: uuid.UUID | None = None,
    org: uuid.UUID | None = None,
    issued: bool = True,
) -> uuid.UUID:
    admin = admin or tenants.admin_a
    org = org or tenants.org_a

    # `as_org` as well as the `:org` binding: seeding org B's invoice needs
    # org B's tenant context, or RLS refuses the insert.
    #
    # ON CONFLICT DO UPDATE, not a plain INSERT: fiscal_year_unique_start
    # (0001) is (administration_id, start_date), and every call here uses
    # the same hardcoded 2026-01-01 - fine the one time _invoice() is called
    # per administration, but test_undelivered_invoices_lists_what_never_
    # went_out calls it twice for the SAME admin_a (one invoice sent, one
    # not - the whole point of that test), and a second plain INSERT there
    # collided with the first. Real usage is exactly this: one fiscal year,
    # many invoices inside it, so reusing the existing row - the DO UPDATE
    # is a no-op that only exists to make RETURNING id work on the conflict
    # path too - is the correct shape, not a workaround.
    result = await _exec(
        tenants,
        "INSERT INTO fiscal_year (organization_id, administration_id, start_date, end_date) "
        "VALUES (:org, :admin, '2026-01-01', '2026-12-31') "
        "ON CONFLICT ON CONSTRAINT fiscal_year_unique_start "
        "DO UPDATE SET end_date = excluded.end_date "
        "RETURNING id",
        as_org=org,
        org=str(org),
        admin=str(admin),
    )
    year = result.scalar_one()

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(org)},
        )
        invoice = (
            await conn.execute(
                text(
                    "INSERT INTO sales_invoice (organization_id, administration_id, "
                    "  fiscal_year_id, invoice_date, customer_name, customer_address, "
                    "  customer_country, customer_language) "
                    "VALUES (:org, :admin, :year, '2026-09-09', 'De Vries Holding B.V.', "
                    "  'Damrak 70', 'NL', 'nl') RETURNING id"
                ),
                {"org": str(org), "admin": str(admin), "year": str(year)},
            )
        ).scalar_one()
        if issued:
            await conn.execute(
                text("UPDATE sales_invoice SET status = 'issued' WHERE id = :id"),
                {"id": str(invoice)},
            )
    return invoice  # type: ignore[no-any-return]


async def _dispatch(
    tenants: SeededTenants,
    invoice: uuid.UUID,
    *,
    channel: str = "email",
    recipient: str = "facturen@devries.example",
    admin: uuid.UUID | None = None,
    org: uuid.UUID | None = None,
) -> uuid.UUID:
    result = await _exec(
        tenants,
        "INSERT INTO invoice_delivery (organization_id, administration_id, invoice_id, "
        "  channel, recipient, language, requested_by_user_id) "
        "VALUES (:org, :admin, :invoice, :channel, :recipient, 'nl', :user) RETURNING id",
        org=str(org or tenants.org_a),
        admin=str(admin or tenants.admin_a),
        invoice=str(invoice),
        channel=channel,
        recipient=recipient,
        user=str(tenants.owner_a),
    )
    return result.scalar_one()  # type: ignore[no-any-return]


# --- what may be dispatched ---------------------------------------------------


async def test_an_issued_invoice_can_be_dispatched(two_organizations: SeededTenants) -> None:
    invoice = await _invoice(two_organizations)
    assert await _dispatch(two_organizations, invoice) is not None


async def test_a_draft_cannot_be_dispatched(two_organizations: SeededTenants) -> None:
    """It carries no number. Sending one would put a document with no invoice
    reference in a customer's hands, which FR-AR-004 spends a whole numbering
    scheme preventing.
    """
    invoice = await _invoice(two_organizations, issued=False)

    with pytest.raises(Exception, match="(?i)cannot be delivered|FR-AR-005"):
        await _dispatch(two_organizations, invoice)


async def test_a_dispatch_cannot_name_another_tenants_invoice(
    two_organizations: SeededTenants,
) -> None:
    """IAM-001. A dispatch carrying a different administration from its invoice
    would send one tenant's document under another tenant's name.
    """
    foreign = await _invoice(
        two_organizations, admin=two_organizations.admin_b, org=two_organizations.org_b
    )

    with pytest.raises(Exception, match="(?i)IAM-001|does not exist"):
        await _dispatch(two_organizations, foreign)


async def test_the_organization_is_derived_not_trusted(
    two_organizations: SeededTenants,
) -> None:
    """`invoice_delivery_needs_an_issued_invoice` overwrites it from the
    invoice, so a caller cannot claim one.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    result = await _exec(
        two_organizations,
        "SELECT organization_id FROM invoice_delivery WHERE id = :id",
        id=str(dispatch),
    )
    assert result.scalar_one() == two_organizations.org_a


# --- the state machine --------------------------------------------------------


async def test_a_sent_dispatch_must_carry_a_date(two_organizations: SeededTenants) -> None:
    """`invoice_delivery_sent_is_dated`. "We sent it" with no moment attached
    is not a record of anything.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    with pytest.raises(Exception, match="(?i)check constraint"):
        await _exec(
            two_organizations,
            "UPDATE invoice_delivery SET status = 'sent' WHERE id = :id",
            id=str(dispatch),
        )


async def test_a_settled_dispatch_cannot_change_status(
    two_organizations: SeededTenants,
) -> None:
    """Once a provider has accepted a message no outcome here can recall it, so
    the record of what happened must not be editable either.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'failed', settled_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    with pytest.raises(Exception, match="(?i)settled|new dispatch"):
        await _exec(
            two_organizations,
            "UPDATE invoice_delivery SET status = 'queued', settled_at = NULL  WHERE id = :id",
            id=str(dispatch),
        )


async def test_a_sent_dispatch_may_still_bounce(two_organizations: SeededTenants) -> None:
    """The whole reason `sent` and `delivered` are separate states: the
    provider tells us what became of the message minutes later.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'sent', sent_at = now() WHERE id = :id",
        id=str(dispatch),
    )
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'bounced', settled_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    result = await _exec(
        two_organizations,
        "SELECT status FROM invoice_delivery WHERE id = :id",
        id=str(dispatch),
    )
    assert result.scalar_one() == "bounced"


async def test_a_sent_dispatch_cannot_return_to_queued(
    two_organizations: SeededTenants,
) -> None:
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'sent', sent_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    with pytest.raises(Exception, match="(?i)accepted by the provider"):
        await _exec(
            two_organizations,
            "UPDATE invoice_delivery SET status = 'queued', sent_at = NULL WHERE id = :id",
            id=str(dispatch),
        )


async def test_where_it_went_cannot_be_rewritten_after_hand_over(
    two_organizations: SeededTenants,
) -> None:
    """The address is the record of where a document actually went. Editing it
    afterwards would make the audit trail describe a send that did not happen.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'sent', sent_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    with pytest.raises(Exception, match="(?i)cannot change"):
        await _exec(
            two_organizations,
            "UPDATE invoice_delivery SET recipient = 'elsewhere@example.com'  WHERE id = :id",
            id=str(dispatch),
        )


async def test_only_a_queued_dispatch_carries_a_schedule(
    two_organizations: SeededTenants,
) -> None:
    """A retry time on a settled row would make a drained queue re-send an
    invoice that already arrived.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    with pytest.raises(Exception, match="(?i)check constraint"):
        await _exec(
            two_organizations,
            "UPDATE invoice_delivery SET status = 'sent', sent_at = now(), "
            "  next_attempt_at = now() + interval '5 minutes' WHERE id = :id",
            id=str(dispatch),
        )


async def test_a_dispatch_cannot_be_deleted(two_organizations: SeededTenants) -> None:
    """CMP-009's posture: "we never sent that" is exactly the question this
    table exists to answer, so the row cannot be removed.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)

    with pytest.raises(Exception, match="(?i)permission denied|policy"):
        await _exec(
            two_organizations,
            "DELETE FROM invoice_delivery WHERE id = :id",
            id=str(dispatch),
        )


# --- FR-AR-005's per-channel status -------------------------------------------


async def test_the_state_function_answers_the_latest_per_channel(
    two_organizations: SeededTenants,
) -> None:
    """One row per dispatch, and the current state of a channel is the most
    recent one. A resend after a bounce shows the resend, and the bounce is
    still on the row behind it.
    """
    invoice = await _invoice(two_organizations)

    first = await _dispatch(two_organizations, invoice, recipient="oud@example.com")
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'bounced', settled_at = now() WHERE id = :id",
        id=str(first),
    )
    second = await _dispatch(two_organizations, invoice, recipient="nieuw@example.com")
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'sent', sent_at = now() WHERE id = :id",
        id=str(second),
    )
    await _dispatch(two_organizations, invoice, channel="post", recipient="Damrak 70")

    result = await _exec(
        two_organizations,
        "SELECT channel, status, recipient FROM invoicing.delivery_state_of(:id)  ORDER BY channel",
        id=str(invoice),
    )
    rows = result.fetchall()

    assert [r.channel for r in rows] == ["email", "post"]
    email = next(r for r in rows if r.channel == "email")
    assert email.status == "sent"
    assert email.recipient == "nieuw@example.com"


async def test_the_bounce_survives_the_resend(two_organizations: SeededTenants) -> None:
    """The history is one row per human decision - which is the grain somebody
    asking "did this ever bounce" is actually asking about.
    """
    invoice = await _invoice(two_organizations)
    first = await _dispatch(two_organizations, invoice, recipient="oud@example.com")
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'bounced', settled_at = now() WHERE id = :id",
        id=str(first),
    )
    await _dispatch(two_organizations, invoice, recipient="nieuw@example.com")

    result = await _exec(
        two_organizations,
        "SELECT count(*) FROM invoice_delivery  WHERE invoice_id = :id AND status = 'bounced'",
        id=str(invoice),
    )
    assert result.scalar_one() == 1


async def test_undelivered_invoices_lists_what_never_went_out(
    two_organizations: SeededTenants,
) -> None:
    """A work list, not an alarm: issuing and sending are separate acts, so an
    invoice can legitimately sit here for a while.
    """
    never_sent = await _invoice(two_organizations)
    sent = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, sent)
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'sent', sent_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    result = await _exec(
        two_organizations,
        "SELECT invoice_id FROM invoicing.undelivered_invoices(:admin)",
        admin=str(two_organizations.admin_a),
    )
    listed = {row.invoice_id for row in result}

    assert never_sent in listed
    assert sent not in listed


async def test_a_bounced_invoice_returns_to_the_work_list(
    two_organizations: SeededTenants,
) -> None:
    """It was dispatched and did not arrive, which is exactly the case somebody
    needs to see - it looks sent and is not.
    """
    invoice = await _invoice(two_organizations)
    dispatch = await _dispatch(two_organizations, invoice)
    await _exec(
        two_organizations,
        "UPDATE invoice_delivery SET status = 'bounced', settled_at = now() WHERE id = :id",
        id=str(dispatch),
    )

    result = await _exec(
        two_organizations,
        "SELECT invoice_id, last_status FROM invoicing.undelivered_invoices(:admin)",
        admin=str(two_organizations.admin_a),
    )
    rows = [r for r in result if r.invoice_id == invoice]

    assert rows and rows[0].last_status == "bounced"

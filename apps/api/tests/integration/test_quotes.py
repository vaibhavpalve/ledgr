"""Migration 0057 and `SqlQuoteRepository` against a real Postgres - ADR-075.

What only the database can establish: the numbering counter, the lifecycle a second
writer cannot sidestep, that a quote is immutable once it has gone to a customer
(except extending its validity), that one quote converts into exactly one invoice, and
that another tenant sees none of it. Then every repository method as `ledgr_app`
under RLS.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; needs migrations through 0057.
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
from api.invoicing.quote_repository import SqlQuoteRepository
from api.invoicing.quote_service import QuoteDraft
from api.invoicing.quotes import QuoteKind, QuoteLine, QuoteStatus
from tests.integration.test_sales_invoice_posting import _draft, _exec, _scalar, _world
from tests.support.seed import SeededTenants


async def _as_repo[T](org: uuid.UUID, work: Callable[[SqlQuoteRepository], Awaitable[T]]) -> T:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org)}
        )
        return await work(SqlQuoteRepository(AsyncSession(bind=conn)))


async def _customer(tenants: SeededTenants) -> uuid.UUID:
    return await _scalar(
        tenants,
        "INSERT INTO customer (organization_id, administration_id, name, delivery_channel, "
        "  invoice_email) VALUES (:org, :admin, 'De Vries Holding B.V.', 'email', 'f@example.com') "
        "RETURNING id",
        org=str(tenants.org_a),
        admin=str(tenants.admin_a),
    )


def _quote_draft(customer: uuid.UUID, **overrides: object) -> QuoteDraft:
    defaults: dict[str, object] = dict(
        kind=QuoteKind.QUOTE,
        customer_id=customer,
        subject="Nieuwe website",
        valid_until=date(2027, 1, 31),
        notes="In twee termijnen",
        lines=(
            QuoteLine(
                description="Ontwerp",
                quantity=Decimal("10.0000"),
                unit_price=Decimal("95.0000"),
                vat_treatment="btw_21",
                discount_percent=Decimal("5.0000"),
            ),
            QuoteLine(
                description="Hosting",
                quantity=Decimal("1.0000"),
                unit_price=Decimal("0.0350"),
                vat_treatment="btw_9",
            ),
        ),
    )
    defaults.update(overrides)
    return QuoteDraft(**defaults)  # type: ignore[arg-type]


async def _make(tenants: SeededTenants, customer: uuid.UUID, **overrides: object) -> uuid.UUID:
    created = await _as_repo(
        tenants.org_a,
        lambda r: r.create(
            administration_id=tenants.admin_a,
            user_id=tenants.owner_a,
            draft=_quote_draft(customer, **overrides),
        ),
    )
    return created.id


async def _set(tenants: SeededTenants, quote: uuid.UUID, sql: str, **params: object) -> None:
    await _exec(
        tenants,
        f"UPDATE sales_quote SET {sql} WHERE id = :quote",
        quote=str(quote),
        **params,
    )


# --- numbering --------------------
async def test_quotes_are_numbered_per_kind_from_one(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    ids = [await _make(two_organizations, customer) for _ in range(2)]
    confirmation = await _make(two_organizations, customer, kind=QuoteKind.ORDER_CONFIRMATION)

    rows = await _exec(
        two_organizations,
        "SELECT id, reference, quote_number FROM sales_quote WHERE administration_id = :admin",
        admin=str(two_organizations.admin_a),
    )
    by_id = {r.id: (r.reference, r.quote_number) for r in rows}
    assert [by_id[i] for i in ids] == [("OF-0001", 1), ("OF-0002", 2)]
    assert by_id[confirmation] == ("OB-0001", 1)  # its own series


async def test_a_number_is_never_supplied_or_changed(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)

    with pytest.raises(Exception, match="(?i)identity"):
        await _set(two_organizations, quote, "reference = 'OF-9999'")
    with pytest.raises(Exception, match="(?i)identity"):
        await _set(two_organizations, quote, "quote_number = 9")


async def test_a_quote_is_addressed_to_a_customer_of_its_own_administration(
    two_organizations: SeededTenants,
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        stranger = (
            await conn.execute(
                text(
                    "INSERT INTO customer (organization_id, administration_id, name, "
                    "  delivery_channel, invoice_email) "
                    "VALUES (:org, :admin, 'Elders B.V.', 'email', 'e@example.com') RETURNING id"
                ),
                {"org": str(two_organizations.org_b), "admin": str(two_organizations.admin_b)},
            )
        ).scalar_one()

    with pytest.raises(Exception, match="(?i)own administration|CLAUDE.md rule 1"):
        await _make(two_organizations, stranger)


# --- the lifecycle --------------------
@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("draft", "declined"),  # only a quote that is OUT can be declined
        ("draft", "converted"),
        ("sent", "converted"),  # only an ACCEPTED one converts
        ("sent", "draft"),  # never backwards
        ("accepted", "declined"),
        ("accepted", "sent"),
    ],
)
async def test_a_second_writer_cannot_skip_the_lifecycle(
    two_organizations: SeededTenants, start: str, target: str
) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    if start == "sent":
        await _set(two_organizations, quote, "status = 'sent'")
    elif start == "accepted":
        await _set(two_organizations, quote, "status = 'accepted', accepted_at = now()")

    with pytest.raises(Exception, match="(?i)cannot go from|converted|violates"):
        await _set(two_organizations, quote, f"status = '{target}'")


@pytest.mark.parametrize("terminal", ["declined", "cancelled"])
async def test_a_terminal_quote_is_history(two_organizations: SeededTenants, terminal: str) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    await _set(two_organizations, quote, "status = 'sent'")
    await _set(two_organizations, quote, f"status = '{terminal}'")

    with pytest.raises(Exception, match="(?i)history"):
        await _set(two_organizations, quote, "notes = 'revised'")
    with pytest.raises(Exception, match="(?i)history"):
        await _set(two_organizations, quote, "status = 'sent'")


# --- immutable once out --------------------
async def test_a_quote_that_has_gone_out_is_what_the_customer_was_told(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    await _set(two_organizations, quote, "status = 'sent'")

    for change in ("subject = 'anders'", "notes = 'anders'", "valid_until = DATE '2027-01-01'"):
        with pytest.raises(Exception, match="(?i)no longer be edited|what the customer was told"):
            await _set(two_organizations, quote, change)


async def test_validity_can_be_extended_but_not_shortened_while_sent(
    two_organizations: SeededTenants,
) -> None:
    """The one edit allowed after sending: it can only help the customer."""
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)  # valid until 2027-01-31
    await _set(two_organizations, quote, "status = 'sent'")

    await _set(two_organizations, quote, "valid_until = DATE '2027-03-31'")  # later: fine
    with pytest.raises(Exception, match="(?i)no longer be edited"):
        await _set(two_organizations, quote, "valid_until = DATE '2027-02-01'")  # earlier


async def test_the_lines_of_a_quote_that_has_gone_out_cannot_change(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    await _set(two_organizations, quote, "status = 'sent'")

    with pytest.raises(Exception, match="(?i)only a draft is editable"):
        await _exec(
            two_organizations,
            "UPDATE sales_quote_line SET unit_price = 1 WHERE quote_id = :quote",
            quote=str(quote),
        )
    with pytest.raises(Exception, match="(?i)only a draft is editable"):
        await _exec(
            two_organizations,
            "DELETE FROM sales_quote_line WHERE quote_id = :quote",
            quote=str(quote),
        )
    with pytest.raises(Exception, match="(?i)only a draft is editable"):
        await _exec(
            two_organizations,
            "INSERT INTO sales_quote_line (organization_id, administration_id, quote_id, "
            "  position, description, quantity, unit_price, vat_treatment) "
            "VALUES (:org, :admin, :quote, 9, 'x', 1, 1, 'btw_21')",
            org=str(two_organizations.org_a),
            admin=str(two_organizations.admin_a),
            quote=str(quote),
        )


async def test_the_quoted_line_total_is_the_invoices_generated_total(
    two_organizations: SeededTenants,
) -> None:
    """The same generated column as an invoice line, so a quote's net total is what
    the invoice's will be: 10 x 95 less 5% = 902.50 ; 1 x 0.035 = 0.04 (half up)."""
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    rows = await _exec(
        two_organizations,
        "SELECT line_net FROM sales_quote_line WHERE quote_id = :q ORDER BY position",
        q=str(quote),
    )
    assert [Decimal(r.line_net) for r in rows] == [Decimal("902.50"), Decimal("0.04")]


# --- one quote, one invoice --------------------
async def test_a_converted_quote_needs_an_invoice_and_an_invoice_serves_one_quote(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    first = await _make(two_organizations, customer)
    second = await _make(two_organizations, customer)
    invoice = await _draft(two_organizations, world)
    for quote in (first, second):
        await _set(two_organizations, quote, "status = 'accepted', accepted_at = now()")

    with pytest.raises(Exception, match="(?i)converted_has_an_invoice|check"):
        await _set(two_organizations, first, "status = 'converted'")  # no invoice
    await _set(
        two_organizations, first, "status = 'converted', converted_invoice_id = :i", i=str(invoice)
    )
    with pytest.raises(Exception, match="(?i)sales_quote_invoice_idx|unique"):
        await _set(
            two_organizations,
            second,
            "status = 'converted', converted_invoice_id = :i",
            i=str(invoice),
        )


async def test_a_quote_is_cancelled_never_deleted(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    await _make(two_organizations, customer)
    with pytest.raises(Exception, match="(?i)permission denied"):
        await _exec(two_organizations, "DELETE FROM sales_quote")


async def test_another_tenant_sees_none_of_it(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    await _make(two_organizations, customer)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        counts = {
            table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            for table in ("sales_quote", "sales_quote_line", "sales_quote_counter")
        }
    assert counts == {"sales_quote": 0, "sales_quote_line": 0, "sales_quote_counter": 0}


# --- the repository --------------------
async def test_a_quote_round_trips_with_exact_decimals_and_lines(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    draft = _quote_draft(customer)
    created = await _as_repo(
        two_organizations.org_a,
        lambda r: r.create(
            administration_id=two_organizations.admin_a,
            user_id=two_organizations.owner_a,
            draft=draft,
        ),
    )

    loaded = await _as_repo(
        two_organizations.org_a,
        lambda r: r.get(administration_id=two_organizations.admin_a, quote_id=created.id),
    )

    assert loaded == created  # create answers exactly what a later read does
    assert loaded is not None
    assert loaded.lines == draft.lines  # both lines, exact four-decimal Decimals
    assert loaded.lines[1].unit_price == Decimal("0.0350")  # never rounded to the cent
    assert (loaded.reference, loaded.status) == ("OF-0001", QuoteStatus.DRAFT)


async def test_replace_swaps_a_drafts_content_and_all_its_lines(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    one_line = _quote_draft(
        customer,
        subject="Nieuw",
        lines=(
            QuoteLine(
                description="Alleen dit",
                quantity=Decimal("1"),
                unit_price=Decimal("10"),
                vat_treatment="btw_21",
            ),
        ),
    )

    replaced = await _as_repo(
        two_organizations.org_a,
        lambda r: r.replace(
            administration_id=two_organizations.admin_a, quote_id=quote, draft=one_line
        ),
    )

    assert replaced.subject == "Nieuw"
    assert [line.description for line in replaced.lines] == ["Alleen dit"]  # one, not three
    assert replaced.reference == "OF-0001"  # identity unchanged


async def test_list_filters_by_status_and_is_scoped_to_the_administration(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    draft_id = await _make(two_organizations, customer)
    sent_id = await _make(two_organizations, customer)
    await _set(two_organizations, sent_id, "status = 'sent'")
    admin, org = two_organizations.admin_a, two_organizations.org_a

    everything = await _as_repo(org, lambda r: r.list(administration_id=admin, status=None))
    only_sent = await _as_repo(
        org, lambda r: r.list(administration_id=admin, status=QuoteStatus.SENT)
    )
    other_tenant = await _as_repo(
        two_organizations.org_b, lambda r: r.list(administration_id=admin, status=None)
    )

    assert {q.id for q in everything} == {draft_id, sent_id}
    assert [q.id for q in only_sent] == [sent_id]
    assert other_tenant == []


async def test_transition_is_conditional_and_stamps_what_it_moved(
    two_organizations: SeededTenants,
) -> None:
    """A quote somebody else moved first is REPORTED, not overwritten."""
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    admin, org, user = (
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
    )

    moved = await _as_repo(
        org,
        lambda r: r.transition(
            administration_id=admin,
            quote_id=quote,
            expected=frozenset({QuoteStatus.DRAFT}),
            to=QuoteStatus.ACCEPTED,
            user_id=user,
            accepted_by_name="J. de Vries",
            acceptance_reference="PO-4471",
        ),
    )
    again = await _as_repo(
        org,
        lambda r: r.transition(
            administration_id=admin,
            quote_id=quote,
            expected=frozenset({QuoteStatus.DRAFT}),  # stale: it is no longer a draft
            to=QuoteStatus.SENT,
            user_id=user,
        ),
    )
    loaded = await _as_repo(org, lambda r: r.get(administration_id=admin, quote_id=quote))

    assert moved is True and again is False
    assert loaded is not None
    assert loaded.status is QuoteStatus.ACCEPTED and loaded.accepted_at is not None
    assert (loaded.accepted_by_name, loaded.acceptance_reference) == ("J. de Vries", "PO-4471")


async def test_a_decline_records_its_reason(two_organizations: SeededTenants) -> None:
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    await _set(two_organizations, quote, "status = 'sent'")

    await _as_repo(
        two_organizations.org_a,
        lambda r: r.transition(
            administration_id=two_organizations.admin_a,
            quote_id=quote,
            expected=frozenset({QuoteStatus.SENT}),
            to=QuoteStatus.DECLINED,
            user_id=two_organizations.owner_a,
            decline_reason="Te duur",
        ),
    )
    loaded = await _as_repo(
        two_organizations.org_a,
        lambda r: r.get(administration_id=two_organizations.admin_a, quote_id=quote),
    )
    assert loaded is not None
    assert (loaded.status, loaded.decline_reason) == (QuoteStatus.DECLINED, "Te duur")
    assert loaded.declined_at is not None


async def test_mark_converted_is_conditional_on_accepted(
    two_organizations: SeededTenants,
) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    invoice = await _draft(two_organizations, world)
    admin, org = two_organizations.admin_a, two_organizations.org_a

    not_accepted = await _as_repo(
        org,
        lambda r: r.mark_converted(administration_id=admin, quote_id=quote, invoice_id=invoice),
    )
    await _set(two_organizations, quote, "status = 'accepted', accepted_at = now()")
    converted = await _as_repo(
        org,
        lambda r: r.mark_converted(administration_id=admin, quote_id=quote, invoice_id=invoice),
    )
    twice = await _as_repo(
        org,
        lambda r: r.mark_converted(administration_id=admin, quote_id=quote, invoice_id=invoice),
    )
    loaded = await _as_repo(org, lambda r: r.get(administration_id=admin, quote_id=quote))

    assert (not_accepted, converted, twice) == (False, True, False)
    assert loaded is not None
    assert loaded.status is QuoteStatus.CONVERTED and loaded.converted_invoice_id == invoice


async def test_extend_validity_and_fiscal_year_lookup(two_organizations: SeededTenants) -> None:
    world = await _world(two_organizations)
    customer = await _customer(two_organizations)
    quote = await _make(two_organizations, customer)
    admin, org = two_organizations.admin_a, two_organizations.org_a

    await _as_repo(
        org,
        lambda r: r.extend_validity(
            administration_id=admin, quote_id=quote, valid_until=date(2027, 6, 30)
        ),
    )
    loaded = await _as_repo(org, lambda r: r.get(administration_id=admin, quote_id=quote))
    inside = await _as_repo(
        org, lambda r: r.fiscal_year_for(administration_id=admin, on=date(2026, 9, 1))
    )
    outside = await _as_repo(
        org, lambda r: r.fiscal_year_for(administration_id=admin, on=date(2019, 1, 1))
    )

    assert loaded is not None and loaded.valid_until == date(2027, 6, 30)
    assert inside == world["year"] and outside is None


async def test_customer_exists_is_scoped_to_the_administration(
    two_organizations: SeededTenants,
) -> None:
    customer = await _customer(two_organizations)
    admin, org = two_organizations.admin_a, two_organizations.org_a
    assert await _as_repo(
        org, lambda r: r.customer_exists(administration_id=admin, customer_id=customer)
    )
    assert not await _as_repo(
        two_organizations.org_b,
        lambda r: r.customer_exists(administration_id=admin, customer_id=customer),
    )

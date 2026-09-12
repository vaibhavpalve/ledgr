"""Migration 0039 against a real Postgres - FR-AR-006.

The constraints and triggers this file exercises are the ones
`tests/support/fake_customer_repository.py` reimplements in memory, so the two
are checked against each other rather than only against the tests that use
them. Where they disagree, the database is right.

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1 (see this package's conftest).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

_INSERT = """
    INSERT INTO customer (
        organization_id, administration_id, name,
        address_line1, postal_code, city, country,
        kvk_number, vat_number, payment_terms_days, credit_limit,
        delivery_channel, invoice_email, language
    ) VALUES (
        :org, :admin, :name,
        'Damrak 70', '1012 LM', 'Amsterdam', 'NL',
        :kvk, :vat, :terms, :limit,
        :channel, :email, :language
    )
    RETURNING id
"""


async def _insert(tenants: SeededTenants, **overrides: object) -> uuid.UUID:
    params: dict[str, object] = {
        "org": str(tenants.org_a),
        "admin": str(tenants.admin_a),
        "name": "De Vries Holding B.V.",
        "kvk": "12345678",
        "vat": "NL123456789B01",
        "terms": 30,
        "limit": Decimal("50000.00"),
        "channel": "email",
        "email": "facturen@devries.example",
        "language": "nl",
    }
    params.update(overrides)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        result = await conn.execute(text(_INSERT), params)
        return result.scalar_one()  # type: ignore[no-any-return]


# --- FR-AR-006's fields -----------------------------------------------------


async def test_a_customer_round_trips_every_field(two_organizations: SeededTenants) -> None:
    customer_id = await _insert(two_organizations)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT kvk_number, vat_number, vat_number_status, "
                    "       payment_terms_days, credit_limit, delivery_channel, "
                    "       language, archived_at "
                    "  FROM customer WHERE id = :id"
                ),
                {"id": str(customer_id)},
            )
        ).one()

    assert row.kvk_number == "12345678"
    assert row.vat_number == "NL123456789B01"
    # A new customer has been checked by nobody, which is not a rejection.
    assert row.vat_number_status == "unchecked"
    assert row.payment_terms_days == 30
    # NFR-031: numeric(19,2) comes back as Decimal, never a float.
    assert row.credit_limit == Decimal("50000.00")
    assert isinstance(row.credit_limit, Decimal)
    assert row.delivery_channel == "email"
    assert row.language == "nl"
    assert row.archived_at is None


# --- the CHECK constraints --------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"kvk": "1234567"}, "a KvK number is eight digits"),
        ({"kvk": "1234567X"}, "a KvK number is digits"),
        ({"terms": -1}, "payment terms cannot be negative"),
        ({"terms": 400}, "payment terms are bounded"),
        ({"limit": Decimal("-1.00")}, "a credit limit cannot be negative"),
        ({"channel": "carrier_pigeon"}, "the channel list is closed"),
        ({"language": "de"}, "only the languages the product ships"),
        ({"channel": "email", "email": None}, "an email channel needs an address"),
        ({"vat": "N1"}, "a VAT number is two letters and a national part"),
    ],
)
async def test_the_database_refuses_what_the_service_refuses(
    two_organizations: SeededTenants, overrides: dict[str, object], why: str
) -> None:
    """The second line is not the only line. Every one of these is also caught
    in `CustomerService._validated`, which exists to name the field - but the
    layer that cannot be bypassed is the one that has to be right.
    """
    with pytest.raises(Exception) as caught:
        await _insert(two_organizations, **overrides)

    assert (
        "violates check constraint" in str(caught.value).lower()
        or "invalid" in str(caught.value).lower()
    ), why


async def test_a_verdict_without_a_date_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """customer_vat_verdict_is_dated. A verdict is evidence for an
    intra-Community supply, and evidence without a date is unusable.
    """
    customer_id = await _insert(two_organizations)

    with pytest.raises(Exception, match="(?i)check constraint"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text("UPDATE customer SET vat_number_status = 'valid' WHERE id = :id"),
                {"id": str(customer_id)},
            )


async def test_a_verdict_about_no_number_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """customer_vat_verdict_needs_a_number. An answer about a number that is
    not there describes nothing.
    """
    with pytest.raises(Exception, match="(?i)check constraint"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    "INSERT INTO customer (organization_id, administration_id, name, "
                    "  delivery_channel, invoice_email, vat_number, vat_number_status, "
                    "  vat_number_checked_at) "
                    "VALUES (:org, :admin, 'Nameless B.V.', 'email', 'x@example.com', "
                    "  NULL, 'valid', now())"
                ),
                {"org": str(two_organizations.org_a), "admin": str(two_organizations.admin_a)},
            )


# --- no delete --------------------------------------------------------------


async def test_a_customer_cannot_be_deleted(two_organizations: SeededTenants) -> None:
    """0039 grants no DELETE on `customer` and declares no delete policy. A
    customer row is pointed at by statutory documents CMP-001 keeps for seven
    years, so "who was this invoice made out to" has to stay answerable.

    Archiving is the supported act, and it is what the API offers.
    """
    customer_id = await _insert(two_organizations)

    with pytest.raises(Exception, match="(?i)permission denied|policy"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text("DELETE FROM customer WHERE id = :id"), {"id": str(customer_id)}
            )


# --- updated_at -------------------------------------------------------------


async def test_updated_at_moves_on_its_own(two_organizations: SeededTenants) -> None:
    """Maintained by a trigger, not by the repository's UPDATE, so a second
    write path cannot leave a row whose contents moved and whose timestamp did
    not.
    """
    customer_id = await _insert(two_organizations)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        before = (
            await conn.execute(
                text("SELECT updated_at FROM customer WHERE id = :id"),
                {"id": str(customer_id)},
            )
        ).scalar_one()
        # Deliberately does NOT set updated_at.
        await conn.execute(
            text("UPDATE customer SET trade_name = 'De Vries' WHERE id = :id"),
            {"id": str(customer_id)},
        )
        after = (
            await conn.execute(
                text("SELECT updated_at FROM customer WHERE id = :id"),
                {"id": str(customer_id)},
            )
        ).scalar_one()

    assert after > before


# --- the link to sales_invoice ----------------------------------------------


async def _fiscal_year(tenants: SeededTenants) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO fiscal_year (organization_id, administration_id, "
                "  start_date, end_date) "
                "VALUES (:org, :admin, '2026-01-01', '2026-12-31') RETURNING id"
            ),
            {"org": str(tenants.org_a), "admin": str(tenants.admin_a)},
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def _draft_invoice(
    tenants: SeededTenants, *, customer_id: uuid.UUID | None, year_id: uuid.UUID
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO sales_invoice (organization_id, administration_id, "
                "  fiscal_year_id, invoice_date, customer_name, customer_address, "
                "  customer_country, customer_id, customer_language) "
                "VALUES (:org, :admin, :year, :on, 'De Vries Holding B.V.', "
                "  'Damrak 70', 'NL', :customer, 'nl') RETURNING id"
            ),
            {
                "org": str(tenants.org_a),
                "admin": str(tenants.admin_a),
                "year": str(year_id),
                "on": date(2026, 9, 9),
                "customer": str(customer_id) if customer_id else None,
            },
        )
        return result.scalar_one()  # type: ignore[no-any-return]


async def test_an_invoice_may_name_no_customer_at_all(
    two_organizations: SeededTenants,
) -> None:
    """A one-off customer stays invoiceable without first being made a master
    record - 0039 keeps `customer_id` nullable, permanently.
    """
    year = await _fiscal_year(two_organizations)
    invoice_id = await _draft_invoice(two_organizations, customer_id=None, year_id=year)

    assert invoice_id is not None


async def test_an_invoice_cannot_point_at_another_tenants_customer(
    two_organizations: SeededTenants,
) -> None:
    """sales_invoice_customer_same_tenant. RLS hides the row on read; this is
    what stops it being written in the first place (IAM-001).
    """
    year = await _fiscal_year(two_organizations)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        foreign = (
            await conn.execute(
                text(
                    "INSERT INTO customer (organization_id, administration_id, name, "
                    "  delivery_channel, invoice_email) "
                    "VALUES (:org, :admin, 'Andere B.V.', 'email', 'x@example.com') "
                    "RETURNING id"
                ),
                {
                    "org": str(two_organizations.org_b),
                    "admin": str(two_organizations.admin_b),
                },
            )
        ).scalar_one()

    with pytest.raises(Exception, match="(?i)IAM-001|does not exist"):
        await _draft_invoice(two_organizations, customer_id=foreign, year_id=year)


async def test_an_issued_invoices_customer_link_and_language_are_frozen(
    two_organizations: SeededTenants,
) -> None:
    """0039 extends 0037's freeze to both new columns.

    Re-pointing an issued invoice at a different customer would rewrite its
    provenance while every visible field stayed identical - the quietest
    possible corruption of a statutory record. And the language decides which
    legal wording the document carries (art. 226(11)), so changing it changes
    the document.
    """
    year = await _fiscal_year(two_organizations)
    customer_id = await _insert(two_organizations)
    other_id = await _insert(two_organizations, name="Tweede B.V.")
    invoice_id = await _draft_invoice(two_organizations, customer_id=customer_id, year_id=year)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text("UPDATE sales_invoice SET status = 'issued' WHERE id = :id"),
            {"id": str(invoice_id)},
        )

    for column, value in (("customer_id", str(other_id)), ("customer_language", "en")):
        with pytest.raises(Exception, match="(?i)has been issued"):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_org_id', :org, true)"),
                    {"org": str(two_organizations.org_a)},
                )
                await conn.execute(
                    text(f"UPDATE sales_invoice SET {column} = :value WHERE id = :id"),
                    {"value": value, "id": str(invoice_id)},
                )


async def test_editing_a_customer_does_not_touch_an_issued_invoice(
    two_organizations: SeededTenants,
) -> None:
    """The property the whole snapshot design exists for. A customer who moves
    must not rewrite the address on a document already filed.
    """
    year = await _fiscal_year(two_organizations)
    customer_id = await _insert(two_organizations)
    invoice_id = await _draft_invoice(two_organizations, customer_id=customer_id, year_id=year)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text("UPDATE sales_invoice SET status = 'issued' WHERE id = :id"),
            {"id": str(invoice_id)},
        )
        await conn.execute(
            text(
                "UPDATE customer SET name = 'Verhuisd B.V.', address_line1 = 'Elders 1', "
                "  language = 'en' WHERE id = :id"
            ),
            {"id": str(customer_id)},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT customer_name, customer_address, customer_language "
                    "  FROM sales_invoice WHERE id = :id"
                ),
                {"id": str(invoice_id)},
            )
        ).one()

    assert row.customer_name == "De Vries Holding B.V."
    assert row.customer_address == "Damrak 70"
    assert row.customer_language == "nl"

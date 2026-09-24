"""SI-13 against a real Postgres: the preview reads `vat.rules_on`, not a copy.

The unit tests run the preview over the in-memory rules repository, which
reimplements 0028. What only the database can establish is that
`SqlInvoiceRepository.effective_rules_on` - the one production reader - returns
the mapping in a shape `rubriek_preview.preview` accepts, as `ledgr_app`, with
no tenant context at all (the ruleset is global reference data).

Needs the VAT reference data loaded (`make load-vat`), like every test that
touches `vat_treatment`. Skipped without TENANT_ISOLATION_TESTS_ENABLED=1.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.invoicing.repository import SqlInvoiceRepository
from api.invoicing.rubriek_preview import preview
from api.invoicing.vat import VatGroup
from api.vat.rules import TreatmentRole
from tests.support.seed import SeededTenants


async def test_the_production_reader_places_a_standard_supply_in_1a(
    two_organizations: SeededTenants,
) -> None:
    # `two_organizations` is requested for the suite's shared fixtures (the
    # migrated, reachable database); the lookup itself carries no tenant.
    async with app_engine.begin() as conn:
        rules = await SqlInvoiceRepository(AsyncSession(bind=conn)).effective_rules_on(
            on_date=date(2026, 9, 9)
        )

    result = preview(
        [
            VatGroup(
                treatment="btw_21",
                role=TreatmentRole.STANDARD,
                rate=Decimal("21"),
                taxable=Decimal("100.00"),
                vat=Decimal("21.00"),
            )
        ],
        rules,
    )

    (box,) = result.boxes
    assert box.code == "1a"
    assert box.turnover == Decimal("100.00")
    assert box.vat == Decimal("21.00")
    assert result.unplaced_treatments == ()
    # The box's title comes from the database too, not from the code.
    assert box.description_nl != "1a"

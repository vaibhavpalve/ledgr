"""The seller's own address on an administration (0038), set from Settings.

FR-AR-003 refuses to issue an invoice without the seller's street, postcode and
city (Wet OB art. 35a(1)(e)), but until now nothing in the app could set them, so
the gate's "your business address is missing" could not be fixed. These prove
the update route takes them, `/v1/me` returns them for Settings to show, blank
means missing rather than a stored empty string, and the country stays a valid
code.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import app
from tests.integration.test_sepa_routes import _patch_administration
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

pytestmark = pytest.mark.anyio

ADDRESS = {
    "address_line1": "Keizersgracht 100",
    "postal_code": "1015 AB",
    "city": "Amsterdam",
}


async def _me(tenants: SeededTenants) -> dict[str, object]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(tenants.org_a, user_id=tenants.owner_a)
        response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    entries = response.json()["administrations"]
    return next(e for e in entries if e["id"] == str(tenants.admin_a))  # type: ignore[no-any-return]


async def test_the_address_is_saved_trimmed_and_read_back_from_me(
    two_organizations: SeededTenants,
) -> None:
    saved = await _patch_administration(
        two_organizations,
        {**ADDRESS, "address_line1": "  Keizersgracht 100  ", "city": "Amsterdam "},
    )
    assert saved.status_code == 200
    assert saved.json()["address_line1"] == "Keizersgracht 100"
    assert saved.json()["city"] == "Amsterdam"

    entry = await _me(two_organizations)
    assert entry["address_line1"] == "Keizersgracht 100"
    assert entry["postal_code"] == "1015 AB"
    assert entry["city"] == "Amsterdam"
    assert entry["country"] == "NL"


async def test_a_blank_field_is_cleared_to_missing_not_stored_as_empty(
    two_organizations: SeededTenants,
) -> None:
    await _patch_administration(two_organizations, {**ADDRESS, "address_line2": "Unit 4"})

    cleared = await _patch_administration(two_organizations, {"address_line2": "", "city": "   "})

    assert cleared.status_code == 200
    assert cleared.json()["address_line2"] is None
    # The invoice gate asks whether the field is present; "   " must not pass for it.
    assert cleared.json()["city"] is None
    # Fields not named are left alone.
    assert cleared.json()["address_line1"] == "Keizersgracht 100"


async def test_the_country_is_normalised_and_must_be_a_two_letter_code(
    two_organizations: SeededTenants,
) -> None:
    lower = await _patch_administration(two_organizations, {"country": " de "})
    assert lower.status_code == 200
    assert lower.json()["country"] == "DE"

    bad = await _patch_administration(two_organizations, {"country": "Netherlands"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["field"] == "country"

    # It cannot be cleared: the column is NOT NULL, so empty means "leave it".
    blank = await _patch_administration(two_organizations, {"country": ""})
    assert blank.status_code == 200
    assert blank.json()["country"] == "DE"


async def test_another_organization_cannot_change_this_address(
    two_organizations: SeededTenants,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        token = make_token(two_organizations.org_b, user_id=two_organizations.owner_b)
        response = await client.patch(
            f"/v1/administrations/{two_organizations.admin_a}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
            json=ADDRESS,
        )

    assert response.status_code == 404

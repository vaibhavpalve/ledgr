"""ADR-117 against a real database: throwing a draft away, and everything that
has to stop seeing it once it is.

What only a database can show: the route refuses another tenant's invoice, the
discarded draft leaves the list, stops being "a duplicate of" what was kept, and
stops being counted by the readers that count drafts (reminders, the VAT
return's unbooked purchases). The unit tests cover the rules.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.main import app
from api.reminders.service import _waiting_receipts
from api.vat_returns.repository import SqlVatReturnRepository
from tests.support.capture_helpers import JPEG
from tests.support.capture_helpers import fiscal_year as _fiscal_year
from tests.support.capture_helpers import open_session as _open_session
from tests.support.capture_helpers import upload as _upload
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

DISCARD = "/v1/administrations/{administration_id}/expenses/{expense_id}/discard"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"d-{uuid.uuid4()}"}


async def _discard(
    client: AsyncClient, token: str, administration_id: uuid.UUID, expense_id: object, **body: str
):
    return await client.post(
        f"/v1/administrations/{administration_id}/expenses/{expense_id}/discard",
        headers=_headers(token),
        json=body or None,
    )


@pytest.mark.isolation("POST", DISCARD)
async def test_another_tenants_invoice_cannot_be_discarded(
    two_organizations: SeededTenants,
) -> None:
    """The write that would remove one client's invoice from their list."""
    t = two_organizations
    year_b = await _fiscal_year(t.admin_b, t.org_b)
    token_a = make_token(t.org_a, user_id=t.owner_a)
    token_b = make_token(t.org_b, user_id=t.owner_b)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session_b = await _open_session(client, token_b, t.admin_b)
        theirs = await _upload(client, token_b, t.admin_b, session_b, year_b, JPEG)

        # Org A naming org B's administration, and org B's invoice under A's own.
        across = await _discard(client, token_a, t.admin_b, theirs["expense_id"])
        under_mine = await _discard(client, token_a, t.admin_a, theirs["expense_id"])

    assert across.status_code in (403, 404), across.text
    assert under_mine.status_code in (403, 404), under_mine.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(t.org_b)}
        )
        discarded = await conn.execute(
            text("SELECT discarded_at FROM capture_item WHERE id = :id"),
            {"id": theirs["item_id"]},
        )
    assert discarded.scalar_one() is None, "the other tenant's invoice is untouched"


async def test_a_discarded_invoice_leaves_the_list_and_cannot_be_opened(
    two_organizations: SeededTenants,
) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        kept = await _upload(client, token, a.admin_a, session, year, JPEG)
        thrown = await _upload(client, token, a.admin_a, session, year, JPEG)

        response = await _discard(
            client, token, a.admin_a, thrown["expense_id"], reason="duplicate"
        )
        opened = await client.get(
            f"/v1/administrations/{a.admin_a}/expenses/{thrown['expense_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        listed = await client.get(
            f"/v1/administrations/{a.admin_a}/expenses",
            headers={"Authorization": f"Bearer {token}"},
        )
        again = await _discard(client, token, a.admin_a, thrown["expense_id"])

    assert response.status_code == 200, response.text
    assert response.json() == {"id": thrown["expense_id"], "discarded": True, "reason": "duplicate"}
    assert opened.status_code == 404
    assert [row["id"] for row in listed.json()] == [kept["expense_id"]]
    assert again.status_code == 404, "once discarded it is not there to be discarded again"


async def test_the_original_file_is_kept(two_organizations: SeededTenants) -> None:
    """FR-DOC-002: the document is inside its retention period and is not the
    form's to delete. Discarding records who and why on the capture item."""
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        uploaded = await _upload(client, token, a.admin_a, session, year, JPEG)
        await _discard(client, token, a.admin_a, uploaded["expense_id"], reason="not_an_invoice")

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(a.org_a)}
        )
        document = await conn.execute(
            text("SELECT count(*) FROM document WHERE id = :id"), {"id": uploaded["document_id"]}
        )
        item = await conn.execute(
            text("SELECT discarded_by_user_id, discarded_reason FROM capture_item WHERE id = :id"),
            {"id": uploaded["item_id"]},
        )
    assert document.scalar_one() == 1
    row = item.one()
    assert row.discarded_by_user_id == a.owner_a
    assert row.discarded_reason == "not_an_invoice"


async def test_what_was_kept_stops_being_blocked_by_what_was_thrown_away(
    two_organizations: SeededTenants,
) -> None:
    """The point of the feature. A confirmed duplicate (same invoice number)
    blocks submitting; discarding the copy must lift it, which means a discarded
    claim has to stop counting as a duplicate of anything."""
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)
    auth = {"Authorization": f"Bearer {token}"}
    fields = {
        "supplier": "Mistral AI SAS",
        "expense_date": "2026-09-28",
        "gross_amount": "10.00",
        "invoice_number": "MSTRL-1",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        first = await _upload(client, token, a.admin_a, session, year, JPEG)
        second = await _upload(client, token, a.admin_a, session, year, JPEG + b"2")
        for expense in (first, second):
            patched = await client.patch(
                f"/v1/administrations/{a.admin_a}/expenses/{expense['expense_id']}",
                headers={**auth, "Idempotency-Key": f"u-{uuid.uuid4()}"},
                json=fields,
            )
            assert patched.status_code == 200, patched.text

        before = await client.get(
            f"/v1/administrations/{a.admin_a}/expenses/{first['expense_id']}", headers=auth
        )
        await _discard(client, token, a.admin_a, second["expense_id"], reason="duplicate")
        after = await client.get(
            f"/v1/administrations/{a.admin_a}/expenses/{first['expense_id']}", headers=auth
        )

    assert [w["invoice_number_match"] for w in before.json()["duplicate_warnings"]] == ["same"]
    assert after.json()["duplicate_warnings"] == []


async def test_a_claim_in_front_of_an_approver_is_refused(
    two_organizations: SeededTenants,
) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)
    auth = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        uploaded = await _upload(client, token, a.admin_a, session, year, JPEG)
        path = f"/v1/administrations/{a.admin_a}/expenses/{uploaded['expense_id']}"
        patched = await client.patch(
            path,
            headers={**auth, "Idempotency-Key": f"u-{uuid.uuid4()}"},
            json={
                "supplier": "KPN BV",
                "expense_date": "2026-05-05",
                "gross_amount": "34.44",
                "vat_treatment": "btw_21",
                "category": "Phone & internet",
            },
        )
        assert patched.status_code == 200, patched.text
        ready = await client.post(
            f"{path}/ready", headers={**auth, "Idempotency-Key": f"r-{uuid.uuid4()}"}
        )
        assert ready.status_code == 200, ready.text

        refused = await _discard(client, token, a.admin_a, uploaded["expense_id"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "expense_not_discardable"


async def test_a_discarded_draft_is_not_counted_by_the_readers_that_count_drafts(
    two_organizations: SeededTenants,
) -> None:
    """Reminders nag about waiting receipts and the BTW return warns about
    unbooked purchases. Neither should mention one somebody threw away."""
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        uploaded = await _upload(client, token, a.admin_a, session, year, JPEG)

        async def counts() -> tuple[int, int]:
            async with AsyncSession(app_engine) as db, db.begin():
                await db.execute(
                    text("SELECT set_config('app.current_org_id', :org, true)"),
                    {"org": str(a.org_a)},
                )
                waiting = (await _waiting_receipts(db)).get(a.admin_a, (0, date.today()))[0]
                unbooked = await SqlVatReturnRepository(db).unposted_purchases(
                    administration_id=a.admin_a, start=date(2026, 1, 1), end=date(2026, 12, 31)
                )
            return waiting, unbooked

        before = await counts()
        await _discard(client, token, a.admin_a, uploaded["expense_id"])
        after = await counts()

    assert before == (1, 1)
    assert after == (0, 0)

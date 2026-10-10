"""ADR-116 against a real database: uploading the same invoice twice says so in
the second upload's own response, and never reaches across clients.

The unit tests (tests/expenses/test_upload_duplicate_notice.py) cover the
grading; what only a database can show is the join on `document.content_hash`,
that it stays inside one administration, and that a discarded receipt is not a
duplicate of anything.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.capture_helpers import JPEG, OTHER_JPEG
from tests.support.capture_helpers import fiscal_year as _fiscal_year
from tests.support.capture_helpers import open_session as _open_session
from tests.support.capture_helpers import upload as _upload
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants


async def test_the_second_upload_of_a_file_is_told_it_is_a_duplicate(
    two_organizations: SeededTenants,
) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        first = await _upload(client, token, a.admin_a, session, year, JPEG)
        second = await _upload(client, token, a.admin_a, session, year, JPEG)

    assert first["duplicate"] is None, "the first copy is not a duplicate of anything"
    assert first["expense_id"] is not None

    notice = second["duplicate"]
    assert isinstance(notice, dict)
    assert notice["match"] == "same_file"
    assert notice["expense_id"] == first["expense_id"]
    assert notice["count"] == 1
    assert second["expense_id"] != first["expense_id"], "it is still stored and still a draft"


async def test_a_different_file_is_not_a_duplicate(two_organizations: SeededTenants) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        await _upload(client, token, a.admin_a, session, year, JPEG)
        other = await _upload(client, token, a.admin_a, session, year, OTHER_JPEG)

    assert other["duplicate"] is None


async def test_another_clients_identical_file_is_never_reported(
    two_organizations: SeededTenants,
) -> None:
    """The one that matters. The same bytes uploaded for another organization
    must not make this upload look like a duplicate, and must not leak that the
    other upload exists - not its id, not its supplier."""
    t = two_organizations
    year_a = await _fiscal_year(t.admin_a, t.org_a)
    year_b = await _fiscal_year(t.admin_b, t.org_b)
    token_a = make_token(t.org_a, user_id=t.owner_a)
    token_b = make_token(t.org_b, user_id=t.owner_b)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session_b = await _open_session(client, token_b, t.admin_b)
        theirs = await _upload(client, token_b, t.admin_b, session_b, year_b, JPEG)
        session_a = await _open_session(client, token_a, t.admin_a)
        mine = await _upload(client, token_a, t.admin_a, session_a, year_a, JPEG)

    assert mine["duplicate"] is None
    assert theirs["expense_id"] not in str(mine)


async def test_a_discarded_receipt_is_not_a_duplicate_of_anything(
    two_organizations: SeededTenants,
) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        first = await _upload(client, token, a.admin_a, session, year, JPEG)

        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(a.org_a)},
            )
            await conn.execute(
                text(
                    "UPDATE capture_item SET discarded_at = now(), "
                    "discarded_by_user_id = :user, discarded_reason = 'test' WHERE id = :id"
                ),
                {"user": str(a.owner_a), "id": first["item_id"]},
            )

        again = await _upload(client, token, a.admin_a, session, year, JPEG)

    assert again["duplicate"] is None


async def test_an_opened_invoice_describes_the_file_it_came_from(
    two_organizations: SeededTenants,
) -> None:
    a = two_organizations
    year = await _fiscal_year(a.admin_a, a.org_a)
    token = make_token(a.org_a, user_id=a.owner_a)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _open_session(client, token, a.admin_a)
        uploaded = await _upload(client, token, a.admin_a, session, year, JPEG)
        response = await client.get(
            f"/v1/administrations/{a.admin_a}/expenses/{uploaded['expense_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["original"]["filename"] == "invoice.jpg"
    assert body["original"]["content_type"] == "image/jpeg"
    assert body["original"]["byte_size"] == len(JPEG)
    assert body["original"]["page_count"] == 1
    assert body["original"]["source"] == "upload"
    assert body["created_at"] is not None
    assert body["posted_at"] is None

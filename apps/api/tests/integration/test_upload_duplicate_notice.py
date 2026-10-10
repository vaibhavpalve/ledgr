"""ADR-116 against a real database: uploading the same invoice twice says so in
the second upload's own response, and never reaches across clients.

The unit tests (tests/expenses/test_upload_duplicate_notice.py) cover the
grading; what only a database can show is the join on `document.content_hash`,
that it stays inside one administration, and that a discarded receipt is not a
duplicate of anything.
"""

from __future__ import annotations

import uuid
from datetime import date

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import SeededTenants

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"x" * 96
OTHER_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"y" * 96


async def _fiscal_year(administration_id: uuid.UUID, organization_id: uuid.UUID) -> uuid.UUID:
    """Opens a year AND provisions the administration's encryption key, which
    onboarding does in the same transaction as creating it - a stored original
    cannot be written without one."""
    async with AsyncSession(app_engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        await EnvelopeEncryptionService(
            build_kms(), SqlAdministrationKeyRepository(session)
        ).provision_key(administration_id)
        result = await session.execute(
            text("SELECT (ledger.open_fiscal_year(:a, :s, :e, 'monthly', null)).id"),
            {"a": str(administration_id), "s": date(2026, 1, 1), "e": date(2026, 12, 31)},
        )
        return result.scalar_one()


async def _open_session(client: AsyncClient, token: str, administration_id: uuid.UUID) -> uuid.UUID:
    response = await client.post(
        f"/v1/administrations/{administration_id}/capture-sessions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"s-{uuid.uuid4()}"},
    )
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["id"])


async def _upload(
    client: AsyncClient,
    token: str,
    administration_id: uuid.UUID,
    session_id: uuid.UUID,
    year: uuid.UUID,
    data: bytes,
) -> dict[str, object]:
    response = await client.post(
        f"/v1/administrations/{administration_id}/capture-sessions/{session_id}/pages"
        f"?fiscal_year_id={year}&source=upload&filename=invoice.jpg",
        content=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg",
            "Idempotency-Key": f"p-{uuid.uuid4()}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


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

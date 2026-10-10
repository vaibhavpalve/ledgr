"""Driving the real capture endpoints from an integration test.

Shared by the suites that need a receipt that actually went through
`capture_page` - a stored original, a draft expense, a reading attempt - rather
than a row seeded by hand.
"""

from __future__ import annotations

import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import build_kms
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import engine as app_engine

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"x" * 96
OTHER_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"y" * 96


async def fiscal_year(administration_id: uuid.UUID, organization_id: uuid.UUID) -> uuid.UUID:
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


async def open_session(client: AsyncClient, token: str, administration_id: uuid.UUID) -> uuid.UUID:
    response = await client.post(
        f"/v1/administrations/{administration_id}/capture-sessions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"s-{uuid.uuid4()}"},
    )
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["id"])


async def upload(
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

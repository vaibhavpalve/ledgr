"""IAM-005 isolation tests for the document routes, against a real Postgres.

A source document is the evidence a posting rests on, so a leak here is a leak
of one client's invoices to another firm's screen. These are the tests that
prove RLS holds for the archive; tests/test_isolation_coverage.py only proves
they exist.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.db import engine as app_engine
from api.main import app
from tests.support.isolation import assert_tenant_isolated, make_token
from tests.support.seed import SeededTenants

PDF = b"%PDF-1.7\nBakker Consultancy invoice 2025-042\n" + b"0" * 64


async def _fiscal_year(administration_id: uuid.UUID, organization_id: uuid.UUID) -> uuid.UUID:
    """A calendar 2025 year, opened through the ledger's own function so the
    periods exist too - a document anchors to the year, and a year with no
    periods is not a state anything else in this schema reads.
    """
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text("SELECT (ledger.open_fiscal_year(:admin, :start, :end, 'monthly', null)).id"),
            {
                "admin": str(administration_id),
                "start": date(2025, 1, 1),
                "end": date(2025, 12, 31),
            },
        )
        return result.scalar_one()


async def _seed_document(
    administration_id: uuid.UUID, organization_id: uuid.UUID, *, filename: str
) -> uuid.UUID:
    """A row written directly, because the point of these tests is the READ
    path. Going through the service would also need an encryption key and a
    scanner, which are exercised in tests/documents/.
    """
    fiscal_year_id = await _fiscal_year(administration_id, organization_id)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text(
                """
                INSERT INTO document (
                    organization_id, administration_id, fiscal_year_id,
                    storage_key, content_hash, byte_size, content_type,
                    original_filename, scan_status, scanned_at, scanner,
                    retention_until
                ) VALUES (
                    :org, :admin, :year, :key, :hash, :size, 'application/pdf',
                    :filename, 'clean', now(), 'test', 'epoch'::date
                ) RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "year": str(fiscal_year_id),
                "key": f"{administration_id}/{uuid.uuid4().hex}",
                "hash": bytes(32),
                "size": len(PDF),
                "filename": filename,
            },
        )
        return result.scalar_one()


@pytest.mark.isolation("POST", "/v1/administrations/{administration_id}/documents")
async def test_uploading_into_another_tenants_administration_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """The write side. Org A's owner naming org B's administration must not
    put a document in org B's archive - and must not be able to tell whether
    that administration exists.
    """
    year = await _fiscal_year(two_organizations.admin_b, two_organizations.org_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = make_token(two_organizations.org_a, user_id=two_organizations.owner_a)
        response = await client.post(
            f"/v1/administrations/{two_organizations.admin_b}/documents"
            f"?fiscal_year_id={year}&filename=stolen.pdf",
            content=PDF,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/pdf",
                "Idempotency-Key": f"doc-{uuid.uuid4()}",
            },
        )

    assert response.status_code in (403, 404), response.text

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        count = await conn.execute(
            text("SELECT count(*) FROM document WHERE administration_id = :admin"),
            {"admin": str(two_organizations.admin_b)},
        )
    assert count.scalar_one() == 0, "nothing was written into the other tenant's archive"


@pytest.mark.isolation("GET", "/v1/administrations/{administration_id}/documents/{document_id}")
async def test_another_tenants_document_metadata_is_not_readable(
    two_organizations: SeededTenants,
) -> None:
    foreign = await _seed_document(
        two_organizations.admin_b,
        two_organizations.org_b,
        filename="de-vries-invoice.pdf",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/administrations/{two_organizations.admin_b}/documents/{foreign}",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign, two_organizations.admin_b],
        )

    assert response.status_code in (403, 404)
    # The filename is a client's own words and must not leak either.
    assert "de-vries-invoice" not in response.text


@pytest.mark.isolation(
    "GET", "/v1/administrations/{administration_id}/documents/{document_id}/content"
)
async def test_another_tenants_document_bytes_are_not_downloadable(
    two_organizations: SeededTenants,
) -> None:
    """The one that matters most: the bytes ARE the client's invoice."""
    foreign = await _seed_document(
        two_organizations.admin_b, two_organizations.org_b, filename="bank-statement.pdf"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "GET",
            f"/v1/administrations/{two_organizations.admin_b}/documents/{foreign}/content",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign],
        )

    assert response.status_code in (403, 404)
    assert PDF not in response.content


@pytest.mark.isolation(
    "POST", "/v1/administrations/{administration_id}/documents/{document_id}/erasure-request"
)
async def test_requesting_another_tenants_document_erasure_is_refused(
    two_organizations: SeededTenants,
) -> None:
    """PRIV-022, across a tenant boundary: org A must not be able to restrict
    or erase org B's document, or even learn it exists.
    """
    foreign = await _seed_document(
        two_organizations.admin_b, two_organizations.org_b, filename="theirs.pdf"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await assert_tenant_isolated(
            client,
            "POST",
            f"/v1/administrations/{two_organizations.admin_b}/documents/{foreign}/erasure-request",
            as_org=two_organizations.org_a,
            as_user=two_organizations.owner_a,
            foreign_record_ids=[foreign],
            headers={"Idempotency-Key": f"erasure-{uuid.uuid4()}"},
        )

    assert response.status_code in (403, 404)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_b)},
        )
        status = await conn.execute(
            text("SELECT status FROM document WHERE id = :id"), {"id": str(foreign)}
        )
    assert status.scalar_one() == "active", "the other tenant's document must not be restricted"


async def test_rls_hides_documents_from_the_other_tenant_directly(
    two_organizations: SeededTenants,
) -> None:
    """The data layer on its own, with no HTTP in the way.

    CLAUDE.md rule 1 puts tenant isolation at the data layer as the FIRST line
    of defence, with application checks as the second and never the only one.
    This is that first line: the same query, run under each tenant's context,
    returning different rows.
    """
    mine = await _seed_document(
        two_organizations.admin_a, two_organizations.org_a, filename="mine.pdf"
    )
    theirs = await _seed_document(
        two_organizations.admin_b, two_organizations.org_b, filename="theirs.pdf"
    )

    async def visible(organization_id: uuid.UUID) -> set[uuid.UUID]:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(organization_id)},
            )
            result = await conn.execute(text("SELECT id FROM document"))
            return {row.id for row in result}

    assert mine in await visible(two_organizations.org_a)
    assert theirs not in await visible(two_organizations.org_a)
    assert theirs in await visible(two_organizations.org_b)
    assert mine not in await visible(two_organizations.org_b)

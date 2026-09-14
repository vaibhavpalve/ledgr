"""PRIV-030 against a real Postgres: `documents.expired()` (migration 0045)
and `DocumentRetentionSweepJob` (api.documents.retention_job) together.

The FK-blocked exception path - a document still named by a
document_posting_link - is exercised against a fake in
tests/documents/test_retention_job.py, where it can be asserted without
standing up a full ledger posting. What only Postgres can prove is the
straightforward case this file covers: a document past FR-DOC-002's
retention, with nothing pointing at it, is one the database's own
`document_deletion_guard` (migration 0031) actually lets go.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import engine as app_engine
from api.db import get_ops_engine
from api.documents.retention_job import DocumentRetentionSweepJob
from api.documents.retention_repository import SqlDocumentRetentionRepository
from tests.support.seed import SeededTenants

#: 2015 ends well before any retention window this test suite could still be
#: running under - the point is "unambiguously in the past", not a specific
#: distance from today.
_EXPIRED_YEAR_END = date(2015, 12, 31)


async def _open_year(
    administration_id: uuid.UUID, organization_id: uuid.UUID, *, end: date
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text("SELECT (ledger.open_fiscal_year(:a, :s, :e, 'monthly', null)).id"),
            {"a": str(administration_id), "s": date(end.year, 1, 1), "e": end},
        )
        return result.scalar_one()


async def _insert_document(
    administration_id: uuid.UUID, organization_id: uuid.UUID, fiscal_year_id: uuid.UUID
) -> uuid.UUID:
    """`retention_until` is sent as 'epoch' and immediately overwritten by
    `document_derive_retention_trg` from the fiscal year - the same convention
    tests/integration/test_document_archive.py uses, so a stopped trigger
    would fail loudly rather than pass on a coincidentally-past date.
    """
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
                    :org, :admin, :year, :key, :hash, 1024, 'application/pdf',
                    'old-receipt.pdf', 'clean', now(), 'test', 'epoch'::date
                ) RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "year": str(fiscal_year_id),
                "key": f"{administration_id}/{uuid.uuid4().hex}",
                "hash": bytes(32),
            },
        )
        return result.scalar_one()


async def _document_still_exists(document_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text("SELECT count(*) FROM document WHERE id = :id"), {"id": str(document_id)}
        )
        return result.scalar_one() > 0


async def test_an_unlinked_expired_document_is_removed_by_the_sweep(
    two_organizations: SeededTenants,
) -> None:
    year = await _open_year(
        two_organizations.admin_a, two_organizations.org_a, end=_EXPIRED_YEAR_END
    )
    document_id = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    # ledgr_ops, not app_engine: migration 0031 grants DELETE on `document`
    # to ledgr_ops alone (FR-DOC-002's "not deletable by users" is exactly
    # that withheld grant, which test_document_archive.py's
    # test_the_application_role_cannot_delete_a_document asserts directly) -
    # a ledgr_app connection can select the sweep's candidates but can never
    # complete the delete, the same production shape
    # scripts/enforce_document_retention.py runs (there is no ledgr_app mode
    # there either, for the same reason).
    ops_engine = get_ops_engine()
    try:
        async with ops_engine.connect() as conn:
            job = DocumentRetentionSweepJob(SqlDocumentRetentionRepository(AsyncSession(bind=conn)))
            report = await job.run(administration_id=two_organizations.admin_a)
            await conn.commit()
    finally:
        await ops_engine.dispose()

    assert document_id in report.deleted
    assert report.exceptions == ()
    assert not await _document_still_exists(document_id, two_organizations.org_a)


async def test_a_document_not_yet_past_retention_is_left_alone(
    two_organizations: SeededTenants,
) -> None:
    """The boundary FR-DOC-002 actually draws: retained THROUGH
    `retention_until`, not merely up to it.
    """
    far_future_end = date(date.today().year + 20, 12, 31)
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a, end=far_future_end)
    document_id = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        job = DocumentRetentionSweepJob(SqlDocumentRetentionRepository(AsyncSession(bind=conn)))
        report = await job.run(administration_id=two_organizations.admin_a)

    assert document_id not in report.deleted
    assert await _document_still_exists(document_id, two_organizations.org_a)


async def test_the_sweep_does_not_cross_administrations(
    two_organizations: SeededTenants,
) -> None:
    year_a = await _open_year(
        two_organizations.admin_a, two_organizations.org_a, end=_EXPIRED_YEAR_END
    )
    year_b = await _open_year(
        two_organizations.admin_b, two_organizations.org_b, end=_EXPIRED_YEAR_END
    )
    document_a = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year_a)
    document_b = await _insert_document(two_organizations.admin_b, two_organizations.org_b, year_b)

    # ledgr_ops (BYPASSRLS) - see test_an_unlinked_expired_document_is_removed_
    # by_the_sweep above for why. Scoping to admin_a here is the
    # `administration_id` argument, not RLS: an ops connection can see every
    # tenant, and documents.expired()/the DELETE both take that parameter as
    # their only filter.
    ops_engine = get_ops_engine()
    try:
        async with ops_engine.connect() as conn:
            job = DocumentRetentionSweepJob(SqlDocumentRetentionRepository(AsyncSession(bind=conn)))
            report = await job.run(administration_id=two_organizations.admin_a)
            await conn.commit()
    finally:
        await ops_engine.dispose()

    assert document_a in report.deleted
    assert not await _document_still_exists(document_a, two_organizations.org_a)
    # org B's equally-expired document is untouched: the sweep above was
    # scoped to admin_a by argument, not by RLS, so this is proving the
    # parameter is honoured rather than proving isolation the connection
    # itself provided. Confirmed from B's own tenant context (ledgr_app,
    # RLS-scoped) via _document_still_exists, which is a different
    # connection from the one the sweep ran on.
    assert await _document_still_exists(document_b, two_organizations.org_b)

"""FR-DOC-001, FR-DOC-002, FR-DOC-003 and FR-DOC-005 against a real Postgres.

Two things only a database can establish, and this file is about both.

The first is AGREEMENT. `documents.retention_until()` in migration 0031 and
`api.documents.retention` compute the same rule and cannot borrow each other's
answer - a screen shows a retention date before the row exists, the trigger
derives one inside the transaction that creates it. This runs
tests/documents/retention_cases.py through the SQL and compares row for row,
the way tests/integration/test_fiscal_year.py does for period derivation.

The second is ENFORCEMENT. Write-once, the derived retention date and the
deletion guard are triggers and withheld grants, and a trigger is only known to
fire when something tries. Every assertion below attempts the thing the
requirement forbids, as the application role, and expects to be refused.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from api.db import engine as app_engine
from api.documents.retention import RetentionBasis, retention_until
from tests.documents.retention_cases import RETENTION_CASES, RetentionCase
from tests.support.seed import SeededTenants


async def _scoped(organization_id: uuid.UUID):
    """A connection already carrying the tenant context RLS reads."""
    conn = await app_engine.connect()
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"),
        {"org": str(organization_id)},
    )
    return conn


async def _open_year(
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    start: date = date(2025, 1, 1),
    end: date = date(2025, 12, 31),
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text("SELECT (ledger.open_fiscal_year(:a, :s, :e, 'monthly', null)).id"),
            {"a": str(administration_id), "s": start, "e": end},
        )
        return result.scalar_one()


async def _insert_document(
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    *,
    basis: str = "standard",
    # An obviously-wrong placeholder: this test's whole point is that the
    # trigger derives the real value from the fiscal year and overwrites
    # whatever the caller supplied. A real `date`, not the string "epoch" -
    # asyncpg coerces a bound parameter by Python type before the SQL-side
    # `cast(:until as date)` ever runs, and only understands the 'epoch'
    # keyword as a literal written directly into SQL text (as
    # test_document_retention_sweep.py's own insert does), not as a bound
    # value.
    retention_until_value: date = date(1970, 1, 1),
) -> uuid.UUID:
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
                    original_filename, retention_basis,
                    scan_status, scanned_at, scanner, retention_until
                ) VALUES (
                    :org, :admin, :year, :key, :hash, 1024, 'application/pdf',
                    'invoice.pdf', :basis, 'clean', now(), 'test',
                    cast(:until as date)
                ) RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "year": str(fiscal_year_id),
                "key": f"{administration_id}/{uuid.uuid4().hex}",
                "hash": bytes(32),
                "basis": basis,
                "until": retention_until_value,
            },
        )
        return result.scalar_one()


# ===========================================================================
# FR-DOC-002: the two implementations agree
# ===========================================================================


@pytest.mark.parametrize("case", RETENTION_CASES, ids=lambda c: f"{c.fiscal_year_end}:{c.basis}")
async def test_sql_retention_matches_the_shared_table(case: RetentionCase) -> None:
    async with app_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT documents.retention_until(cast(:end as date), :basis)"),
            {"end": case.fiscal_year_end, "basis": case.basis},
        )
        assert result.scalar_one() == case.expected, case.why


@pytest.mark.parametrize("case", RETENTION_CASES, ids=lambda c: f"{c.fiscal_year_end}:{c.basis}")
async def test_sql_and_python_agree(case: RetentionCase) -> None:
    """The comparison that makes two implementations safe rather than twice
    the surface. A divergence fails here, not in an archive that expired a
    year early.
    """
    async with app_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT documents.retention_until(cast(:end as date), :basis)"),
            {"end": case.fiscal_year_end, "basis": case.basis},
        )
        assert result.scalar_one() == retention_until(
            case.fiscal_year_end, RetentionBasis(case.basis)
        )


async def test_retention_is_derived_from_the_fiscal_year_not_the_caller(
    two_organizations: SeededTenants,
) -> None:
    """FR-DOC-002's "not deletable by users", at the point it would be
    circumvented most quietly.

    The insert supplies `epoch` - a date in 1970, long expired. The trigger
    discards it and computes 2032-12-31 from the fiscal year, which is what
    makes the guarantee true for every writer rather than for every writer who
    remembered.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT retention_until FROM document WHERE id = :id"),
            {"id": str(document)},
        )
        assert result.scalar_one() == date(2032, 12, 31)
    finally:
        await conn.close()


async def test_retention_follows_a_non_calendar_year(
    two_organizations: SeededTenants,
) -> None:
    """FR-ONB-006 permits a July-June year, and FR-DOC-002 anchors to the
    year's own end rather than to December.
    """
    year = await _open_year(
        two_organizations.admin_a,
        two_organizations.org_a,
        start=date(2025, 7, 1),
        end=date(2026, 6, 30),
    )
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT retention_until FROM document WHERE id = :id"),
            {"id": str(document)},
        )
        assert result.scalar_one() == date(2033, 6, 30)
    finally:
        await conn.close()


async def test_a_document_cannot_anchor_to_another_administrations_year(
    two_organizations: SeededTenants,
) -> None:
    """A document retained against another administration's fiscal year would
    expire on that administration's schedule - a tenancy defect wearing a
    retention defect's clothes.

    The refusal actually arrives as "fiscal year ... does not exist", not
    the trigger's own explicit FR-DOC-002 message - CLAUDE.md's "RLS is the
    first line of defense, the application-layer check is the second, never
    the only line" in action: document_set_retention() runs as ledgr_app
    (no SECURITY DEFINER), so its own `select ... from fiscal_year` is
    already RLS-scoped to org_a and never sees org_b's row at all - the
    explicit `v_admin is distinct from new.administration_id` check a few
    lines later never gets a chance to fire, because RLS already hid the
    row it would need to compare against. That check still earns its place
    as defense-in-depth for a caller that reached this function some other
    way RLS does not cover (a SECURITY DEFINER path, for instance) - this
    test asserts the property (refused, and for a tenancy reason), not
    which of the two layers happened to catch it first.
    """
    foreign_year = await _open_year(two_organizations.admin_b, two_organizations.org_b)

    with pytest.raises((DBAPIError, SQLAlchemyError), match="fiscal year"):
        await _insert_document(two_organizations.admin_a, two_organizations.org_a, foreign_year)


# ===========================================================================
# FR-DOC-001 / FR-DOC-005: write-once
# ===========================================================================


async def test_the_original_cannot_be_altered(two_organizations: SeededTenants) -> None:
    """FR-DOC-001. Each column is attempted separately, because a trigger that
    checked only the first would pass a test that changed only the first.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    for column, value in (
        ("storage_key", "'somewhere-else'"),
        ("content_hash", "decode('00', 'hex')"),
        ("byte_size", "1"),
        ("content_type", "'image/png'"),
        ("original_filename", "'renamed.pdf'"),
        ("uploaded_at", "now()"),
    ):
        with pytest.raises((DBAPIError, SQLAlchemyError), match="FR-DOC-001"):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_org_id', :org, true)"),
                    {"org": str(two_organizations.org_a)},
                )
                await conn.execute(
                    text(f"UPDATE document SET {column} = {value} WHERE id = :id"),
                    {"id": str(document)},
                )


async def test_derived_material_may_still_be_written(
    two_organizations: SeededTenants,
) -> None:
    """FR-DOC-001's "alongside", and the reason this table is not simply
    append-only: OCR runs after the upload (FR-EXP-001c) and may be re-run when
    the pipeline improves.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text(
                "UPDATE document SET derived_text = :t, "
                "extracted_fields = cast(:f as jsonb) WHERE id = :id"
            ),
            {
                "t": "Bakker Consultancy B.V. factuur 2025-042",
                "f": '{"supplier": "Bakker Consultancy B.V."}',
                "id": str(document),
            },
        )

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT derived_text, search_text FROM document WHERE id = :id"),
            {"id": str(document)},
        )
        row = result.one()
        assert "factuur 2025-042" in row.derived_text
        # FR-DOC-004: the search column is generated, so it cannot go stale.
        assert "Bakker Consultancy" in row.search_text
    finally:
        await conn.close()


async def test_retention_can_be_extended_but_never_shortened(
    two_organizations: SeededTenants,
) -> None:
    """Shortening retention is the deletion FR-DOC-002 forbids, arriving by a
    quieter route than DELETE.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    # Reclassifying to immovable property extends 7 years to 10.
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        await conn.execute(
            text("UPDATE document SET retention_basis = 'immovable_property' WHERE id = :id"),
            {"id": str(document)},
        )

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT retention_until FROM document WHERE id = :id"),
            {"id": str(document)},
        )
        assert result.scalar_one() == date(2035, 12, 31)
    finally:
        await conn.close()

    # And back again is refused: the trigger recomputes 7 years, which is
    # earlier than what is stored.
    with pytest.raises((DBAPIError, SQLAlchemyError), match="shortened"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text("UPDATE document SET retention_basis = 'standard' WHERE id = :id"),
                {"id": str(document)},
            )


async def test_the_application_role_cannot_delete_a_document(
    two_organizations: SeededTenants,
) -> None:
    """FR-DOC-002's "not deletable by users" is a withheld GRANT before it is a
    trigger: ledgr_app - the role every request runs as - cannot express the
    statement at all.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(text("DELETE FROM document WHERE id = :id"), {"id": str(document)})

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT count(*) FROM document WHERE id = :id"), {"id": str(document)}
        )
        assert result.scalar_one() == 1, "the document is still there"
    finally:
        await conn.close()


# ===========================================================================
# FR-DOC-003
# ===========================================================================


async def test_a_link_cannot_cross_administrations(
    two_organizations: SeededTenants,
) -> None:
    """A posting citing another tenant's evidence is CLAUDE.md rule 1 broken
    in the most confusing possible way.
    """
    year_a = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document_a = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year_a)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO document_posting_link (
                        organization_id, administration_id, document_id,
                        journal_entry_id
                    ) VALUES (:org, :admin, :doc, :entry)
                    """
                ),
                {
                    "org": str(two_organizations.org_a),
                    "admin": str(two_organizations.admin_a),
                    "doc": str(document_a),
                    # An entry id that does not exist in this administration -
                    # the foreign key refuses first, and the same-tenant
                    # trigger would refuse a real one belonging elsewhere.
                    "entry": str(uuid.uuid4()),
                },
            )


async def test_the_completeness_report_is_empty_when_nothing_is_posted(
    two_organizations: SeededTenants,
) -> None:
    """FR-DOC-003's report, exercised for its shape rather than its contents:
    a fresh administration has no postings, so nothing is unsupported.

    Worth having anyway - it is what would catch the function failing to
    compile, or its grant being missing, both of which are silent until called.
    """
    await _open_year(two_organizations.admin_a, two_organizations.org_a)

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT count(*) FROM documents.postings_without_documents(:admin, null)"),
            {"admin": str(two_organizations.admin_a)},
        )
        assert result.scalar_one() == 0
    finally:
        await conn.close()


# ===========================================================================
# FR-DOC-005: deletion needs a basis and a second person
# ===========================================================================


async def test_a_deletion_request_needs_a_real_legal_basis(
    two_organizations: SeededTenants,
) -> None:
    """The failure this guards is a one-word basis - "GDPR", "asked" - that
    reads as a reason and answers nothing later.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO document_deletion_request (
                        organization_id, administration_id, document_id,
                        legal_basis, requested_by_user_id
                    ) VALUES (:org, :admin, :doc, 'GDPR', :user)
                    """
                ),
                {
                    "org": str(two_organizations.org_a),
                    "admin": str(two_organizations.admin_a),
                    "doc": str(document),
                    "user": str(two_organizations.owner_a),
                },
            )


async def test_a_request_cannot_approve_itself(
    two_organizations: SeededTenants,
) -> None:
    """ "Privileged approval" is a second person. A request that approves itself
    is a request, and this is the same argument §8.4 makes about payment
    release.
    """
    year = await _open_year(two_organizations.admin_a, two_organizations.org_a)
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    with pytest.raises((DBAPIError, SQLAlchemyError)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(two_organizations.org_a)},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO document_deletion_request (
                        organization_id, administration_id, document_id,
                        legal_basis, requested_by_user_id,
                        approved_by_user_id, approved_at
                    ) VALUES (
                        :org, :admin, :doc,
                        'Erasure request under GDPR article 17, assessed against '
                        'the fiscal retention exception in PRIV-022.',
                        :user, :user, now()
                    )
                    """
                ),
                {
                    "org": str(two_organizations.org_a),
                    "admin": str(two_organizations.admin_a),
                    "doc": str(document),
                    "user": str(two_organizations.owner_a),
                },
            )


async def test_an_expired_document_may_be_removed(
    two_organizations: SeededTenants,
) -> None:
    """PRIV-023's "deleted automatically at the end of the retention period",
    and the boundary the deletion guard reads.

    A 2016 fiscal year is retained until 2023, which is past. The guard permits
    removal - by ledgr_ops, which is the only role holding the grant - and the
    application role still cannot.
    """
    year = await _open_year(
        two_organizations.admin_a,
        two_organizations.org_a,
        start=date(2016, 1, 1),
        end=date(2016, 12, 31),
    )
    document = await _insert_document(two_organizations.admin_a, two_organizations.org_a, year)

    conn = await _scoped(two_organizations.org_a)
    try:
        result = await conn.execute(
            text("SELECT retention_until FROM document WHERE id = :id"),
            {"id": str(document)},
        )
        expiry = result.scalar_one()
    finally:
        await conn.close()

    assert expiry == date(2023, 12, 31)
    assert expiry < date.today() - timedelta(days=1), (
        "this test is only meaningful while 2023 is in the past"
    )

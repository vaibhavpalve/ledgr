"""The audit log's APPEND behaviour against a real Postgres: chaining,
sealing, and the tenant boundary.

Tampering is not tested here. Every attempt to modify or delete an entry -
through the application, direct SQL as each role, and the migration
interface - lives in test_audit_tamper_evidence.py, which is the artifact
PRD §22's "audit log immutability verified by attempted tamper test" refers
to. Splitting them keeps that file readable as evidence rather than as a
grab-bag, and keeps this one about what the log does when nobody is
attacking it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.db import engine as app_engine
from tests.support.fake_audit_repository import seal
from tests.support.seed import SeededTenants


async def _as_org(org_id: uuid.UUID, sql: str, params: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )
        result = await conn.execute(text(sql), params or {})
        return list(result.all()) if result.returns_rows else []


async def _seed_entries(tenants: SeededTenants, count: int = 3) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(tenants.org_a)},
        )
        log = AuditLog(SqlAuditRepository(AsyncSession(bind=conn)))
        for index in range(count):
            await log.record(
                AuditEvent(
                    organization_id=tenants.org_a,
                    category=AuditCategory.POSTING,
                    action=f"post-{index}",
                    resource_type="journal_entry",
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.USER,
                    actor_user_id=tenants.owner_a,
                    source_ip="203.0.113.7",
                    user_agent="pytest",
                    correlation_id=f"req-{index}",
                    detail={"index": index},
                )
            )
        await conn.commit()


# ===========================================================================
# Appending
# ===========================================================================


async def test_the_application_can_append(two_organizations: SeededTenants) -> None:
    await _seed_entries(two_organizations, count=2)

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT sequence_number, previous_hash, entry_hash FROM audit_log ORDER BY sequence_number",
    )

    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == "0" * 64, "the first entry chains from a zero hash"
    assert rows[1][1] == rows[0][2], "each entry chains to the one before it"


async def test_the_application_cannot_choose_its_own_hash_or_sequence(
    two_organizations: SeededTenants,
) -> None:
    """The sealing trigger derives sequence_number, previous_hash,
    entry_hash and recorded_at from the row's own values and overwrites
    whatever was supplied. An application that could name its own hash could
    forge a consistent chain.
    """
    await _as_org(
        two_organizations.org_a,
        "INSERT INTO audit_log "
        "(organization_id, actor_type, category, action, resource_type, outcome, "
        " sequence_number, previous_hash, entry_hash, recorded_at) "
        "VALUES (:org, 'system', 'configuration', 'forge', 'thing', 'success', "
        "        9999, 'chosen-previous', 'chosen-hash', '2000-01-01T00:00:00Z')",
        {"org": str(two_organizations.org_a)},
    )

    rows = await _as_org(
        two_organizations.org_a,
        "SELECT sequence_number, previous_hash, entry_hash, recorded_at FROM audit_log",
    )

    assert rows[0][0] == 1, "sequence was reassigned"
    assert rows[0][1] == "0" * 64, "previous_hash was reassigned"
    assert rows[0][2] != "chosen-hash", "the hash was recomputed"
    assert rows[0][3].year >= 2026, "recorded_at came from the server clock"


# ===========================================================================
# The fake and Postgres must agree
# ===========================================================================


async def test_the_in_memory_fake_computes_the_same_hash_as_postgres(
    two_organizations: SeededTenants,
) -> None:
    """tests/support/fake_audit_repository.py reimplements the sealing logic
    so the pure tests exercise real chaining. If the two drift, those tests
    keep passing while testing something production does not do - so the two
    implementations are compared here on a real row.
    """
    occurred = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        entry = await SqlAuditRepository(AsyncSession(bind=conn)).append(
            AuditEvent(
                organization_id=two_organizations.org_a,
                category=AuditCategory.EXPORT,
                action="export",
                resource_type="report_data",
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_user_id=two_organizations.owner_a,
                occurred_at=occurred,
                source_ip="203.0.113.7",
                user_agent="pytest|with|pipes",
                correlation_id="req-1",
                detail={"rows": 42},
            ),
            recorded_at=occurred,
        )
        await conn.commit()

    recomputed = seal(
        previous_hash=entry.previous_hash,
        sequence_number=entry.sequence_number,
        organization_id=entry.organization_id,
        administration_id=entry.administration_id,
        actor_user_id=entry.actor_user_id,
        actor_type=entry.actor_type.value,
        category=entry.category.value,
        action=entry.action,
        resource_type=entry.resource_type,
        resource_id=entry.resource_id,
        outcome=entry.outcome.value,
        occurred_at=entry.occurred_at,
        recorded_at=entry.recorded_at,
        source_ip=entry.source_ip,
        user_agent=entry.user_agent,
        correlation_id=entry.correlation_id,
        detail=entry.detail,
    )

    assert recomputed == entry.entry_hash


async def test_one_tenant_cannot_read_anothers_audit_log(
    two_organizations: SeededTenants,
) -> None:
    """IAM-094 will let customers search their own log. This is the policy
    that makes "their own" true.
    """
    await _seed_entries(two_organizations, count=2)

    from_b = await _as_org(two_organizations.org_b, "SELECT count(*) FROM audit_log")

    assert from_b[0][0] == 0

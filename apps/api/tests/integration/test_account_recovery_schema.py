"""DB-backed proof that account_recovery_event (migrations/0007_account_recovery.sql)
holds: the FK to users(id) on both target and actor, and the check
constraint requiring an admin_reset to name a performing actor.
Complements tests/auth/test_account_recovery.py, which proves the
recovery LOGIC in isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.account_recovery_repository import SqlAccountRecoveryEventRepository
from api.auth.repository import SqlUserRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_self_service_event_round_trip() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"recovery-{uuid.uuid4()}@example.com")
        log = SqlAccountRecoveryEventRepository(session)

        recorded = await log.record(
            target_user_id=user.id, method="totp", performed_by_user_id=None, reason=None
        )

        events = await log.list_for_user(user.id)
        assert len(events) == 1
        assert events[0].id == recorded.id
        assert events[0].performed_by_user_id is None


async def test_admin_reset_without_an_actor_is_rejected_by_the_database() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"recovery-noactor-{uuid.uuid4()}@example.com")

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            log = SqlAccountRecoveryEventRepository(session)
            await log.record(
                target_user_id=user.id,
                method="admin_reset",
                performed_by_user_id=None,
                reason="missing actor - must be rejected",
            )


async def test_admin_reset_with_an_actor_is_recorded() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        target = await users.create(f"recovery-target-{uuid.uuid4()}@example.com")
        admin = await users.create(f"recovery-admin-{uuid.uuid4()}@example.com")
        log = SqlAccountRecoveryEventRepository(session)

        recorded = await log.record(
            target_user_id=target.id,
            method="admin_reset",
            performed_by_user_id=admin.id,
            reason="support ticket #456",
        )

        assert recorded.performed_by_user_id == admin.id
        assert recorded.reason == "support ticket #456"


async def test_logging_for_a_nonexistent_target_is_rejected_by_the_foreign_key() -> None:
    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            log = SqlAccountRecoveryEventRepository(session)
            await log.record(
                target_user_id=uuid.uuid4(), method="totp", performed_by_user_id=None, reason=None
            )

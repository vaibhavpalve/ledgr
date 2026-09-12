"""DB-backed proof that the real schema (migrations/0003_authentication.sql)
holds: unique email, unique token_hash, the password-credential upsert, and
the deferred foreign keys from 0001/0002 now genuinely reference users(id).
Complements tests/auth/, which proves the service-layer LOGIC in isolation
from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.repository import SqlSessionRepository, SqlUserRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_duplicate_email_is_rejected_by_the_database() -> None:
    email = f"dup-{uuid.uuid4()}@example.com"
    async with _session_factory() as session, session.begin():
        repository = SqlUserRepository(session)
        await repository.create(email)

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            repository = SqlUserRepository(session)
            await repository.create(email)


async def test_email_uniqueness_is_case_insensitive() -> None:
    """users.email is `citext`, not `text` - "A@b.com" and "a@b.com" must
    collide at the database layer, not only via application-side
    lowercasing (which api.auth.service also does, but this proves the
    data-layer backstop independently).
    """
    base = uuid.uuid4().hex
    async with _session_factory() as session, session.begin():
        repository = SqlUserRepository(session)
        await repository.create(f"Case-{base}@Example.com")

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            repository = SqlUserRepository(session)
            await repository.create(f"case-{base}@example.com")


async def test_password_credential_upsert_replaces_the_hash() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"credential-{uuid.uuid4()}@example.com")
        await users.upsert_password_credential(user.id, password_hash="hash-one")
        await users.upsert_password_credential(user.id, password_hash="hash-two")

        credential = await users.get_password_credential(user.id)
        assert credential is not None
        assert credential.password_hash == "hash-two"


async def test_session_token_hash_must_be_unique() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"session-{uuid.uuid4()}@example.com")

    now = datetime.now(UTC)
    # Unique per run. The hash was a fixed string, which made the FIRST insert
    # collide with a leftover row whenever the suite ran twice against the same
    # database - failing the test in setup, before it reached the collision it
    # is actually about.
    token_hash = f"duplicate-{uuid.uuid4()}"

    async with _session_factory() as session, session.begin():
        sessions = SqlSessionRepository(session)
        await sessions.create(
            user_id=user.id,
            token_hash=token_hash,
            privileged=True,
            created_at=now,
            expires_at=now + timedelta(hours=12),
            mfa_verified_at=None,
            ip_address=None,
            user_agent=None,
        )

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            sessions = SqlSessionRepository(session)
            await sessions.create(
                user_id=user.id,
                token_hash=token_hash,
                privileged=True,
                created_at=now,
                expires_at=now + timedelta(hours=12),
                mfa_verified_at=None,
                ip_address=None,
                user_agent=None,
            )


async def test_session_mfa_verification_persists() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"session-mfa-{uuid.uuid4()}@example.com")
        sessions = SqlSessionRepository(session)
        now = datetime.now(UTC)
        created = await sessions.create(
            user_id=user.id,
            token_hash=f"hash-{uuid.uuid4()}",
            privileged=True,
            created_at=now,
            expires_at=now + timedelta(hours=12),
            mfa_verified_at=None,
            ip_address=None,
            user_agent=None,
        )

        fetched = await sessions.get_by_token_hash(created.token_hash)
        assert fetched is not None
        assert fetched.mfa_verified_at is None

        await sessions.record_mfa_verification(created.id, at=now)

        reloaded = await sessions.get_by_token_hash(created.token_hash)
        assert reloaded is not None
        assert reloaded.mfa_verified_at is not None


async def test_deferred_foreign_keys_now_reference_real_users() -> None:
    """0001 and 0002 left firm_engagement.*_user_id and
    administration_encryption_key.revoked_by_user_id as bare uuid columns,
    commented "FK to users table, added when users ships." 0003 adds those
    constraints - this proves they actually reject a nonexistent user id.
    """
    async with _session_factory() as session, session.begin():
        result = await session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname IN ("
                "  'firm_engagement_invited_by_user_id_fkey',"
                "  'firm_engagement_accepted_by_user_id_fkey',"
                "  'firm_engagement_revoked_by_user_id_fkey',"
                "  'period_locked_by_user_id_fkey',"
                "  'administration_encryption_key_revoked_by_user_id_fkey'"
                ")"
            )
        )
        found = {row.conname for row in result}

    assert found == {
        "firm_engagement_invited_by_user_id_fkey",
        "firm_engagement_accepted_by_user_id_fkey",
        "firm_engagement_revoked_by_user_id_fkey",
        "period_locked_by_user_id_fkey",
        "administration_encryption_key_revoked_by_user_id_fkey",
    }

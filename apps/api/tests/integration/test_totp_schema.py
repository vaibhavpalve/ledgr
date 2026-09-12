"""DB-backed proof that user_totp_credential (migrations/0006_mfa.sql)
holds: the partial unique index (one ACTIVE credential per user, but
re-enrollment after revocation succeeds), and the FK to users(id).
Complements tests/auth/test_totp.py, which proves the TOTP LOGIC in
isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.repository import SqlUserRepository
from api.auth.totp_repository import SqlTotpRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_create_then_lookup_round_trip() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"totp-{uuid.uuid4()}@example.com")

        totp = SqlTotpRepository(session)
        created = await totp.create(
            user_id=user.id,
            wrapped_secret=os.urandom(48),
            secret_nonce=os.urandom(12),
            wrapped_dek=os.urandom(256),
            wrap_algorithm="A256KW",
            kek_key_id="test-kek-v1",
            confirmed_at=datetime.now(UTC),
            last_used_step=100,
        )

        found = await totp.get_for_user(user.id)
        assert found is not None
        assert found.id == created.id
        assert found.is_active


async def test_a_second_active_credential_for_the_same_user_is_rejected() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"totp-dup-{uuid.uuid4()}@example.com")
        totp = SqlTotpRepository(session)
        await totp.create(
            user_id=user.id,
            wrapped_secret=os.urandom(48),
            secret_nonce=os.urandom(12),
            wrapped_dek=os.urandom(256),
            wrap_algorithm="A256KW",
            kek_key_id="test-kek-v1",
            confirmed_at=datetime.now(UTC),
            last_used_step=1,
        )

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            totp = SqlTotpRepository(session)
            await totp.create(
                user_id=user.id,
                wrapped_secret=os.urandom(48),
                secret_nonce=os.urandom(12),
                wrapped_dek=os.urandom(256),
                wrap_algorithm="A256KW",
                kek_key_id="test-kek-v1",
                confirmed_at=datetime.now(UTC),
                last_used_step=1,
            )


async def test_reenrollment_after_revocation_is_allowed_by_the_schema() -> None:
    """The partial unique index (WHERE revoked_at IS NULL) is what makes
    this succeed where a plain UNIQUE(user_id) would not.
    """
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"totp-reenroll-{uuid.uuid4()}@example.com")
        totp = SqlTotpRepository(session)
        first = await totp.create(
            user_id=user.id,
            wrapped_secret=os.urandom(48),
            secret_nonce=os.urandom(12),
            wrapped_dek=os.urandom(256),
            wrap_algorithm="A256KW",
            kek_key_id="test-kek-v1",
            confirmed_at=datetime.now(UTC),
            last_used_step=1,
        )
        await totp.revoke(first.id, at=datetime.now(UTC))

        second = await totp.create(
            user_id=user.id,
            wrapped_secret=os.urandom(48),
            secret_nonce=os.urandom(12),
            wrapped_dek=os.urandom(256),
            wrap_algorithm="A256KW",
            kek_key_id="test-kek-v1",
            confirmed_at=datetime.now(UTC),
            last_used_step=1,
        )
        assert second.id != first.id
        assert second.is_active


async def test_enrolling_for_a_nonexistent_user_is_rejected_by_the_foreign_key() -> None:
    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            totp = SqlTotpRepository(session)
            await totp.create(
                user_id=uuid.uuid4(),
                wrapped_secret=os.urandom(48),
                secret_nonce=os.urandom(12),
                wrapped_dek=os.urandom(256),
                wrap_algorithm="A256KW",
                kek_key_id="test-kek-v1",
                confirmed_at=datetime.now(UTC),
                last_used_step=1,
            )

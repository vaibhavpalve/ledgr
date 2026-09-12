"""DB-backed proof that user_passkey (migrations/0005_webauthn_passkeys.sql)
holds: unique credential_id, the FK to users(id), and the blank-name check
constraint. Complements tests/auth/test_passkeys.py, which proves the
WebAuthn ceremony LOGIC in isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.repository import SqlUserRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_create_then_lookup_round_trip() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"passkey-{uuid.uuid4()}@example.com")

        passkeys = SqlPasskeyRepository(session)
        created = await passkeys.create(
            user_id=user.id,
            name="Test Device",
            credential_id=os.urandom(32),
            public_key=os.urandom(77),
            sign_count=0,
            transports=["internal", "hybrid"],
            aaguid="00000000-0000-0000-0000-000000000000",
            backup_eligible=True,
            backed_up=False,
            authenticator_attachment="platform",
        )

        found = await passkeys.get_by_credential_id(created.credential_id)
        assert found is not None
        assert found.id == created.id
        assert found.transports == ["internal", "hybrid"]
        assert found.authenticator_attachment == "platform"


async def test_the_same_credential_id_cannot_be_registered_twice() -> None:
    credential_id = os.urandom(32)
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user_a = await users.create(f"passkey-a-{uuid.uuid4()}@example.com")
        user_b = await users.create(f"passkey-b-{uuid.uuid4()}@example.com")
        passkeys = SqlPasskeyRepository(session)
        await passkeys.create(
            user_id=user_a.id,
            name="Device",
            credential_id=credential_id,
            public_key=os.urandom(77),
            sign_count=0,
            transports=[],
            aaguid=None,
            backup_eligible=False,
            backed_up=False,
            authenticator_attachment=None,
        )

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            passkeys = SqlPasskeyRepository(session)
            await passkeys.create(
                user_id=user_b.id,
                name="Device",
                credential_id=credential_id,
                public_key=os.urandom(77),
                sign_count=0,
                transports=[],
                aaguid=None,
                backup_eligible=False,
                backed_up=False,
                authenticator_attachment=None,
            )


async def test_a_blank_name_is_rejected_by_the_database() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"passkey-blank-{uuid.uuid4()}@example.com")

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            passkeys = SqlPasskeyRepository(session)
            await passkeys.create(
                user_id=user.id,
                name="   ",
                credential_id=os.urandom(32),
                public_key=os.urandom(77),
                sign_count=0,
                transports=[],
                aaguid=None,
                backup_eligible=False,
                backed_up=False,
                authenticator_attachment=None,
            )


async def test_registering_for_a_nonexistent_user_is_rejected_by_the_foreign_key() -> None:
    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            passkeys = SqlPasskeyRepository(session)
            await passkeys.create(
                user_id=uuid.uuid4(),
                name="Orphan Device",
                credential_id=os.urandom(32),
                public_key=os.urandom(77),
                sign_count=0,
                transports=[],
                aaguid=None,
                backup_eligible=False,
                backed_up=False,
                authenticator_attachment=None,
            )


async def test_revoke_persists_and_only_affects_the_targeted_passkey() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"passkey-revoke-{uuid.uuid4()}@example.com")
        passkeys = SqlPasskeyRepository(session)
        keep = await passkeys.create(
            user_id=user.id,
            name="Keep",
            credential_id=os.urandom(32),
            public_key=os.urandom(77),
            sign_count=0,
            transports=[],
            aaguid=None,
            backup_eligible=False,
            backed_up=False,
            authenticator_attachment=None,
        )
        revoke_me = await passkeys.create(
            user_id=user.id,
            name="Revoke",
            credential_id=os.urandom(32),
            public_key=os.urandom(77),
            sign_count=0,
            transports=[],
            aaguid=None,
            backup_eligible=False,
            backed_up=False,
            authenticator_attachment=None,
        )

        await passkeys.revoke(revoke_me.id, at=datetime.now(UTC))

        all_passkeys = await passkeys.list_for_user(user.id)
        by_id = {p.id: p for p in all_passkeys}
        assert by_id[revoke_me.id].is_revoked
        assert not by_id[keep.id].is_revoked

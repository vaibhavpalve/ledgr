"""DB-backed proof that user_google_identity (migrations/0004_google_identity.sql)
holds: unique google_subject, unique user_id (at most one Google identity
per user), and the FK to users(id). Complements tests/auth/test_google_*.py,
which prove the OIDC and resolution LOGIC in isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.google_oidc import GoogleIdentity
from api.auth.google_repository import SqlGoogleIdentityRepository
from api.auth.repository import SqlUserRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_link_then_lookup_round_trip() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"google-{uuid.uuid4()}@example.com")

        google_identities = SqlGoogleIdentityRepository(session)
        identity = GoogleIdentity(
            subject=f"subject-{uuid.uuid4()}", email=user.email, name="Test", picture=None
        )
        await google_identities.link(user.id, identity)

        found_user_id = await google_identities.get_user_id_by_subject(identity.subject)
        assert found_user_id == user.id


async def test_the_same_google_subject_cannot_be_linked_twice() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user_a = await users.create(f"google-a-{uuid.uuid4()}@example.com")
        user_b = await users.create(f"google-b-{uuid.uuid4()}@example.com")
        google_identities = SqlGoogleIdentityRepository(session)
        subject = f"subject-{uuid.uuid4()}"
        await google_identities.link(
            user_a.id, GoogleIdentity(subject=subject, email=user_a.email, name=None, picture=None)
        )

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            google_identities = SqlGoogleIdentityRepository(session)
            await google_identities.link(
                user_b.id,
                GoogleIdentity(subject=subject, email=user_b.email, name=None, picture=None),
            )


async def test_a_user_can_have_at_most_one_google_identity() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"google-onlyone-{uuid.uuid4()}@example.com")
        google_identities = SqlGoogleIdentityRepository(session)
        await google_identities.link(
            user.id,
            GoogleIdentity(
                subject=f"first-{uuid.uuid4()}", email=user.email, name=None, picture=None
            ),
        )

    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            google_identities = SqlGoogleIdentityRepository(session)
            await google_identities.link(
                user.id,
                GoogleIdentity(
                    subject=f"second-{uuid.uuid4()}", email=user.email, name=None, picture=None
                ),
            )


async def test_linking_a_nonexistent_user_is_rejected_by_the_foreign_key() -> None:
    with pytest.raises(IntegrityError):
        async with _session_factory() as session, session.begin():
            google_identities = SqlGoogleIdentityRepository(session)
            await google_identities.link(
                uuid.uuid4(),
                GoogleIdentity(
                    subject=f"orphan-{uuid.uuid4()}",
                    email="nobody@example.com",
                    name=None,
                    picture=None,
                ),
            )

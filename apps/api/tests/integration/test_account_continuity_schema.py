"""DB-backed proof that GoogleIdentityRepository.exists_for_user
(api/auth/google_repository.py) and SignInMethodChecker
(api/auth/account_continuity.py) work against the real schema.
Complements tests/auth/test_account_continuity.py, which proves the LOGIC
in isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from api.auth.account_continuity import SignInMethodChecker, ensure_can_post_to_ledger
from api.auth.google_oidc import GoogleIdentity
from api.auth.google_repository import SqlGoogleIdentityRepository
from api.auth.passkeys_repository import SqlPasskeyRepository
from api.auth.repository import SqlUserRepository
from api.db import engine as app_engine

_session_factory = async_sessionmaker(app_engine, expire_on_commit=False)


async def test_exists_for_user_reflects_a_real_link() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"continuity-{uuid.uuid4()}@example.com")
        google = SqlGoogleIdentityRepository(session)

        assert await google.exists_for_user(user.id) is False

        await google.link(
            user.id,
            GoogleIdentity(
                subject=f"sub-{uuid.uuid4()}", email=user.email, name=None, picture=None
            ),
        )

        assert await google.exists_for_user(user.id) is True


async def test_google_only_account_is_flagged_against_the_real_schema() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"continuity-google-only-{uuid.uuid4()}@example.com")
        google = SqlGoogleIdentityRepository(session)
        await google.link(
            user.id,
            GoogleIdentity(
                subject=f"sub-{uuid.uuid4()}", email=user.email, name=None, picture=None
            ),
        )

        checker = SignInMethodChecker(users, google, SqlPasskeyRepository(session))
        status = await checker.check(user.id)
        assert status.is_google_only is True


async def test_adding_a_password_lifts_the_flag_against_the_real_schema() -> None:
    async with _session_factory() as session, session.begin():
        users = SqlUserRepository(session)
        user = await users.create(f"continuity-recovers-{uuid.uuid4()}@example.com")
        google = SqlGoogleIdentityRepository(session)
        await google.link(
            user.id,
            GoogleIdentity(
                subject=f"sub-{uuid.uuid4()}", email=user.email, name=None, picture=None
            ),
        )
        await users.upsert_password_credential(user.id, password_hash="a-real-hash")

        checker = SignInMethodChecker(users, google, SqlPasskeyRepository(session))
        await ensure_can_post_to_ledger(checker, user.id)  # does not raise

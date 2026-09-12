"""Pure-logic tests for IAM-010f: an account whose only sign-in method is
Google must be blocked from posting to the ledger until a passkey or
password is added. No database - uses the in-memory repository fakes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.auth.account_continuity import (
    GoogleOnlyAccountError,
    SignInMethodChecker,
    ensure_can_post_to_ledger,
)
from api.auth.google_oidc import GoogleIdentity
from tests.support.fake_auth_repository import InMemoryUserRepository
from tests.support.fake_google_identity_repository import InMemoryGoogleIdentityRepository
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository

_CheckerFixture = tuple[
    SignInMethodChecker,
    InMemoryUserRepository,
    InMemoryGoogleIdentityRepository,
    InMemoryPasskeyRepository,
]


def _checker() -> _CheckerFixture:
    users = InMemoryUserRepository()
    google = InMemoryGoogleIdentityRepository()
    passkeys = InMemoryPasskeyRepository()
    return SignInMethodChecker(users, google, passkeys), users, google, passkeys


async def _link_google(google: InMemoryGoogleIdentityRepository, user_id: uuid.UUID) -> None:
    await google.link(
        user_id,
        GoogleIdentity(subject=f"sub-{user_id}", email="user@example.com", name=None, picture=None),
    )


async def _add_passkey(passkeys: InMemoryPasskeyRepository, user_id: uuid.UUID) -> uuid.UUID:
    passkey = await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=uuid.uuid4().bytes,
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )
    return passkey.id


# --- The central claim ------------------------------------------------------


async def test_google_only_account_is_flagged_and_blocked() -> None:
    checker, users, google, _passkeys = _checker()
    user = await users.create("owner@example.com")
    await _link_google(google, user.id)

    status = await checker.check(user.id)
    assert status.is_google_only is True

    with pytest.raises(GoogleOnlyAccountError) as exc_info:
        await ensure_can_post_to_ledger(checker, user.id)
    assert exc_info.value.user_id == user.id


async def test_google_plus_password_is_not_google_only() -> None:
    checker, users, google, _passkeys = _checker()
    user = await users.create("owner@example.com")
    await _link_google(google, user.id)
    await users.upsert_password_credential(user.id, password_hash="a-real-hash")

    status = await checker.check(user.id)
    assert status.is_google_only is False

    await ensure_can_post_to_ledger(checker, user.id)  # does not raise


async def test_google_plus_passkey_is_not_google_only() -> None:
    checker, users, google, passkeys = _checker()
    user = await users.create("owner@example.com")
    await _link_google(google, user.id)
    await _add_passkey(passkeys, user.id)

    status = await checker.check(user.id)
    assert status.is_google_only is False

    await ensure_can_post_to_ledger(checker, user.id)  # does not raise


async def test_password_only_account_is_never_flagged() -> None:
    checker, users, _google, _passkeys = _checker()
    user = await users.create("owner@example.com")
    await users.upsert_password_credential(user.id, password_hash="a-real-hash")

    status = await checker.check(user.id)
    assert status.is_google_only is False
    assert status.has_google is False


async def test_passkey_only_account_is_never_flagged() -> None:
    checker, users, _google, passkeys = _checker()
    user = await users.create("owner@example.com")
    await _add_passkey(passkeys, user.id)

    status = await checker.check(user.id)
    assert status.is_google_only is False


async def test_no_methods_at_all_is_not_reported_as_google_only() -> None:
    """A theoretical, currently-unreachable state (no real signup path
    creates a user with zero methods) - is_google_only specifically means
    "Google is the sole method present," not "this account can't sign in
    at all," and the two must not be conflated.
    """
    checker, users, _google, _passkeys = _checker()
    user = await users.create("owner@example.com")

    status = await checker.check(user.id)
    assert status.has_google is False
    assert status.is_google_only is False


# --- Dynamic re-evaluation: not cached --------------------------------------


async def test_revoking_the_only_passkey_reintroduces_the_block() -> None:
    """The check must be evaluated fresh each call - an account that
    passed yesterday because it held a passkey, and has since revoked it
    (its last non-Google method), must be caught today.
    """
    checker, users, google, passkeys = _checker()
    user = await users.create("owner@example.com")
    await _link_google(google, user.id)
    passkey_id = await _add_passkey(passkeys, user.id)

    await ensure_can_post_to_ledger(checker, user.id)  # fine, has a passkey

    await passkeys.revoke(passkey_id, at=datetime.now(UTC))

    with pytest.raises(GoogleOnlyAccountError):
        await ensure_can_post_to_ledger(checker, user.id)


async def test_adding_a_password_later_lifts_the_block() -> None:
    checker, users, google, _passkeys = _checker()
    user = await users.create("owner@example.com")
    await _link_google(google, user.id)

    with pytest.raises(GoogleOnlyAccountError):
        await ensure_can_post_to_ledger(checker, user.id)

    await users.upsert_password_credential(user.id, password_hash="a-real-hash")

    await ensure_can_post_to_ledger(checker, user.id)  # does not raise

"""IAM-010c: when a Google sign-in matches an existing verified email, the
user must authenticate with their existing method once before the
identities are linked. Automatic linking on matching email alone must be
impossible.

These tests are written before the implementation (api.auth.google_signin
does not yet have a re-authentication-gated linking method as of this
file's creation) - they define the required behavior, most importantly the
attack case: an attacker who controls a Google account bearing a victim's
email address must gain nothing from that alone.
"""

from __future__ import annotations

import uuid

import pytest

from api.auth.breach_check import LocalDenylistBreachChecker
from api.auth.google_oidc import GoogleIdentity
from api.auth.google_signin import (
    GoogleSignInLinkRequired,
    GoogleSignInService,
    LinkIdentityMismatchError,
)
from api.auth.models import User
from api.auth.service import AuthenticationService, InvalidCredentialsError
from tests.support.fake_auth_repository import InMemoryUserRepository
from tests.support.fake_google_identity_repository import InMemoryGoogleIdentityRepository

_VICTIM_EMAIL = "victim@example.com"
_VICTIM_PASSWORD = "the victims real passphrase"


def _harness() -> tuple[GoogleSignInService, AuthenticationService, InMemoryUserRepository]:
    users = InMemoryUserRepository()
    authentication = AuthenticationService(users, LocalDenylistBreachChecker())
    google_signin = GoogleSignInService(users, InMemoryGoogleIdentityRepository(), authentication)
    return google_signin, authentication, users


def _attacker_identity() -> GoogleIdentity:
    """A Google identity an ATTACKER controls, whose email happens to
    match the victim's LEDGR account. email_verified=True is exactly what
    api.auth.google_oidc already guarantees before a GoogleIdentity can
    exist at all (IAM-010b) - the point of this test file is that being a
    real, verified Google account for that email is NOT sufficient on its
    own to gain access to the victim's LEDGR account (IAM-010c).
    """
    return GoogleIdentity(
        subject="attacker-controlled-google-subject",
        email=_VICTIM_EMAIL,
        name="Attacker",
        picture=None,
    )


async def _register_victim(authentication: AuthenticationService) -> User:
    return await authentication.register_user(_VICTIM_EMAIL, _VICTIM_PASSWORD)


# --- The attack case -----------------------------------------------------


async def test_attacker_controlled_google_identity_gains_no_access_without_the_password() -> None:
    """The central claim of IAM-010c. An attacker who controls a verified
    Google account for the victim's email address, and who does NOT know
    the victim's password, must not be able to link that identity to the
    victim's account - and therefore must not gain access to it.
    """
    google_signin, authentication, users = _harness()
    victim = await _register_victim(authentication)

    link_required = await google_signin.sign_in(_attacker_identity())
    assert isinstance(link_required, GoogleSignInLinkRequired)
    assert link_required.existing_user_id == victim.id

    # The attacker does not know the victim's password. Any guess fails.
    with pytest.raises(InvalidCredentialsError):
        await google_signin.confirm_link_with_password(
            link_required, password="attacker's best guess"
        )

    # Nothing was linked as a side effect of the failed attempt.
    still_link_required = await google_signin.sign_in(_attacker_identity())
    assert isinstance(still_link_required, GoogleSignInLinkRequired)

    # The victim's account itself is entirely unaffected - they can still
    # sign in with their own password, undisturbed.
    authenticated_victim = await authentication.authenticate_with_password(
        _VICTIM_EMAIL, _VICTIM_PASSWORD
    )
    assert authenticated_victim.id == victim.id


async def test_repeated_attack_attempts_never_succeed() -> None:
    """Guards against a subtler bug: some kind of state accumulating across
    failed attempts (a retry counter, a cached partial link) that
    eventually lets an unauthenticated attempt through. It shouldn't -
    every attempt is independent and every wrong password fails the same
    way, no matter how many times it's tried.
    """
    google_signin, authentication, users = _harness()
    await _register_victim(authentication)

    for _ in range(5):
        link_required = await google_signin.sign_in(_attacker_identity())
        assert isinstance(link_required, GoogleSignInLinkRequired)
        with pytest.raises(InvalidCredentialsError):
            await google_signin.confirm_link_with_password(link_required, password="still wrong")


# --- The legitimate flow --------------------------------------------------


async def test_confirm_link_with_the_correct_password_links_and_returns_the_user() -> None:
    google_signin, authentication, users = _harness()
    victim = await _register_victim(authentication)
    identity = GoogleIdentity(
        subject="victims-real-google-subject", email=_VICTIM_EMAIL, name="Victim", picture=None
    )

    link_required = await google_signin.sign_in(identity)
    assert isinstance(link_required, GoogleSignInLinkRequired)

    linked_user = await google_signin.confirm_link_with_password(
        link_required, password=_VICTIM_PASSWORD
    )
    assert linked_user.id == victim.id

    # The link took effect: signing in with the same Google identity again
    # now recognizes the account directly, no further password needed.
    second_sign_in = await google_signin.sign_in(identity)
    assert isinstance(second_sign_in, User)
    assert second_sign_in.id == victim.id


# --- Defense in depth ------------------------------------------------------


async def test_a_mismatched_existing_user_id_is_rejected_even_with_a_correct_password() -> None:
    """confirm_link_with_password re-authenticates by the email the
    GoogleSignInLinkRequired carries, then checks the resulting user's id
    against link_required.existing_user_id. This test forces that check to
    matter, by constructing a link_required whose existing_user_id has been
    tampered with (simulating a stale or forged challenge object) - it
    must be rejected even though the password itself is correct for the
    account that email actually resolves to.
    """
    google_signin, authentication, users = _harness()
    await _register_victim(authentication)
    identity = GoogleIdentity(
        subject="victims-real-google-subject", email=_VICTIM_EMAIL, name="Victim", picture=None
    )
    tampered = GoogleSignInLinkRequired(existing_user_id=uuid.uuid4(), google_identity=identity)

    with pytest.raises(LinkIdentityMismatchError):
        await google_signin.confirm_link_with_password(tampered, password=_VICTIM_PASSWORD)


async def test_an_account_with_no_password_credential_cannot_be_linked_this_way() -> None:
    """A user who exists but has no password credential at all (e.g. a
    future Google-only or passkey-only account) cannot be "re-authenticated
    with their existing method" via a password, because they don't have
    one - confirm_link_with_password must fail closed here too, not treat
    "no credential" as "no barrier."
    """
    users = InMemoryUserRepository()
    authentication = AuthenticationService(users, LocalDenylistBreachChecker())
    google_signin = GoogleSignInService(users, InMemoryGoogleIdentityRepository(), authentication)
    passwordless_user = await users.create(_VICTIM_EMAIL)
    identity = GoogleIdentity(
        subject="some-google-subject", email=_VICTIM_EMAIL, name=None, picture=None
    )
    link_required = GoogleSignInLinkRequired(
        existing_user_id=passwordless_user.id, google_identity=identity
    )

    with pytest.raises(InvalidCredentialsError):
        await google_signin.confirm_link_with_password(link_required, password="anything at all")


async def test_google_sign_in_service_has_no_link_path_that_skips_reauthentication() -> None:
    """A structural safeguard, not just a behavioral one: the only public
    method capable of linking a Google identity to an EXISTING account is
    confirm_link_with_password. There must be no other method on this
    class - present now or added carelessly later - that takes a user id
    and a Google identity and links them without going through a
    credential check. (Creating a brand-new account during sign_in for an
    email nobody holds yet is not this case - there is no existing account
    to protect there.)
    """
    public_methods = {
        name
        for name in dir(GoogleSignInService)
        if not name.startswith("_") and callable(getattr(GoogleSignInService, name))
    }
    assert public_methods == {"sign_in", "confirm_link_with_password"}

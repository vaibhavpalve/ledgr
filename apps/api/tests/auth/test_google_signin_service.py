"""Pure-logic tests for GoogleSignInService.sign_in - no database, using
InMemoryUserRepository and InMemoryGoogleIdentityRepository.

The reauthenticated-linking path (confirm_link_with_password) and its
security properties, including the IAM-010c attack case, are covered
separately in tests/auth/test_google_account_linking.py - kept apart from
this file so the linking/security-focused tests aren't lost among the
more routine sign_in-shape tests here.
"""

from __future__ import annotations

from api.auth.breach_check import LocalDenylistBreachChecker
from api.auth.google_oidc import GoogleIdentity
from api.auth.google_signin import GoogleSignInLinkRequired, GoogleSignInService
from api.auth.models import User
from api.auth.service import AuthenticationService
from tests.support.fake_auth_repository import InMemoryUserRepository
from tests.support.fake_google_identity_repository import InMemoryGoogleIdentityRepository


def _identity(*, subject: str = "sub-1", email: str = "new@example.com") -> GoogleIdentity:
    return GoogleIdentity(subject=subject, email=email, name="Test User", picture=None)


def _service() -> tuple[GoogleSignInService, InMemoryUserRepository]:
    users = InMemoryUserRepository()
    authentication = AuthenticationService(users, LocalDenylistBreachChecker())
    service = GoogleSignInService(users, InMemoryGoogleIdentityRepository(), authentication)
    return service, users


async def test_brand_new_email_creates_and_links_a_user() -> None:
    service, users = _service()

    result = await service.sign_in(_identity(email="fresh@example.com"))

    assert isinstance(result, User)
    assert result.email == "fresh@example.com"
    assert await users.get_by_email("fresh@example.com") is not None


async def test_a_returning_google_user_is_recognized_by_subject_not_email() -> None:
    service, _ = _service()
    identity = _identity(subject="sub-42", email="repeat@example.com")
    first_sign_in = await service.sign_in(identity)
    assert isinstance(first_sign_in, User)

    second_sign_in = await service.sign_in(identity)

    assert isinstance(second_sign_in, User)
    assert second_sign_in.id == first_sign_in.id


async def test_an_email_matching_an_existing_user_requires_linking_not_auto_link() -> None:
    """IAM-010c, the central claim: a Google identity whose email matches
    an existing account (created via a different method) must NOT be
    linked automatically. See test_google_account_linking.py for the full
    re-authentication-gated linking flow and the attack case.
    """
    service, users = _service()
    existing_user = await users.create("shared@example.com")

    result = await service.sign_in(_identity(subject="sub-99", email="shared@example.com"))

    assert isinstance(result, GoogleSignInLinkRequired)
    assert result.existing_user_id == existing_user.id
    # And critically: no link was actually created as a side effect.
    linked_user_id = await service._google_identities.get_user_id_by_subject("sub-99")
    assert linked_user_id is None

"""Pure-logic tests for IAM-018: account recovery never grants access on
email alone. No database - composes the in-memory fakes for every
dependency (users, sessions, TOTP, passkeys, the recovery log).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from api.auth.account_recovery import (
    AccountRecoveryService,
    InvalidRecoveryProofError,
    RecoveryReasonRequiredError,
    UserNotFoundError,
)
from api.auth.breach_check import LocalDenylistBreachChecker
from api.auth.passkeys import WebAuthnService
from api.auth.passwords import WeakPasswordError, verify_password
from api.auth.sessions import SessionRevokedError, SessionService
from api.auth.totp import TotpService
from api.crypto.kms import LocalDevKeyManagementService
from tests.auth.webauthn_helpers import FakeAuthenticator
from tests.support.fake_account_recovery_repository import InMemoryAccountRecoveryEventRepository
from tests.support.fake_auth_repository import InMemorySessionRepository, InMemoryUserRepository
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository
from tests.support.fake_totp_repository import InMemoryTotpRepository

_NEW_PASSWORD = "a perfectly fine new passphrase"
_RP_ID = "ledgr.test"
_ORIGIN = "https://ledgr.test"
_INTERVAL = 30


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class _Harness:
    service: AccountRecoveryService
    users: InMemoryUserRepository
    sessions: SessionService
    totp: TotpService
    webauthn: WebAuthnService
    recovery_log: InMemoryAccountRecoveryEventRepository
    clock: _FakeClock


def _kms() -> LocalDevKeyManagementService:
    return LocalDevKeyManagementService(
        current_kek_id="test-kek-v1", keks={"test-kek-v1": b"3" * 32}
    )


def _harness() -> _Harness:
    clock = _FakeClock()
    users = InMemoryUserRepository()
    sessions = SessionService(InMemorySessionRepository())
    totp = TotpService(InMemoryTotpRepository(), _kms(), clock=clock)
    webauthn = WebAuthnService(
        InMemoryPasskeyRepository(), rp_id=_RP_ID, rp_name="LEDGR Test", expected_origin=_ORIGIN
    )
    recovery_log = InMemoryAccountRecoveryEventRepository()
    service = AccountRecoveryService(
        users, sessions, totp, webauthn, recovery_log, LocalDenylistBreachChecker()
    )
    return _Harness(service, users, sessions, totp, webauthn, recovery_log, clock)


def _totp_code(secret: str, at: datetime) -> str:
    return pyotp.TOTP(secret, interval=_INTERVAL).at(at)


async def _enroll_totp(h: _Harness, user_id: uuid.UUID) -> str:
    """Returns the secret so the caller can compute further codes. Leaves
    the clock one interval past the step consumed by confirmation, so a
    caller's very next _totp_code(secret, h.clock.now) call is guaranteed
    to be for a fresh, not-yet-used step - real recovery attempts always
    happen after enrollment, never in the same 30-second window as it.
    """
    enrollment = h.totp.begin_enrollment(account_name="owner@example.com")
    await h.totp.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_totp_code(enrollment.secret, h.clock.now)
    )
    h.clock.advance(seconds=_INTERVAL)
    return enrollment.secret


# --- TOTP-based recovery ----------------------------------------------------


async def test_recover_with_totp_sets_a_new_password() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    secret = await _enroll_totp(h, user.id)

    resolved_id = await h.service.recover_with_totp(
        email="owner@example.com", code=_totp_code(secret, h.clock.now), new_password=_NEW_PASSWORD
    )

    assert resolved_id == user.id
    credential = await h.users.get_password_credential(user.id)
    assert credential is not None
    assert verify_password(_NEW_PASSWORD, credential.password_hash)


async def test_recover_with_totp_email_is_normalized() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    secret = await _enroll_totp(h, user.id)

    resolved_id = await h.service.recover_with_totp(
        email="  Owner@Example.com  ",
        code=_totp_code(secret, h.clock.now),
        new_password=_NEW_PASSWORD,
    )
    assert resolved_id == user.id


async def test_recover_with_totp_rejects_a_wrong_code() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    await _enroll_totp(h, user.id)

    with pytest.raises(InvalidRecoveryProofError):
        await h.service.recover_with_totp(
            email="owner@example.com", code="000000", new_password=_NEW_PASSWORD
        )


async def test_recover_with_totp_rejects_a_nonexistent_account() -> None:
    """The same exception as a wrong code - no disclosure of whether the
    account exists (SEC-008-flavored, mirroring api.auth.service.
    InvalidCredentialsError).
    """
    h = _harness()
    with pytest.raises(InvalidRecoveryProofError):
        await h.service.recover_with_totp(
            email="nobody@example.com", code="000000", new_password=_NEW_PASSWORD
        )


async def test_recover_with_totp_revokes_every_existing_session() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    secret = await _enroll_totp(h, user.id)
    _, token = await h.sessions.issue_session(user.id, privileged=False)

    await h.service.recover_with_totp(
        email="owner@example.com", code=_totp_code(secret, h.clock.now), new_password=_NEW_PASSWORD
    )

    with pytest.raises(SessionRevokedError):
        await h.sessions.validate_session(token)


async def test_recover_with_totp_logs_a_self_service_event() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    secret = await _enroll_totp(h, user.id)

    await h.service.recover_with_totp(
        email="owner@example.com", code=_totp_code(secret, h.clock.now), new_password=_NEW_PASSWORD
    )

    events = await h.recovery_log.list_for_user(user.id)
    assert len(events) == 1
    assert events[0].method == "totp"
    assert events[0].performed_by_user_id is None


async def test_recover_with_totp_rejects_a_weak_new_password_and_finalizes_nothing() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    secret = await _enroll_totp(h, user.id)

    with pytest.raises(WeakPasswordError):
        await h.service.recover_with_totp(
            email="owner@example.com", code=_totp_code(secret, h.clock.now), new_password="short"
        )

    assert await h.users.get_password_credential(user.id) is None
    assert await h.recovery_log.list_for_user(user.id) == []


# --- Passkey-based recovery --------------------------------------------------


async def test_recover_with_passkey_sets_a_new_password_and_resolves_its_own_owner() -> None:
    """No email is passed - WebAuthn is discoverable, so the credential
    itself identifies the account.
    """
    h = _harness()
    user = await h.users.create("owner@example.com")
    authenticator = FakeAuthenticator()
    reg_challenge = h.webauthn.begin_registration(
        user_id=user.id, user_email="owner@example.com", existing_passkeys=[]
    )
    reg_credential = authenticator.build_registration_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=reg_challenge.challenge
    )
    await h.webauthn.complete_registration(
        user_id=user.id, name="Device", challenge=reg_challenge.challenge, credential=reg_credential
    )

    auth_challenge = h.webauthn.begin_authentication()
    auth_credential = authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=auth_challenge.challenge
    )

    resolved_id = await h.service.recover_with_passkey(
        challenge=auth_challenge.challenge, credential=auth_credential, new_password=_NEW_PASSWORD
    )

    assert resolved_id == user.id
    credential = await h.users.get_password_credential(user.id)
    assert credential is not None
    assert verify_password(_NEW_PASSWORD, credential.password_hash)

    events = await h.recovery_log.list_for_user(user.id)
    assert events[0].method == "passkey"


async def test_recover_with_passkey_rejects_an_unregistered_credential() -> None:
    h = _harness()
    stray_authenticator = FakeAuthenticator()
    auth_challenge = h.webauthn.begin_authentication()
    auth_credential = stray_authenticator.build_authentication_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=auth_challenge.challenge
    )

    with pytest.raises(InvalidRecoveryProofError):
        await h.service.recover_with_passkey(
            challenge=auth_challenge.challenge,
            credential=auth_credential,
            new_password=_NEW_PASSWORD,
        )


# --- Admin-initiated recovery -----------------------------------------------


async def test_admin_reset_requires_a_reason() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    admin_id = uuid.uuid4()

    with pytest.raises(RecoveryReasonRequiredError):
        await h.service.admin_reset_password(
            target_user_id=user.id,
            new_password=_NEW_PASSWORD,
            performed_by_user_id=admin_id,
            reason="   ",
        )


async def test_admin_reset_rejects_a_nonexistent_target() -> None:
    h = _harness()
    with pytest.raises(UserNotFoundError):
        await h.service.admin_reset_password(
            target_user_id=uuid.uuid4(),
            new_password=_NEW_PASSWORD,
            performed_by_user_id=uuid.uuid4(),
            reason="lost everything, verified identity via support ticket #123",
        )


async def test_admin_reset_sets_the_password_revokes_sessions_and_logs_the_actor() -> None:
    h = _harness()
    user = await h.users.create("owner@example.com")
    admin_id = uuid.uuid4()
    _, token = await h.sessions.issue_session(user.id, privileged=False)

    await h.service.admin_reset_password(
        target_user_id=user.id,
        new_password=_NEW_PASSWORD,
        performed_by_user_id=admin_id,
        reason="lost everything, verified identity via support ticket #123",
    )

    credential = await h.users.get_password_credential(user.id)
    assert credential is not None
    assert verify_password(_NEW_PASSWORD, credential.password_hash)

    with pytest.raises(SessionRevokedError):
        await h.sessions.validate_session(token)

    events = await h.recovery_log.list_for_user(user.id)
    assert len(events) == 1
    assert events[0].method == "admin_reset"
    assert events[0].performed_by_user_id == admin_id
    assert events[0].reason == "lost everything, verified identity via support ticket #123"

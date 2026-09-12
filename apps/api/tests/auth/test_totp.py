"""Pure-logic tests for TOTP enrollment and verification (IAM-012) - no
database, using InMemoryTotpRepository and LocalDevKeyManagementService
(the same KMS stand-in used by the encryption-key harness).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from api.auth.totp import (
    InvalidTotpCodeError,
    TotpAlreadyEnrolledError,
    TotpCodeAlreadyUsedError,
    TotpNotEnrolledError,
    TotpService,
)
from api.crypto.kms import LocalDevKeyManagementService
from tests.support.fake_totp_repository import InMemoryTotpRepository

_INTERVAL = 30


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _kms() -> LocalDevKeyManagementService:
    return LocalDevKeyManagementService(
        current_kek_id="test-kek-v1", keks={"test-kek-v1": b"7" * 32}
    )


def _service(clock: _FakeClock) -> TotpService:
    return TotpService(InMemoryTotpRepository(), _kms(), clock=clock)


def _code_for(secret: str, at: datetime) -> str:
    return pyotp.TOTP(secret, interval=_INTERVAL).at(at)


def _wrong_code_for(secret: str, at: datetime) -> str:
    """A code guaranteed not to match any step in the +/-1 verification
    window, so this never risks the ~3-in-a-million flake a fixed literal
    like "000000" would carry.
    """
    valid = {
        pyotp.TOTP(secret, interval=_INTERVAL).at(at + timedelta(seconds=_INTERVAL * offset))
        for offset in (-1, 0, 1)
    }
    for candidate in range(1_000_000):
        code = f"{candidate:06d}"
        if code not in valid:
            return code
    raise AssertionError("unreachable")  # pragma: no cover


async def test_enrollment_then_ongoing_verification_round_trip() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()

    enrollment = service.begin_enrollment(account_name="owner@example.com")
    credential = await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )
    assert credential.is_active

    clock.advance(seconds=_INTERVAL)
    verified = await service.verify_code(
        user_id=user_id, code=_code_for(enrollment.secret, clock.now)
    )
    assert verified.id == credential.id


async def test_provisioning_uri_names_the_account_and_issuer() -> None:
    service = _service(_FakeClock())
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    assert (
        "owner%40example.com" in enrollment.provisioning_uri
        or "owner@example.com" in enrollment.provisioning_uri
    )
    assert "otpauth://totp/" in enrollment.provisioning_uri


async def test_confirming_with_a_wrong_code_fails_and_persists_nothing() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")

    with pytest.raises(InvalidTotpCodeError):
        await service.confirm_enrollment(
            user_id=user_id,
            secret=enrollment.secret,
            code=_wrong_code_for(enrollment.secret, clock.now),
        )

    with pytest.raises(TotpNotEnrolledError):
        await service.verify_code(user_id=user_id, code="000000")


async def test_wrong_code_during_ongoing_verification_is_rejected() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )

    clock.advance(seconds=_INTERVAL)
    with pytest.raises(InvalidTotpCodeError):
        await service.verify_code(
            user_id=user_id, code=_wrong_code_for(enrollment.secret, clock.now)
        )


async def test_a_code_cannot_be_reused_replay_protection() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    code = _code_for(enrollment.secret, clock.now)
    await service.confirm_enrollment(user_id=user_id, secret=enrollment.secret, code=code)

    # Same code again, same step - a replay attempt, distinguished from an
    # ordinary wrong code.
    with pytest.raises(TotpCodeAlreadyUsedError):
        await service.verify_code(user_id=user_id, code=code)


async def test_clock_drift_within_one_step_is_tolerated() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )

    # Generate a code as if only one step had passed since enrollment
    # (simulating an authenticator app whose clock lags the server's by
    # ~30s), then advance the VERIFIER's clock by two steps before
    # checking it - within the +/-1 step tolerance, and crucially a step
    # that enrollment itself did not already consume.
    lagging_code = _code_for(enrollment.secret, clock.now + timedelta(seconds=_INTERVAL))
    clock.advance(seconds=_INTERVAL * 2)

    verified = await service.verify_code(user_id=user_id, code=lagging_code)
    assert verified is not None


async def test_double_enrollment_is_rejected_while_active() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )

    second_enrollment = service.begin_enrollment(account_name="owner@example.com")
    with pytest.raises(TotpAlreadyEnrolledError):
        await service.confirm_enrollment(
            user_id=user_id,
            secret=second_enrollment.secret,
            code=_code_for(second_enrollment.secret, clock.now),
        )


async def test_reenrollment_after_revocation_succeeds() -> None:
    """Regression test: the schema's uniqueness is a partial index (active
    credentials only, migrations/0006_mfa.sql) precisely so this works -
    the same class of bug fixed for administration_encryption_key.
    """
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    first = await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )

    await service.revoke(user_id)
    clock.advance(seconds=_INTERVAL)

    new_enrollment = service.begin_enrollment(account_name="owner@example.com")
    second = await service.confirm_enrollment(
        user_id=user_id,
        secret=new_enrollment.secret,
        code=_code_for(new_enrollment.secret, clock.now),
    )
    assert second.id != first.id
    assert second.is_active


async def test_revoking_without_enrollment_fails() -> None:
    service = _service(_FakeClock())
    with pytest.raises(TotpNotEnrolledError):
        await service.revoke(uuid.uuid4())


async def test_verifying_a_revoked_credential_fails() -> None:
    clock = _FakeClock()
    service = _service(clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )
    await service.revoke(user_id)

    clock.advance(seconds=_INTERVAL)
    with pytest.raises(TotpNotEnrolledError):
        await service.verify_code(user_id=user_id, code=_code_for(enrollment.secret, clock.now))


async def test_the_secret_is_never_stored_in_recoverable_plaintext_form() -> None:
    """The InMemoryTotpRepository stores exactly what confirm_enrollment
    hands it - so if the wrapped_secret bytes contained the plaintext
    secret, this test would catch it directly.
    """
    clock = _FakeClock()
    repository = InMemoryTotpRepository()
    service = TotpService(repository, _kms(), clock=clock)
    user_id = uuid.uuid4()
    enrollment = service.begin_enrollment(account_name="owner@example.com")
    await service.confirm_enrollment(
        user_id=user_id, secret=enrollment.secret, code=_code_for(enrollment.secret, clock.now)
    )

    stored = await repository.get_for_user(user_id)
    assert stored is not None
    assert enrollment.secret.encode("ascii") not in stored.wrapped_secret

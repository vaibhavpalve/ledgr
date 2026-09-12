"""Pure-logic tests for the MFA policy layer (IAM-011, IAM-012, IAM-010e) -
no database, using the in-memory passkey/TOTP repository fakes and
directly-constructed GoogleIdentity values.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from api.auth.google_oidc import GoogleIdentity
from api.auth.mfa import (
    AlwaysRequireMfaPolicy,
    MfaEnrollmentChecker,
    MfaFactorKind,
    MfaPolicyService,
    google_asserts_second_factor,
)
from tests.support.fake_passkey_repository import InMemoryPasskeyRepository
from tests.support.fake_totp_repository import InMemoryTotpRepository


def _empty_checker() -> MfaEnrollmentChecker:
    return MfaEnrollmentChecker(InMemoryPasskeyRepository(), InMemoryTotpRepository())


# --- IAM-012: the closed factor set ----------------------------------------


def test_mfa_factor_kinds_are_exhaustive_and_exclude_sms() -> None:
    """This closed enum is the mechanism that keeps SMS from becoming
    addable without a code change - see api.auth.mfa's module docstring.
    """
    values = {member.value for member in MfaFactorKind}
    assert values == {"passkey", "totp"}
    assert "sms" not in values
    assert len(MfaFactorKind) == 2


# --- Enrollment checking ----------------------------------------------------


async def test_enrollment_checker_reports_no_factors_for_a_fresh_user() -> None:
    status = await _empty_checker().check(uuid.uuid4())
    assert status.is_enrolled is False
    assert status.has_passkey is False
    assert status.has_totp is False


async def test_enrollment_checker_reports_a_registered_passkey() -> None:
    passkeys = InMemoryPasskeyRepository()
    user_id = uuid.uuid4()
    await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=b"cred-1",
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )

    status = await MfaEnrollmentChecker(passkeys, InMemoryTotpRepository()).check(user_id)

    assert status.has_passkey is True
    assert status.is_enrolled is True


async def test_enrollment_checker_ignores_revoked_passkeys() -> None:
    passkeys = InMemoryPasskeyRepository()
    user_id = uuid.uuid4()
    created = await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=b"cred-1",
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )
    await passkeys.revoke(created.id, at=datetime.now(UTC))

    status = await MfaEnrollmentChecker(passkeys, InMemoryTotpRepository()).check(user_id)

    assert status.has_passkey is False
    assert status.is_enrolled is False


# --- MfaPolicyService.evaluate: the four outcomes --------------------------


class _NeverRequirePolicy:
    async def is_required(self, *, user_id: uuid.UUID) -> bool:
        return False


async def test_not_required_short_circuits_without_consulting_enrollment() -> None:
    service = MfaPolicyService(_NeverRequirePolicy(), _empty_checker())

    evaluation = await service.evaluate(user_id=uuid.uuid4(), mfa_verified=False)

    assert evaluation.required is False
    assert evaluation.satisfied is True
    assert evaluation.reason == "not_required"


async def test_satisfied_when_this_request_is_already_mfa_verified() -> None:
    service = MfaPolicyService(AlwaysRequireMfaPolicy(), _empty_checker())

    evaluation = await service.evaluate(user_id=uuid.uuid4(), mfa_verified=True)

    assert evaluation.required is True
    assert evaluation.satisfied is True
    assert evaluation.reason == "satisfied"


async def test_not_enrolled_when_no_factors_and_not_verified() -> None:
    service = MfaPolicyService(AlwaysRequireMfaPolicy(), _empty_checker())

    evaluation = await service.evaluate(user_id=uuid.uuid4(), mfa_verified=False)

    assert evaluation.satisfied is False
    assert evaluation.reason == "not_enrolled"


async def test_not_verified_when_enrolled_but_not_verified_this_request() -> None:
    """The key distinction from "not_enrolled": having a factor is not the
    same as this request having proven it.
    """
    passkeys = InMemoryPasskeyRepository()
    user_id = uuid.uuid4()
    await passkeys.create(
        user_id=user_id,
        name="Device",
        credential_id=b"cred-1",
        public_key=b"pub",
        sign_count=0,
        transports=[],
        aaguid=None,
        backup_eligible=False,
        backed_up=False,
        authenticator_attachment=None,
    )
    service = MfaPolicyService(
        AlwaysRequireMfaPolicy(), MfaEnrollmentChecker(passkeys, InMemoryTotpRepository())
    )

    evaluation = await service.evaluate(user_id=user_id, mfa_verified=False)

    assert evaluation.satisfied is False
    assert evaluation.reason == "not_verified"


# --- IAM-010e: Google's amr claim -------------------------------------------


def _identity(amr: tuple[str, ...] = ()) -> GoogleIdentity:
    return GoogleIdentity(subject="sub", email="user@example.com", name=None, picture=None, amr=amr)


def test_google_asserts_second_factor_when_amr_contains_mfa() -> None:
    assert google_asserts_second_factor(_identity(amr=("pwd", "mfa"))) is True


def test_google_does_not_assert_second_factor_when_amr_is_absent() -> None:
    assert google_asserts_second_factor(_identity()) is False


def test_google_does_not_assert_second_factor_for_unrelated_amr_values() -> None:
    assert google_asserts_second_factor(_identity(amr=("pwd",))) is False

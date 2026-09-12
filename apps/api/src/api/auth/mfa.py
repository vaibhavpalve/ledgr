"""MFA policy (IAM-011, IAM-012, IAM-010e): what counts as a factor, who
must have one verified, and how those two questions combine into a single
enforcement decision the HTTP middleware (api.mfa_middleware) applies.

--- The closed factor set (IAM-012) ---

MfaFactorKind below is the complete, exhaustive set of MFA mechanisms this
system implements: passkey (api.auth.passkeys - this also covers "platform
biometrics bound to a device key," see the note on that member) and TOTP
(api.auth.totp). SMS is not a member, and there is no generic, pluggable
"factor type" registry, config flag, or database-driven list that could add
one at runtime - "SMS is not offered" is enforced by there being no code
path that verifies an SMS code at all, not by a setting that happens to be
off. Adding SMS support would require: a new MfaFactorKind member (a code
change to this enum), a new credential table and repository (a migration
and new modules, mirroring api.auth.totp), and explicit new logic in
MfaEnrollmentChecker below wiring it in - three separate, reviewable code
changes, not a configuration toggle. Do not add an SMS member here as a
"quick" way to support it later; that is exactly the shortcut this
requirement exists to prevent.

--- Passkey and "platform biometrics bound to a device key" are one mechanism ---

IAM-012 names these as two items, but both are WebAuthn credentials -
api.auth.passkeys.Passkey.authenticator_attachment records which kind a
given credential is ('platform' vs 'cross-platform'/roaming) for audit,
but MfaEnrollmentChecker below treats any non-revoked passkey, regardless
of attachment, as satisfying this factor. Building two separately-tracked
"factor kinds" for what is one underlying credential mechanism and one
verification code path was judged unnecessary complexity - see
docs/decisions/ADR-008-mfa-policy.md.

--- IAM-011's two triggers, and why the interim policy is "always required" ---

IAM-011 requires MFA for (a) any user with write permission, and (b) every
user of an administration that has filed a tax return. Neither is
determinable today: (a) needs the role/permission system (IAM-030+, not
built anywhere in this codebase yet - see every prior auth ADR's
"Consequences" section), and (b) needs VAT filing (FR-VAT-003, not built).
AlwaysRequireMfaPolicy is the deliberately conservative interim answer:
until those systems exist to narrow the requirement to its stated
boundary, every authenticated user is treated as if trigger (a) applies -
which is a safe superset of both triggers, never a gap, since "everyone"
already includes "everyone with write permission" and "everyone in a
filed administration." MfaRequirementPolicy is a real, typed extension
point specifically so a future RoleBasedMfaRequirementPolicy can replace
this without touching api.mfa_middleware at all.

--- IAM-010e: Google's amr claim ---

google_asserts_second_factor below is the one place that claim is
interpreted. See its docstring for what it does and does not guarantee.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from api.auth.google_oidc import GoogleIdentity
from api.auth.passkeys import PasskeyRepository
from api.auth.totp import TotpRepository


class MfaFactorKind(enum.Enum):
    """Exhaustive by design - see module docstring. Do not add SMS."""

    PASSKEY = "passkey"
    TOTP = "totp"


# RFC 8176 Authentication Method Reference values that indicate a second
# factor was used. Google's real-world amr support for consumer accounts
# is limited in practice - IAM-010e's own wording ("where... this is
# asserted") anticipates it may often be absent, in which case
# google_asserts_second_factor correctly returns False and LEDGR enrols
# its own factor, exactly as the requirement specifies.
_MFA_ASSERTING_AMR_VALUES = frozenset({"mfa"})


def google_asserts_second_factor(identity: GoogleIdentity) -> bool:
    """IAM-010e: "Google sign-in does not satisfy the MFA requirement on
    its own. Where the user's Google account has 2FA enabled and this is
    asserted in the token (amr), it may be accepted as the second factor;
    otherwise LEDGR enrols its own."

    This function is the accept/reject decision for the first half of that
    sentence. It does NOT itself cause a session to be marked MFA-verified
    - a caller (a future login flow) must still choose to act on it, e.g.
    by passing mfa_verified=True to SessionService.issue_session. Returns
    False (not "unknown" or an exception) when amr is absent, which is the
    common case for Google's real-world token issuance - absence is
    treated as "not asserted," never as "trust anyway."
    """
    return bool(set(identity.amr) & _MFA_ASSERTING_AMR_VALUES)


@dataclass(frozen=True, slots=True)
class MfaEnrollmentStatus:
    has_passkey: bool
    has_totp: bool

    @property
    def is_enrolled(self) -> bool:
        return self.has_passkey or self.has_totp


class MfaEnrollmentChecker:
    """Combines the two factor repositories into one enrollment answer.
    This is the ONLY place that decides which storage mechanisms count as
    "having a factor enrolled" - see the module docstring on why that is
    deliberately a fixed, two-branch check rather than a generic registry.
    """

    def __init__(self, passkeys: PasskeyRepository, totp: TotpRepository) -> None:
        self._passkeys = passkeys
        self._totp = totp

    async def check(self, user_id: uuid.UUID) -> MfaEnrollmentStatus:
        passkeys = await self._passkeys.list_for_user(user_id)
        has_passkey = any(not p.is_revoked for p in passkeys)

        totp_credential = await self._totp.get_for_user(user_id)
        has_totp = totp_credential is not None and totp_credential.is_active

        return MfaEnrollmentStatus(has_passkey=has_passkey, has_totp=has_totp)


class MfaRequirementPolicy(Protocol):
    """Decides WHO must satisfy MFA. See the module docstring for why
    AlwaysRequireMfaPolicy is the only implementation today and what would
    replace it.
    """

    async def is_required(self, *, user_id: uuid.UUID) -> bool: ...


class AlwaysRequireMfaPolicy:
    async def is_required(self, *, user_id: uuid.UUID) -> bool:
        return True


MfaEvaluationReason = Literal["not_required", "satisfied", "not_enrolled", "not_verified"]


@dataclass(frozen=True, slots=True)
class MfaEvaluation:
    required: bool
    satisfied: bool
    reason: MfaEvaluationReason


class MfaPolicyService:
    """The single combined decision api.mfa_middleware.MfaEnforcementMiddleware
    acts on: is MFA required for this user, and if so, is it satisfied.
    "Satisfied" means the CURRENT session/request already had a factor
    verified (mfa_verified=True, threaded through from
    Session.mfa_verified_at once real sessions are wired into the HTTP
    layer - see docs/decisions/ADR-008-mfa-policy.md) - not merely that
    the user has a factor enrolled somewhere. Enrollment status is only
    consulted to distinguish two different flavors of "not satisfied," for
    a more useful error: "you have no factor, go enroll one" versus "you
    have a factor, verify it."
    """

    def __init__(
        self, requirement_policy: MfaRequirementPolicy, enrollment: MfaEnrollmentChecker
    ) -> None:
        self._requirement_policy = requirement_policy
        self._enrollment = enrollment

    async def evaluate(self, *, user_id: uuid.UUID, mfa_verified: bool) -> MfaEvaluation:
        if not await self._requirement_policy.is_required(user_id=user_id):
            return MfaEvaluation(required=False, satisfied=True, reason="not_required")

        if mfa_verified:
            return MfaEvaluation(required=True, satisfied=True, reason="satisfied")

        status = await self._enrollment.check(user_id)
        if not status.is_enrolled:
            return MfaEvaluation(required=True, satisfied=False, reason="not_enrolled")

        return MfaEvaluation(required=True, satisfied=False, reason="not_verified")

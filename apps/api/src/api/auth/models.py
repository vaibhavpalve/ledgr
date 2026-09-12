"""Plain records for the authentication foundation (IAM-013, IAM-016).
Mirrors the shape of apps/api/src/api/crypto/envelope.py's EncryptionKeyRecord
pattern: dataclasses here, persistence behind a repository Protocol in
repository.py, so the service layer (service.py, sessions.py) is testable
against an in-memory fake without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    email: str
    status: str  # 'active' | 'suspended' | 'deactivated'
    mfa_enrolled: bool


@dataclass(frozen=True, slots=True)
class PasswordCredential:
    id: uuid.UUID
    user_id: uuid.UUID
    password_hash: str
    algorithm: str


@dataclass(frozen=True, slots=True)
class Session:
    id: uuid.UUID
    user_id: uuid.UUID
    token_hash: str
    privileged: bool
    created_at: datetime
    expires_at: datetime
    last_active_at: datetime
    last_reauthenticated_at: datetime
    # IAM-011: set when THIS session's second factor was verified - either
    # at issuance (e.g. a future login flow that did password+TOTP
    # together, or a Google sign-in whose amr claim asserted 2FA per
    # IAM-010e) or later via SessionService.record_mfa_verification (a
    # step-up flow). None means unverified. See
    # docs/decisions/ADR-008-mfa-policy.md for how this is intended to
    # reach api.mfa_middleware once real sessions are wired into the HTTP
    # layer - not done by this change.
    mfa_verified_at: datetime | None
    revoked_at: datetime | None
    ip_address: str | None
    user_agent: str | None

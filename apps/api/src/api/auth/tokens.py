"""Mints the bearer token api.tenancy.TenantContextMiddleware verifies.

api.auth.sessions.SessionService issues a real, stateful, revocable session
(ADR-005) - but the bearer token a client actually presents on every
subsequent request has to carry the claims TenantContextMiddleware already
decodes (`org_id`, `sub`, `mfa_verified`, `sid`), because every existing
tested route in this codebase reads tenant context through that exact
mechanism and none of it changes here (see docs/decisions/ADR-054-signup-
and-login.md for why replacing it was out of scope for this change).

So a login/signup response mints a JWT whose claims restate what the
session row already knows: `sid` is the session's own id (a UUID, not the
session's raw bearer secret - the two are deliberately different values;
see the ADR), and `mfa_verified`/`org_id` are the caller-supplied facts at
the moment of issuance. The token's own `exp` matches the session's
absolute lifetime, so a copied token cannot outlive the session record it
was minted from purely by not being checked against it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import jwt

from api.config import settings

ALGORITHM = "HS256"


def issue_access_token(
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    session_id: uuid.UUID,
    mfa_verified: bool,
    expires_at: datetime,
    active_administration_id: uuid.UUID | None = None,
) -> str:
    claims: dict[str, object] = {
        "sub": str(user_id),
        "org_id": str(organization_id),
        "sid": str(session_id),
        "mfa_verified": mfa_verified,
        "exp": expires_at,
    }
    if active_administration_id is not None:
        claims["adm"] = str(active_administration_id)
    return jwt.encode(claims, settings.jwt_signing_key, algorithm=ALGORITHM)

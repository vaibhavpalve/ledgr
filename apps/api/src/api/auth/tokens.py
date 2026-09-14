"""Mints the bearer token api.tenancy.TenantContextMiddleware verifies.

api.auth.sessions.SessionService issues a real, stateful, revocable session
(ADR-005). The bearer JWT a client presents on every subsequent request
names that session - `sid` is the session's own id, a UUID, not the raw
session secret, so the token and the session stay two different values
pointing at one row - together with who it was issued to (`sub`) and which
organization it is for (`org_id`).

That is all the token asserts. Whether the session is still live, whether
its second factor has been verified, and which administration it has open
are read from the `sessions` row on every request
(docs/decisions/ADR-060-session-backed-tenant-context.md), because a token
cannot know about anything that happened after it was minted. The token's
own `exp` matches the session's absolute lifetime so a copied token cannot
outlive the row purely by not being checked against it - belt and braces
now that every request does check.

`mfa_verified` is still written into the token for the CLIENT's benefit -
the login/signup responses tell the app whether to route to MFA enrolment
or straight in, and the same value is readable from the token it stores -
but the server does not read it back.
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
) -> str:
    claims: dict[str, object] = {
        "sub": str(user_id),
        "org_id": str(organization_id),
        "sid": str(session_id),
        "mfa_verified": mfa_verified,
        "exp": expires_at,
    }
    return jwt.encode(claims, settings.jwt_signing_key, algorithm=ALGORITHM)

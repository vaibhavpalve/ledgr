"""In-memory GoogleIdentityRepository double for testing
api.auth.google_signin without a database. Enforces the same uniqueness
the real schema does (user_google_identity: UNIQUE user_id, UNIQUE
google_subject) so a bug that would fail against real Postgres fails here
too, rather than only surfacing in tests/integration/ (which needs a live
database this environment may not have) - see the encryption-key harness's
InMemoryAdministrationKeyRepository for the precedent.
"""

from __future__ import annotations

import uuid

from api.auth.google_oidc import GoogleIdentity


class InMemoryGoogleIdentityRepository:
    def __init__(self) -> None:
        self._user_id_by_subject: dict[str, uuid.UUID] = {}
        self._subject_by_user_id: dict[uuid.UUID, str] = {}

    async def get_user_id_by_subject(self, subject: str) -> uuid.UUID | None:
        return self._user_id_by_subject.get(subject)

    async def link(self, user_id: uuid.UUID, identity: GoogleIdentity) -> None:
        if identity.subject in self._user_id_by_subject:
            raise ValueError(f"Google subject {identity.subject!r} is already linked")
        if user_id in self._subject_by_user_id:
            raise ValueError(f"user {user_id} already has a linked Google identity")
        self._user_id_by_subject[identity.subject] = user_id
        self._subject_by_user_id[user_id] = identity.subject

    async def exists_for_user(self, user_id: uuid.UUID) -> bool:
        return user_id in self._subject_by_user_id

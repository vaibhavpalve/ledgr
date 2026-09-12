"""SQLAlchemy-backed GoogleIdentityRepository, reading/writing
user_google_identity through an ordinary AsyncSession - see
migrations/0004_google_identity.sql.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.google_oidc import GoogleIdentity


class SqlGoogleIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user_id_by_subject(self, subject: str) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT user_id FROM user_google_identity WHERE google_subject = :subject"),
            {"subject": subject},
        )
        row = result.first()
        return row.user_id if row is not None else None

    async def link(self, user_id: uuid.UUID, identity: GoogleIdentity) -> None:
        await self._session.execute(
            text(
                "INSERT INTO user_google_identity "
                "(user_id, google_subject, email_at_link_time) "
                "VALUES (:user_id, :subject, :email)"
            ),
            {"user_id": str(user_id), "subject": identity.subject, "email": identity.email},
        )

    async def exists_for_user(self, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM user_google_identity WHERE user_id = :user_id"),
            {"user_id": str(user_id)},
        )
        return result.first() is not None

"""SQL for template assets - migration 0042's `template_asset` table.

Thin on purpose, mirroring `api.templates.repository` and
`api.documents.repository`: tenant coherence and the content-type CHECK live
in the database, and this file is statements and row mapping.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.templates.asset_content_type import TemplateAssetContentType
from api.templates.assets import TemplateAsset

_ASSET_COLUMNS = (
    "id, organization_id, administration_id, storage_key, content_type, sanitized, created_at"
)


def _asset(row: Any) -> TemplateAsset:
    return TemplateAsset(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        storage_key=row.storage_key,
        content_type=TemplateAssetContentType(row.content_type),
        sanitized=row.sanitized,
        created_at=row.created_at,
    )


class SqlTemplateAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        storage_key: str,
        content_type: str,
        sanitized: bool,
    ) -> TemplateAsset:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO template_asset (
                    organization_id, administration_id, storage_key, content_type, sanitized
                ) VALUES (
                    :org, :admin, :storage_key, :content_type, :sanitized
                )
                RETURNING {_ASSET_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "storage_key": storage_key,
                "content_type": content_type,
                "sanitized": sanitized,
            },
        )
        return _asset(result.one())

    async def get(
        self, *, administration_id: uuid.UUID, asset_id: uuid.UUID
    ) -> TemplateAsset | None:
        result = await self._session.execute(
            text(
                f"SELECT {_ASSET_COLUMNS} FROM template_asset "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(asset_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _asset(row)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

"""SQLAlchemy-backed SwitcherRepository over administration as extended by
migration 0018.

The search predicates are built to match the indexes 0018 creates - a
trigram GIN on each name column and a text_pattern_ops btree on kvk_number -
so a firm with hundreds of clients filters in the database rather than
transferring the portfolio to filter in Python.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.firm.switcher import ClientBadge, SwitcherEntry, initials_for, is_kvk_query

# "Showing only granted administrations" (FR-FRM-000) is this join, not a
# filter applied to a wider result: the switcher never sees an administration
# the user holds no live grant on, so there is nothing to accidentally leak
# into a response body.
#
# A grant reaches an administration two ways, and both are joined: an
# administration-scoped grant names it directly (firm staff, IAM-107), and an
# organization-scoped grant cascades to the administrations that organization
# OWNS (ADR-011) - which is how a self-managed business's Owner sees their own
# books here without a second grant on each. The cascade keys on ownership,
# never on firm_engagement, so a firm's organization grant reaches no client.
# See ADR-059.
#
# DISTINCT ON keeps one row per administration when a user holds several
# grants on it, preferring the administration-scoped one and then the most
# recently granted - which is the role a switcher should show.
_BASE = """
    SELECT DISTINCT ON (a.id)
           a.id, a.legal_name, a.trade_name, a.kvk_number, a.colour_token,
           (a.vat_number IS NOT NULL OR a.ob_number IS NOT NULL) AS vat_registered,
           r.name AS role_name, r.is_system AS role_is_system, ra.expires_at
    FROM role_assignment ra
    JOIN "role" r          ON r.id = ra.role_id
    JOIN administration a  ON (ra.scope_type = 'administration' AND a.id = ra.scope_id)
                           OR (ra.scope_type = 'organization' AND a.organization_id = ra.scope_id)
    WHERE ra.user_id = :user_id
      AND ra.revoked_at IS NULL
      AND (ra.expires_at IS NULL OR ra.expires_at > :now)
      AND r.archived_at IS NULL
      AND a.status = 'active'
"""

# Names by substring (a bookkeeper types the fragment they remember, not the
# opening of the legal name); KvK by prefix.
_NAME_FILTER = """
      AND (a.legal_name ILIKE :contains OR a.trade_name ILIKE :contains)
"""
_KVK_FILTER = """
      AND a.kvk_number LIKE :prefix
"""

# Exact, then prefix, then substring - so the first keyboard-selectable
# result is the one someone typing a specific client's name is reaching for.
# The interaction FR-FRM-000 calls keyboard-reachable is type-then-Enter, and
# that only works if position 1 is predictable.
_ORDER = """
    ORDER BY a.id, (ra.scope_type = 'administration') DESC, ra.created_at DESC
"""
_RANKED = """
    SELECT * FROM ({inner}) ranked
    ORDER BY
        CASE
            WHEN lower(ranked.legal_name) = :exact THEN 0
            WHEN lower(coalesce(ranked.trade_name, '')) = :exact THEN 0
            WHEN lower(ranked.legal_name) LIKE :starts THEN 1
            WHEN lower(coalesce(ranked.trade_name, '')) LIKE :starts THEN 1
            ELSE 2
        END,
        coalesce(ranked.trade_name, ranked.legal_name)
"""
_UNRANKED = """
    SELECT * FROM ({inner}) ranked
    ORDER BY coalesce(ranked.trade_name, ranked.legal_name)
"""


class SqlSwitcherRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def granted_administrations(
        self, *, user_id: uuid.UUID, query: str | None, now: datetime
    ) -> Sequence[SwitcherEntry]:
        params: dict[str, object] = {"user_id": str(user_id), "now": now}
        inner = _BASE

        if query is not None:
            if is_kvk_query(query):
                inner += _KVK_FILTER
                params["prefix"] = f"{query}%"
            else:
                inner += _NAME_FILTER
                params["contains"] = f"%{query}%"
            params["exact"] = query.lower()
            params["starts"] = f"{query.lower()}%"
            sql = _RANKED.format(inner=inner + _ORDER)
        else:
            sql = _UNRANKED.format(inner=inner + _ORDER)

        result = await self._session.execute(text(sql), params)
        return [
            SwitcherEntry(
                badge=ClientBadge(
                    administration_id=row.id,
                    name=row.legal_name,
                    trade_name=row.trade_name,
                    kvk_number=row.kvk_number,
                    colour_token=row.colour_token,
                    initials=initials_for(row.trade_name or row.legal_name),
                ),
                role_name=row.role_name,
                # FR-LOC-001: a system role's name is LEDGR's vocabulary and is
                # translated; a custom role's is the organization's own words
                # and is not. Only this column can tell them apart.
                role_is_system=bool(row.role_is_system),
                expires_at=row.expires_at,
                vat_registered=bool(row.vat_registered),
            )
            for row in result
        ]

"""SQLAlchemy-backed IntegrityRepository over migration 0023 (NFR-033).

Two statements, both SELECTs against `ledger.*` functions. This module holds
no checking logic of its own and must not grow any: the comparison has to be
made against the rows by the database, or it is a test of whatever this
process happened to read. See api/ledger/integrity.py's module docstring.

Unlike api/ledger/repository.py, nothing here needs a privilege the calling
role does not already have. The 0023 functions are SECURITY INVOKER, so:

    as ledgr_app   RLS scopes the result to the caller's tenant
    as ledgr_ops   BYPASSRLS covers every tenant in one pass

which means the same two calls serve an administrator checking their own books
and the nightly cross-tenant sweep, with the tenant boundary enforced by the
data layer in both cases rather than by an argument this module passes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.ledger.integrity import Deviation, IntegrityCheck, IntegrityScope

_FINDING_COLUMNS = """
    check_name, requirement, deviation, organization_id, administration_id,
    subject_type, subject_id, summary, detail
"""


def _detail(raw: object) -> Mapping[str, Any]:
    """jsonb arrives as a dict or as a JSON string depending on whether the
    driver has a codec registered for it. Both are handled rather than one
    being assumed, for the same reason
    tests/integration/test_audit_tamper_evidence.py handles both: getting it
    wrong here would silently empty the numbers out of every alert.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _deviation(row: Any) -> Deviation:
    try:
        check = IntegrityCheck(row.check_name)
    except ValueError as exc:
        # Deliberately fatal. A finding this code cannot classify is still a
        # finding, and the one thing an integrity job must never do is drop
        # one. This means the database is running a newer 0023 than the
        # deployed code - a rollout ordering problem, which is worth failing
        # the run over rather than reporting a ledger as clean because part of
        # the report was unreadable.
        raise RuntimeError(
            f"ledger.integrity_findings returned an unknown check {row.check_name!r}. "
            "The database schema is ahead of this code; the report cannot be "
            "trusted to be complete."
        ) from exc

    return Deviation(
        check=check,
        requirement=row.requirement,
        deviation=row.deviation,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        summary=row.summary,
        detail=_detail(row.detail),
    )


class SqlIntegrityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def scope(self, *, administration_id: uuid.UUID | None = None) -> IntegrityScope:
        result = await self._session.execute(
            text(
                "SELECT administrations, journals, accounts, entries, lines "
                "FROM ledger.integrity_scope(cast(:administration_id as uuid))"
            ),
            {"administration_id": (str(administration_id) if administration_id else None)},
        )
        row = result.one()
        return IntegrityScope(
            administrations=int(row.administrations),
            journals=int(row.journals),
            accounts=int(row.accounts),
            entries=int(row.entries),
            lines=int(row.lines),
        )

    async def findings(self, *, administration_id: uuid.UUID | None = None) -> Sequence[Deviation]:
        """One statement, therefore one snapshot.

        Splitting the three checks into three round trips would let a posting
        commit between them and produce a report that describes no state the
        database was ever in.
        """
        result = await self._session.execute(
            text(
                f"SELECT {_FINDING_COLUMNS} "
                "FROM ledger.integrity_findings(cast(:administration_id as uuid))"
            ),
            {"administration_id": (str(administration_id) if administration_id else None)},
        )
        return [_deviation(row) for row in result]

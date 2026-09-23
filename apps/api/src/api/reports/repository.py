"""A minimal chart-of-accounts read for the Reports screen.

Not `api.ledger.chart.ChartOfAccountsService`: that service does its OWN
internal authorization check against `view chart_of_accounts`, and this
screen is gated on `view report` instead (Appendix A's own "View reports"
row - the same permission `api.ledger_reads.routes` uses for the trial
balance and the journal). Going through the chart service here would gate
this screen on a second, different permission by accident. This is a plain
SELECT on `ledger_account` - not a posting table, so it carries none of
`api.ledger.repository`'s import restriction (CLAUDE.md non-negotiable #1 is
about WRITES to journal_entry/journal_line/journal_sequence).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AccountRef:
    __slots__ = ("code", "name", "account_type")

    def __init__(self, code: str, name: str, account_type: str) -> None:
        self.code = code
        self.name = name
        self.account_type = account_type


class SqlReportsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def accounts_by_id(
        self, *, administration_id: uuid.UUID
    ) -> Mapping[uuid.UUID, AccountRef]:
        result = await self._session.execute(
            text(
                "SELECT id, code, name, account_type FROM ledger_account "
                "WHERE administration_id = :admin"
            ),
            {"admin": str(administration_id)},
        )
        return {
            row.id: AccountRef(code=row.code, name=row.name, account_type=row.account_type)
            for row in result
        }
